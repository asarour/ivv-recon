"""Python-side canonicalization, mirroring dialects.py exactly.

Two reasons this exists:

1. Synapse cannot hash wide rows in-database, so we stream and hash here.
2. It makes the canonicalization rules testable without a database. The SQL in
   dialects.py and the Python here must agree; tests/test_canonical.py pins that.

If you change a rule in one place, change it in the other. That coupling is
deliberate and is the price of supporting pushdown and fallback with one semantic.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .dialects import (
    FIELD_SEPARATOR,
    NULL_SENTINEL,
    NUMERIC_SCALE,
    TIMESTAMP_SCALE,
)
from .schema import Column, LogicalType

_QUANT = Decimal(1).scaleb(-NUMERIC_SCALE)  # 1e-10


def canonical_value(value: Any, logical_type: LogicalType) -> str:
    """Render one value as canonical text. Must match the SQL in dialects.py."""
    if value is None:
        return NULL_SENTINEL

    if logical_type is LogicalType.STRING:
        # RTRIM only -- mirrors RTRIM() in SQL. Leading whitespace is data.
        return str(value).rstrip()

    if logical_type is LogicalType.INTEGER:
        return str(int(value))

    if logical_type in (LogicalType.DECIMAL, LogicalType.FLOAT):
        try:
            d = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError(f"cannot canonicalize {value!r} as numeric") from exc
        if not d.is_finite():
            # NaN/Inf have no cross-platform representation. Refuse rather than
            # invent one and silently match rows that should not match.
            raise ValueError(f"non-finite numeric cannot be canonicalized: {value!r}")
        return str(d.quantize(_QUANT, rounding=ROUND_HALF_UP))

    if logical_type is LogicalType.BOOLEAN:
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("1", "true", "t", "y", "yes"):
                return "1"
            if v in ("0", "false", "f", "n", "no"):
                return "0"
            raise ValueError(f"cannot canonicalize {value!r} as boolean")
        return "1" if bool(value) else "0"

    if logical_type is LogicalType.DATE:
        if isinstance(value, dt.datetime):
            value = value.date()
        if isinstance(value, dt.date):
            return value.isoformat()
        return str(value)[:10]

    if logical_type is LogicalType.TIMESTAMP:
        if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
            value = dt.datetime(value.year, value.month, value.day)
        if isinstance(value, dt.datetime):
            # Drop tzinfo: TIMESTAMP_NTZ vs DATETIME have no shared tz semantics.
            # Truncate rather than round -- rounding can push a value into the next
            # second and manufacture a mismatch at a day boundary.
            if value.tzinfo is not None:
                value = value.replace(tzinfo=None)
            micro = value.microsecond
            keep = micro // (10 ** (6 - TIMESTAMP_SCALE))
            frac = str(keep).zfill(TIMESTAMP_SCALE)
            return value.strftime("%Y-%m-%d %H:%M:%S") + "." + frac
        return str(value)

    if logical_type is LogicalType.BINARY:
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value).hex()
        return str(value).lower()

    raise ValueError(f"unhandled logical type: {logical_type}")


def canonical_row(values: Sequence[Any], columns: Sequence[Column]) -> str:
    """Join a row's canonical values with the field separator."""
    if len(values) != len(columns):
        raise ValueError(
            f"row has {len(values)} values but mapping has {len(columns)} columns"
        )
    return FIELD_SEPARATOR.join(
        canonical_value(v, c.logical_type) for v, c in zip(values, columns, strict=True)
    )


def row_hash(values: Sequence[Any], columns: Sequence[Column]) -> str:
    """SHA-256 of the canonical row, lowercase hex -- matches row_hash_expr()."""
    payload = canonical_row(values, columns).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def key_tuple(values: Sequence[Any], columns: Sequence[Column]) -> tuple[str, ...]:
    """Canonical key tuple, used to align rows across the two sides."""
    return tuple(canonical_value(v, c.logical_type) for v, c in zip(values, columns, strict=True))
