"""Per-platform SQL generation for canonical row hashing.

The premise of this module: a row in SQL Server and "the same" row in Snowflake
will not produce the same hash unless you canonicalize first. The differences that
actually bite on a migration:

  - CHAR(n) in SQL Server right-pads with spaces. Snowflake does not.
  - DECIMAL(18,2) and NUMBER(38,10) stringify differently ('1.50' vs '1.5000000000').
  - SQL Server DATETIME has ~3.33ms resolution; Snowflake TIMESTAMP_NTZ has ns.
  - SQL Server HASHBYTES + CONVERT(...,2) yields UPPERCASE hex; Snowflake SHA2 yields
    lowercase. Compare them raw and every single row mismatches.
  - Concatenating NULL yields NULL, which collapses an entire row to NULL. Two such
    rows then "match". That is a false negative, and it is the dangerous kind.
  - BIT vs BOOLEAN.

Each rule below exists to kill one of those.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from .schema import Column, LogicalType

#: Substituted for NULL before concatenation. Must never occur in real data.
NULL_SENTINEL = "<<NULL>>"

#: Placed between fields when concatenating a row. Without it, ('ab','c') and
#: ('a','bc') hash identically. ASCII 31 (unit separator) is not typeable.
FIELD_SEPARATOR = "\x1f"

NUMERIC_PRECISION = 38
NUMERIC_SCALE = 10

#: Fractional-second digits retained. 3 = milliseconds, the most SQL Server
#: DATETIME can faithfully represent.
TIMESTAMP_SCALE = 3


class Dialect(ABC):
    """Generates canonical SQL for one database platform."""

    name: str = "abstract"
    folds_identifiers_upper: bool = False

    # ---- identifiers -------------------------------------------------

    @abstractmethod
    def quote_ident(self, ident: str) -> str: ...

    def qualify(self, *parts: str | None) -> str:
        return ".".join(self.quote_ident(p) for p in parts if p)

    # ---- canonicalization --------------------------------------------

    @abstractmethod
    def _canonical_value(self, expr: str, logical_type: LogicalType) -> str:
        """SQL rendering `expr` as canonical text. NULL in -> NULL out."""

    def canonical_expr(self, column: Column) -> str:
        """Canonical text for a column, NULL-safe.

        Canonicalize first, then substitute the sentinel. Coalescing the raw column
        instead would force an implicit cast whose behaviour differs per platform.
        """
        raw = self.quote_ident(column.name)
        canon = self._canonical_value(raw, column.logical_type)
        return f"COALESCE({canon}, '{NULL_SENTINEL}')"

    def canonical_row_expr(self, columns: Sequence[Column]) -> str:
        cols = list(columns)
        if not cols:
            raise ValueError("cannot build a row expression with no columns")
        sep_literal = "CHAR(31)" if self.uses_char_fn_for_sep else f"'{FIELD_SEPARATOR}'"
        parts = [self.canonical_expr(c) for c in cols]
        return f" || {sep_literal} || ".join(parts)

    #: Some drivers mangle a literal 0x1F in a SQL string. Emit CHAR(31) instead.
    uses_char_fn_for_sep: bool = True

    @abstractmethod
    def row_hash_expr(self, columns: Sequence[Column]) -> str:
        """SHA-256 of the canonical row as lowercase hex."""

    @abstractmethod
    def null_count_expr(self, column: Column) -> str: ...

    def supports_pushdown_hash(self) -> bool:
        """False means hash in Python instead. Correct but slower."""
        return True


class TSQLDialect(Dialect):
    """Base for the T-SQL family: SQL Server, Azure SQL, Azure Synapse.

    Assumes SQL Server 2016+ / Azure SQL for HASHBYTES over NVARCHAR(MAX). On 2014
    and below HASHBYTES silently truncates at 8000 bytes -- rows differing past that
    boundary hash identically. See SynapseDialect for how that is handled.
    """

    name = "tsql"
    uses_char_fn_for_sep = True

    def quote_ident(self, ident: str) -> str:
        return "[" + ident.replace("]", "]]") + "]"

    def _canonical_value(self, expr: str, logical_type: LogicalType) -> str:
        if logical_type is LogicalType.STRING:
            # RTRIM only. Trailing space on CHAR(n) is padding the engine added.
            # Leading space is data -- LTRIM here would destroy real values.
            return f"RTRIM(CAST({expr} AS NVARCHAR(MAX)))"

        if logical_type is LogicalType.INTEGER:
            return f"CAST(CAST({expr} AS BIGINT) AS NVARCHAR(40))"

        if logical_type in (LogicalType.DECIMAL, LogicalType.FLOAT):
            # Binary floats never compare safely across engines. Coerce to fixed
            # scale and accept the loss: a migration that depends on the 11th
            # decimal of a FLOAT is already broken.
            return (
                f"CAST(CAST({expr} AS DECIMAL({NUMERIC_PRECISION},{NUMERIC_SCALE})) "
                f"AS NVARCHAR(60))"
            )

        if logical_type is LogicalType.BOOLEAN:
            return f"CASE WHEN {expr} = 1 THEN '1' WHEN {expr} = 0 THEN '0' ELSE NULL END"

        if logical_type is LogicalType.DATE:
            return f"CONVERT(NVARCHAR(10), CAST({expr} AS DATE), 23)"

        if logical_type is LogicalType.TIMESTAMP:
            # style 121 => 'YYYY-MM-DD HH:MI:SS.mmm'
            return f"CONVERT(NVARCHAR(23), CAST({expr} AS DATETIME2({TIMESTAMP_SCALE})), 121)"

        if logical_type is LogicalType.BINARY:
            return f"LOWER(CONVERT(NVARCHAR(MAX), {expr}, 2))"

        raise ValueError(f"unhandled logical type: {logical_type}")

    def row_hash_expr(self, columns: Sequence[Column]) -> str:
        row = self.canonical_row_expr(columns)
        # CONVERT(...,2) renders VARBINARY as hex, no 0x prefix, upper case.
        # Snowflake SHA2 is lower case. Fold here so both sides are comparable.
        return f"LOWER(CONVERT(NVARCHAR(64), HASHBYTES('SHA2_256', {row}), 2))"

    def null_count_expr(self, column: Column) -> str:
        col = self.quote_ident(column.name)
        return f"SUM(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END)"


class SQLServerDialect(TSQLDialect):
    name = "sqlserver"


class AzureSQLDialect(TSQLDialect):
    """Azure SQL Database. T-SQL surface matches SQL Server closely enough here."""

    name = "azuresql"


class SynapseDialect(TSQLDialect):
    """Azure Synapse dedicated SQL pool.

    Synapse caps HASHBYTES input at 8000 bytes -- NVARCHAR(MAX) is not accepted.
    A wide table would hash only its first 8000 bytes and report false matches, so
    pushdown is refused and the engine hashes in Python instead.
    """

    name = "synapse"

    def supports_pushdown_hash(self) -> bool:
        return False

    def _canonical_value(self, expr: str, logical_type: LogicalType) -> str:
        if logical_type is LogicalType.STRING:
            return f"RTRIM(CAST({expr} AS NVARCHAR(4000)))"
        if logical_type is LogicalType.BINARY:
            return f"LOWER(CONVERT(NVARCHAR(4000), {expr}, 2))"
        return super()._canonical_value(expr, logical_type)


class SnowflakeDialect(Dialect):
    name = "snowflake"
    folds_identifiers_upper = True
    uses_char_fn_for_sep = True

    def quote_ident(self, ident: str) -> str:
        return '"' + ident.replace('"', '""') + '"'

    def _canonical_value(self, expr: str, logical_type: LogicalType) -> str:
        if logical_type is LogicalType.STRING:
            return f"RTRIM(CAST({expr} AS VARCHAR))"

        if logical_type is LogicalType.INTEGER:
            return f"TO_VARCHAR(CAST({expr} AS NUMBER(38,0)))"

        if logical_type in (LogicalType.DECIMAL, LogicalType.FLOAT):
            return f"TO_VARCHAR(CAST({expr} AS NUMBER({NUMERIC_PRECISION},{NUMERIC_SCALE})))"

        if logical_type is LogicalType.BOOLEAN:
            return f"IFF({expr}, '1', '0')"

        if logical_type is LogicalType.DATE:
            return f"TO_VARCHAR(CAST({expr} AS DATE), 'YYYY-MM-DD')"

        if logical_type is LogicalType.TIMESTAMP:
            return (
                f"TO_VARCHAR(CAST({expr} AS TIMESTAMP_NTZ({TIMESTAMP_SCALE})), "
                f"'YYYY-MM-DD HH24:MI:SS.FF{TIMESTAMP_SCALE}')"
            )

        if logical_type is LogicalType.BINARY:
            return f"LOWER(TO_VARCHAR({expr}, 'HEX'))"

        raise ValueError(f"unhandled logical type: {logical_type}")

    def row_hash_expr(self, columns: Sequence[Column]) -> str:
        row = self.canonical_row_expr(columns)
        return f"SHA2({row}, 256)"

    def null_count_expr(self, column: Column) -> str:
        col = self.quote_ident(column.name)
        return f"SUM(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END)"


_REGISTRY: dict[str, type[Dialect]] = {
    "sqlserver": SQLServerDialect,
    "mssql": SQLServerDialect,
    "azuresql": AzureSQLDialect,
    "synapse": SynapseDialect,
    "snowflake": SnowflakeDialect,
}


def get_dialect(name: str) -> Dialect:
    key = (name or "").strip().lower()
    if key not in _REGISTRY:
        valid = ", ".join(sorted(_REGISTRY))
        raise KeyError(f"unknown dialect {name!r}; valid options: {valid}")
    return _REGISTRY[key]()


def registered_dialects() -> list[str]:
    return sorted(_REGISTRY)
