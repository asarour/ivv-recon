"""A demo dataset with deliberately seeded defects.

Every defect below is one I have actually had to chase down on a migration. The
point of the demo is that `ivv-recon demo` finds all of them without a database,
so a reviewer can see what the tool does in about four seconds.

Seeded defects:
  1. Row 1004 missing from target              -> data loss (CRITICAL)
  2. Row 9999 present only in target           -> phantom row (HIGH)
  3. Row 1002 SURNAME trailing-space padding   -> should NOT flag; canonicalized
  4. Row 1003 BALANCE 100.5 vs 100.50          -> should NOT flag; canonicalized
  5. Row 1005 EMAIL NULL -> '' in target       -> null substitution (HIGH)
  6. Row 1006 BALANCE genuinely wrong          -> content mismatch (CRITICAL)
  7. Row 1007 OPENED_AT ms truncation          -> should NOT flag; canonicalized
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from .connectors import InMemoryConnector
from .dialects import get_dialect
from .schema import Column, ColumnMapping, MappingSpec, TableRef

SOURCE_COLUMNS = [
    Column.from_native("CUSTOMER_ID", "INT", nullable=False),
    Column.from_native("SURNAME", "CHAR(20)"),
    Column.from_native("EMAIL", "VARCHAR(100)"),
    Column.from_native("BALANCE", "DECIMAL(18,2)"),
    Column.from_native("IS_ACTIVE", "BIT"),
    Column.from_native("OPENED_AT", "DATETIME"),
]

TARGET_COLUMNS = [
    Column.from_native("CUSTOMER_ID", "NUMBER(38,0)", nullable=False),
    Column.from_native("SURNAME", "VARCHAR(20)"),
    Column.from_native("EMAIL", "VARCHAR(100)"),
    Column.from_native("BALANCE", "NUMBER(38,10)"),
    Column.from_native("IS_ACTIVE", "BOOLEAN"),
    Column.from_native("OPENED_AT", "TIMESTAMP_NTZ(9)"),
]

_TS = dt.datetime(2024, 3, 14, 9, 26, 53, 123000)

SOURCE_ROWS = [
    [1001, "Okonkwo", "ada@example.com", Decimal("2500.00"), 1, _TS],
    # CHAR(20) padding -- the engine added these spaces, they are not data.
    [1002, "Nakamura           ", "kenji@example.com", Decimal("18400.75"), 1, _TS],
    # 100.5 vs 100.50 -- same number, different text.
    [1003, "Silva", "mara@example.com", Decimal("100.5"), 0, _TS],
    # Never arrives in target.
    [1004, "Haddad", "omar@example.com", Decimal("77250.10"), 1, _TS],
    # EMAIL is NULL here, becomes '' in target.
    [1005, "Petrov", None, Decimal("330.00"), 1, _TS],
    # BALANCE genuinely wrong in target.
    [1006, "Adeyemi", "tunde@example.com", Decimal("9100.00"), 1, _TS],
    # Sub-millisecond precision that SQL Server DATETIME cannot hold anyway.
    [1007, "Larsen", "ingrid@example.com", Decimal("42.00"), 0,
     dt.datetime(2024, 3, 14, 9, 26, 53, 123456)],
]

TARGET_ROWS = [
    [1001, "Okonkwo", "ada@example.com", Decimal("2500.0000000000"), True, _TS],
    [1002, "Nakamura", "kenji@example.com", Decimal("18400.7500000000"), True, _TS],
    [1003, "Silva", "mara@example.com", Decimal("100.5000000000"), False, _TS],
    # 1004 absent -- data loss.
    [1005, "Petrov", "", Decimal("330.0000000000"), True, _TS],   # NULL -> ''
    [1006, "Adeyemi", "tunde@example.com", Decimal("9100.9900000000"), True, _TS],  # wrong
    [1007, "Larsen", "ingrid@example.com", Decimal("42.0000000000"), False,
     dt.datetime(2024, 3, 14, 9, 26, 53, 123999)],
    # Phantom row -- in target, never in source.
    [9999, "Ghost", "ghost@example.com", Decimal("0.0000000000"), False, _TS],
]


def build_spec() -> MappingSpec:
    s = {c.name: c for c in SOURCE_COLUMNS}
    t = {c.name: c for c in TARGET_COLUMNS}
    return MappingSpec(
        source_table=TableRef("sqlserver", "CoreBank", "dbo", "CUSTOMER"),
        target_table=TableRef("snowflake", "ANALYTICS", "RAW", "CUSTOMER"),
        key_columns=[ColumnMapping(s["CUSTOMER_ID"], t["CUSTOMER_ID"])],
        compare_columns=[
            ColumnMapping(s["SURNAME"], t["SURNAME"], "CHAR(20) to VARCHAR(20); padding dropped"),
            ColumnMapping(s["EMAIL"], t["EMAIL"]),
            ColumnMapping(s["BALANCE"], t["BALANCE"], "DECIMAL(18,2) widened to NUMBER(38,10)"),
            ColumnMapping(s["IS_ACTIVE"], t["IS_ACTIVE"], "BIT to BOOLEAN"),
            ColumnMapping(s["OPENED_AT"], t["OPENED_AT"], "DATETIME to TIMESTAMP_NTZ"),
        ],
    )


def build_connectors() -> tuple[InMemoryConnector, InMemoryConnector]:
    return (
        InMemoryConnector(SOURCE_ROWS, SOURCE_COLUMNS, get_dialect("sqlserver")),
        InMemoryConnector(TARGET_ROWS, TARGET_COLUMNS, get_dialect("snowflake")),
    )
