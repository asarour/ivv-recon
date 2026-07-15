"""Data access.

Two implementations:

  SQLConnector       -- real databases via SQLAlchemy/DBAPI.
  InMemoryConnector  -- lists of tuples. Used by the test suite and `demo`, so the
                        reconciliation logic can be exercised with no database.

Both satisfy the same Protocol, so engine.py cannot tell them apart.
"""

from __future__ import annotations

from typing import Any, Iterator, Protocol, Sequence

from .canonical import key_tuple, row_hash
from .dialects import Dialect
from .schema import Column, TableRef


class Connector(Protocol):
    """Everything the engine needs from one side of the comparison."""

    dialect: Dialect

    def count_rows(self, table: TableRef, where: str | None = None) -> int: ...

    def key_hashes(
        self, table: TableRef, keys: Sequence[Column], compare: Sequence[Column],
        where: str | None = None,
    ) -> Iterator[tuple[tuple[str, ...], str]]: ...

    def null_counts(
        self, table: TableRef, columns: Sequence[Column], where: str | None = None
    ) -> dict[str, int]: ...

    def aggregates(
        self, table: TableRef, columns: Sequence[Column], where: str | None = None
    ) -> dict[str, dict[str, Any]]: ...

    def rows_by_keys(
        self, table: TableRef, keys: Sequence[Column], compare: Sequence[Column],
        key_values: Sequence[tuple[str, ...]],
    ) -> dict[tuple[str, ...], list[Any]]: ...


class SQLConnector:
    """Talks to a real database through a DBAPI/SQLAlchemy connection.

    The connection is injected rather than constructed here. This module should
    never see a credential -- that is the caller's problem, and it keeps secrets
    out of the config file and out of tracebacks.
    """

    def __init__(self, connection: Any, dialect: Dialect, fetch_size: int = 10_000):
        self.conn = connection
        self.dialect = dialect
        self.fetch_size = fetch_size

    # -- internals ------------------------------------------------------

    def _execute(self, sql: str, params: Sequence[Any] | None = None):
        cur = self.conn.cursor()
        cur.execute(sql, params or [])
        return cur

    def _where(self, where: str | None) -> str:
        # NOTE: `where` is operator-supplied config, not user input. It is
        # interpolated, not parameterised, because a predicate cannot be bound.
        # Anyone who can edit the config can already reach the database.
        return f" WHERE {where}" if where else ""

    def _table(self, table: TableRef) -> str:
        return self.dialect.qualify(*table.parts())

    # -- Connector ------------------------------------------------------

    def count_rows(self, table: TableRef, where: str | None = None) -> int:
        sql = f"SELECT COUNT(*) FROM {self._table(table)}{self._where(where)}"
        cur = self._execute(sql)
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def key_hashes(
        self, table: TableRef, keys: Sequence[Column], compare: Sequence[Column],
        where: str | None = None,
    ) -> Iterator[tuple[tuple[str, ...], str]]:
        """Stream (key, row_hash) pairs.

        Only the key and a 64-char hash cross the wire, never the row itself.
        That is what makes this viable on a table with 200 columns -- and it is
        why a 40M-row table costs megabytes, not gigabytes, to reconcile.
        """
        key_exprs = [self.dialect.canonical_expr(k) for k in keys]

        if self.dialect.supports_pushdown_hash():
            hash_expr = self.dialect.row_hash_expr(compare)
            select = ", ".join(key_exprs + [hash_expr])
            sql = f"SELECT {select} FROM {self._table(table)}{self._where(where)}"
            cur = self._execute(sql)
            nk = len(keys)
            while True:
                batch = cur.fetchmany(self.fetch_size)
                if not batch:
                    break
                for row in batch:
                    yield tuple(str(v) for v in row[:nk]), str(row[nk])
        else:
            # Fallback: pull canonical column text and hash locally. Same bytes on
            # the wire as the row itself, so it is slow -- but it is correct, which
            # matters more than fast when the alternative is a truncated hash.
            compare_exprs = [self.dialect.canonical_expr(c) for c in compare]
            select = ", ".join(key_exprs + compare_exprs)
            sql = f"SELECT {select} FROM {self._table(table)}{self._where(where)}"
            cur = self._execute(sql)
            nk = len(keys)
            while True:
                batch = cur.fetchmany(self.fetch_size)
                if not batch:
                    break
                for row in batch:
                    k = tuple(str(v) for v in row[:nk])
                    # Values are already canonical text from SQL; hash the joined form.
                    from .dialects import FIELD_SEPARATOR
                    import hashlib

                    payload = FIELD_SEPARATOR.join(str(v) for v in row[nk:])
                    yield k, hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def null_counts(
        self, table: TableRef, columns: Sequence[Column], where: str | None = None
    ) -> dict[str, int]:
        if not columns:
            return {}
        exprs = [self.dialect.null_count_expr(c) for c in columns]
        sql = f"SELECT {', '.join(exprs)} FROM {self._table(table)}{self._where(where)}"
        cur = self._execute(sql)
        row = cur.fetchone() or []
        return {c.name: int(v or 0) for c, v in zip(columns, row)}

    def aggregates(
        self, table: TableRef, columns: Sequence[Column], where: str | None = None
    ) -> dict[str, dict[str, Any]]:
        from .schema import LogicalType

        numeric = [
            c for c in columns
            if c.logical_type in (LogicalType.INTEGER, LogicalType.DECIMAL, LogicalType.FLOAT)
        ]
        if not numeric:
            return {}

        selects: list[str] = []
        layout: list[tuple[str, str]] = []
        for c in numeric:
            q = self.dialect.quote_ident(c.name)
            for stat, expr in (
                ("sum", f"SUM(CAST({q} AS DECIMAL(38,10)))"),
                ("min", f"MIN({q})"),
                ("max", f"MAX({q})"),
                ("count_distinct", f"COUNT(DISTINCT {q})"),
            ):
                selects.append(expr)
                layout.append((c.name, stat))

        sql = f"SELECT {', '.join(selects)} FROM {self._table(table)}{self._where(where)}"
        cur = self._execute(sql)
        row = cur.fetchone() or []
        out: dict[str, dict[str, Any]] = {}
        for (col, stat), val in zip(layout, row):
            out.setdefault(col, {})[stat] = val
        return out

    def rows_by_keys(
        self, table: TableRef, keys: Sequence[Column], compare: Sequence[Column],
        key_values: Sequence[tuple[str, ...]],
    ) -> dict[tuple[str, ...], list[Any]]:
        """Fetch canonical column values for specific keys, for drilldown.

        Bounded by MAX_SAMPLES upstream -- this is never called for the full set.
        """
        if not key_values:
            return {}
        key_exprs = [self.dialect.canonical_expr(k) for k in keys]
        cmp_exprs = [self.dialect.canonical_expr(c) for c in compare]

        if len(keys) == 1:
            placeholders = ", ".join("?" for _ in key_values)
            pred = f"{key_exprs[0]} IN ({placeholders})"
            params: list[Any] = [kv[0] for kv in key_values]
        else:
            ors = []
            params = []
            for kv in key_values:
                ors.append("(" + " AND ".join(f"{e} = ?" for e in key_exprs) + ")")
                params.extend(kv)
            pred = " OR ".join(ors)

        select = ", ".join(key_exprs + cmp_exprs)
        sql = f"SELECT {select} FROM {self._table(table)} WHERE {pred}"
        cur = self._execute(sql, params)
        nk = len(keys)
        out: dict[tuple[str, ...], list[Any]] = {}
        for row in cur.fetchall():
            out[tuple(str(v) for v in row[:nk])] = list(row[nk:])
        return out


class InMemoryConnector:
    """Rows as Python tuples. Lets the engine run with no database at all."""

    def __init__(self, rows: Sequence[Sequence[Any]], columns: Sequence[Column],
                 dialect: Dialect):
        self.columns = list(columns)
        self.rows = [list(r) for r in rows]
        self.dialect = dialect
        self._idx = {c.name: i for i, c in enumerate(self.columns)}

    def _cols(self, cols: Sequence[Column]) -> list[int]:
        missing = [c.name for c in cols if c.name not in self._idx]
        if missing:
            raise KeyError(f"columns not present in this dataset: {missing}")
        return [self._idx[c.name] for c in cols]

    def count_rows(self, table: TableRef, where: str | None = None) -> int:
        return len(self.rows)

    def key_hashes(self, table, keys, compare, where=None):
        ki = self._cols(keys)
        ci = self._cols(compare)
        for r in self.rows:
            k = key_tuple([r[i] for i in ki], keys)
            h = row_hash([r[i] for i in ci], compare)
            yield k, h

    def null_counts(self, table, columns, where=None):
        idx = self._cols(columns)
        return {
            c.name: sum(1 for r in self.rows if r[i] is None)
            for c, i in zip(columns, idx)
        }

    def aggregates(self, table, columns, where=None):
        from decimal import Decimal
        from .schema import LogicalType

        out: dict[str, dict[str, Any]] = {}
        for c in columns:
            if c.logical_type not in (LogicalType.INTEGER, LogicalType.DECIMAL, LogicalType.FLOAT):
                continue
            i = self._idx[c.name]
            vals = [r[i] for r in self.rows if r[i] is not None]
            out[c.name] = {
                "sum": sum(Decimal(str(v)) for v in vals) if vals else None,
                "min": min(vals) if vals else None,
                "max": max(vals) if vals else None,
                "count_distinct": len(set(vals)),
            }
        return out

    def rows_by_keys(self, table, keys, compare, key_values):
        ki = self._cols(keys)
        ci = self._cols(compare)
        want = set(key_values)
        out: dict[tuple[str, ...], list[Any]] = {}
        for r in self.rows:
            k = key_tuple([r[i] for i in ki], keys)
            if k in want:
                from .canonical import canonical_value

                out[k] = [canonical_value(r[i], c.logical_type) for i, c in zip(ci, compare)]
        return out
