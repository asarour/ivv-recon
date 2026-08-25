"""Logical type model.

Native types are mapped to a small logical set so that canonicalization rules are
written once per logical type rather than once per (platform, native type) pair.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import Enum


class LogicalType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"
    BINARY = "binary"


#: Native type name -> logical type. Lower-cased, base name only (no precision).
_NATIVE_MAP: dict[str, LogicalType] = {
    # --- character ---
    "char": LogicalType.STRING,
    "nchar": LogicalType.STRING,
    "varchar": LogicalType.STRING,
    "nvarchar": LogicalType.STRING,
    "text": LogicalType.STRING,
    "ntext": LogicalType.STRING,
    "string": LogicalType.STRING,
    "uniqueidentifier": LogicalType.STRING,
    "xml": LogicalType.STRING,
    "variant": LogicalType.STRING,
    # --- integer ---
    "tinyint": LogicalType.INTEGER,
    "smallint": LogicalType.INTEGER,
    "int": LogicalType.INTEGER,
    "integer": LogicalType.INTEGER,
    "bigint": LogicalType.INTEGER,
    # --- exact numeric ---
    "decimal": LogicalType.DECIMAL,
    "numeric": LogicalType.DECIMAL,
    "number": LogicalType.DECIMAL,
    "money": LogicalType.DECIMAL,
    "smallmoney": LogicalType.DECIMAL,
    # --- approximate numeric ---
    "float": LogicalType.FLOAT,
    "real": LogicalType.FLOAT,
    "double": LogicalType.FLOAT,
    "double precision": LogicalType.FLOAT,
    # --- boolean ---
    "bit": LogicalType.BOOLEAN,
    "boolean": LogicalType.BOOLEAN,
    # --- temporal ---
    "date": LogicalType.DATE,
    "datetime": LogicalType.TIMESTAMP,
    "datetime2": LogicalType.TIMESTAMP,
    "smalldatetime": LogicalType.TIMESTAMP,
    "datetimeoffset": LogicalType.TIMESTAMP,
    "timestamp_ntz": LogicalType.TIMESTAMP,
    "timestamp_ltz": LogicalType.TIMESTAMP,
    "timestamp_tz": LogicalType.TIMESTAMP,
    "timestamp": LogicalType.TIMESTAMP,
    # --- binary ---
    "binary": LogicalType.BINARY,
    "varbinary": LogicalType.BINARY,
    "image": LogicalType.BINARY,
}


def _parse_scale(native: str) -> int | None:
    """Extract scale from a native type. 'NUMBER(38,0)' -> 0, 'DECIMAL(18,2)' -> 2."""
    m = re.search(r"\(\s*\d+\s*,\s*(\d+)\s*\)", native)
    return int(m.group(1)) if m else None


def map_native_type(native: str) -> LogicalType:
    """Map a native SQL type name to a logical type.

    Tolerates precision/scale suffixes: 'DECIMAL(18, 2)' -> DECIMAL.

    Scale matters. NUMBER(38,0) is Snowflake's landing type for a SQL Server INT
    and holds no fractional part -- it is an integer, and must canonicalize to
    '1001', not '1001.0000000000'. Treating it as DECIMAL means no key ever
    matches across the two platforms, which is a silent, total failure.

    Unknown types raise rather than defaulting. Silently defaulting an unmapped
    type to STRING is how you end up hashing a BLOB as text and reporting clean.
    """
    if native is None:
        raise ValueError("native type cannot be None")
    # Guard the YAML flow-mapping trap: {type: NUMBER(38,0)} splits on the inner
    # comma and hands us "NUMBER(38". Left alone that maps to DECIMAL instead of
    # INTEGER, and then no key ever matches. Fail loudly instead.
    if native.count("(") != native.count(")"):
        raise ValueError(
            f"malformed native type {native!r}: unbalanced parentheses. "
            f'If this came from YAML flow style, quote it: type: "NUMBER(38,0)"'
        )
    base = native.strip().lower().split("(")[0].strip()
    if base not in _NATIVE_MAP:
        raise ValueError(
            f"unmapped native type {native!r}; add it to _NATIVE_MAP in schema.py "
            f"rather than letting it default"
        )
    logical = _NATIVE_MAP[base]
    if logical is LogicalType.DECIMAL and _parse_scale(native) == 0:
        return LogicalType.INTEGER
    return logical


#: Comparison type for a mapped pair whose logical types differ. Anything not
#: listed is genuinely incomparable and raises rather than being coerced.
_UNIFY: dict[frozenset, LogicalType] = {
    frozenset({LogicalType.INTEGER, LogicalType.DECIMAL}): LogicalType.DECIMAL,
    frozenset({LogicalType.INTEGER, LogicalType.FLOAT}): LogicalType.DECIMAL,
    frozenset({LogicalType.DECIMAL, LogicalType.FLOAT}): LogicalType.DECIMAL,
    frozenset({LogicalType.BOOLEAN, LogicalType.INTEGER}): LogicalType.BOOLEAN,
    frozenset({LogicalType.DATE, LogicalType.TIMESTAMP}): LogicalType.TIMESTAMP,
}


def unify_types(a: LogicalType, b: LogicalType) -> LogicalType:
    """Common comparison type for a mapped pair.

    Both sides must canonicalize under the SAME rule or they cannot be compared.
    Canonicalizing each side under its own declared type is the bug this prevents.
    """
    if a is b:
        return a
    key = frozenset({a, b})
    if key in _UNIFY:
        return _UNIFY[key]
    raise ValueError(
        f"cannot compare {a.value} to {b.value}: no safe common representation. "
        f"Exclude the column or transform it before reconciling."
    )


@dataclass(frozen=True)
class Column:
    """One column, resolved to a logical type."""

    name: str
    logical_type: LogicalType
    nullable: bool = True
    native_type: str | None = None

    @classmethod
    def from_native(cls, name: str, native: str, nullable: bool = True) -> Column:
        return cls(
            name=name,
            logical_type=map_native_type(native),
            nullable=nullable,
            native_type=native,
        )


@dataclass
class TableRef:
    """A fully qualified table on one side of the comparison."""

    dialect: str
    database: str | None = None
    schema: str | None = None
    table: str = ""

    def parts(self) -> list[str]:
        return [p for p in (self.database, self.schema, self.table) if p]

    def __str__(self) -> str:
        return ".".join(self.parts())


@dataclass
class ColumnMapping:
    """Maps one source column to one target column.

    `transform_note` is free text describing an expected transformation. It is
    documentation only -- if a column is genuinely transformed in flight, hash
    comparison will flag it, which is correct. Exclude it or compare it separately.
    """

    source: Column
    target: Column
    transform_note: str | None = None

    def types_agree(self) -> bool:
        """Whether the DECLARED types match. Used for reporting, not comparison."""
        return self.source.logical_type is self.target.logical_type

    @property
    def comparison_type(self) -> LogicalType:
        """The single logical type both sides canonicalize under."""
        return unify_types(self.source.logical_type, self.target.logical_type)

    def source_for_compare(self) -> Column:
        return replace(self.source, logical_type=self.comparison_type)

    def target_for_compare(self) -> Column:
        return replace(self.target, logical_type=self.comparison_type)


@dataclass
class MappingSpec:
    """The full source-to-target mapping for one table pair."""

    source_table: TableRef
    target_table: TableRef
    key_columns: list[ColumnMapping] = field(default_factory=list)
    compare_columns: list[ColumnMapping] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)

    # These return columns retyped to the unified comparison type, which is what
    # every canonicalization path must use. Reporting uses the declared types.

    def source_keys(self) -> list[Column]:
        return [m.source_for_compare() for m in self.key_columns]

    def target_keys(self) -> list[Column]:
        return [m.target_for_compare() for m in self.key_columns]

    def source_compare(self) -> list[Column]:
        return [m.source_for_compare() for m in self.compare_columns]

    def target_compare(self) -> list[Column]:
        return [m.target_for_compare() for m in self.compare_columns]

    def type_mismatches(self) -> list[ColumnMapping]:
        """Mappings whose source and target logical types disagree.

        A type mismatch is not automatically a defect -- widening INT to NUMBER is
        routine -- but it is always worth a human look, because it is where silent
        truncation lives.
        """
        return [
            m
            for m in (self.key_columns + self.compare_columns)
            if not m.types_agree()
        ]

    def validate(self) -> None:
        if not self.key_columns:
            raise ValueError(
                "mapping has no key columns; without a key you can compare row "
                "counts and aggregates but you cannot identify which rows differ"
            )
        if not self.compare_columns:
            raise ValueError("mapping has no comparison columns")
        dupes = [c for c in self.excluded if c in {m.source.name for m in self.compare_columns}]
        if dupes:
            raise ValueError(f"columns both excluded and compared: {dupes}")
        # Fail at load time, not halfway through a reconciliation run.
        for m in self.key_columns + self.compare_columns:
            try:
                _ = m.comparison_type
            except ValueError as exc:
                raise ValueError(f"column '{m.source.name}': {exc}") from exc
