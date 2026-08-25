"""Orchestration: fetch, run checks, assemble the report.

Check order is deliberate and cheap-to-expensive:

  1. schema types   -- free, no query
  2. row counts     -- one COUNT(*) per side
  3. key integrity  -- needs the key set
  4. key sets       -- set difference on keys already in memory
  5. row hashes     -- the expensive one
  6. field level    -- only for rows already known to mismatch, capped
  7. null handling  -- one aggregate query per side
  8. aggregates     -- one aggregate query per side, independent of the hash path

If key integrity fails, row-level checks are skipped: with a non-unique key, row
alignment is meaningless and every downstream finding would be noise.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from . import checks
from .checks import MAX_SAMPLES
from .connectors import Connector
from .findings import CheckResult, CheckStatus, ReconReport, Severity
from .schema import MappingSpec

log = logging.getLogger(__name__)


class ReconEngine:
    def __init__(
        self,
        source: Connector,
        target: Connector,
        spec: MappingSpec,
        source_where: str | None = None,
        target_where: str | None = None,
        fail_threshold: Severity = Severity.HIGH,
        aggregate_tolerance: float = 0.0,
    ):
        spec.validate()
        self.source = source
        self.target = target
        self.spec = spec
        self.source_where = source_where
        self.target_where = target_where
        self.fail_threshold = fail_threshold
        self.aggregate_tolerance = aggregate_tolerance

    def _timed(self, name: str, fn, *args, **kwargs) -> CheckResult:
        """Run a check, time it, and convert an exception into an ERROR result.

        A check that raises must never look like a check that passed. ERROR is a
        distinct status and it fails the run.
        """
        t0 = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 -- deliberate: isolate check failures
            log.exception("check %s raised", name)
            result = CheckResult(name, CheckStatus.ERROR, error=f"{type(exc).__name__}: {exc}")
        result.duration_ms = (time.perf_counter() - t0) * 1000
        return result

    def run(self) -> ReconReport:
        spec = self.spec
        report = ReconReport(
            source_table=str(spec.source_table),
            target_table=str(spec.target_table),
            source_dialect=self.source.dialect.name,
            target_dialect=self.target.dialect.name,
            fail_threshold=self.fail_threshold,
        )

        # 1. schema -----------------------------------------------------
        report.results.append(self._timed("schema_types", checks.check_schema_types, spec))

        # 2. counts -----------------------------------------------------
        def _counts() -> CheckResult:
            s = self.source.count_rows(spec.source_table, self.source_where)
            t = self.target.count_rows(spec.target_table, self.target_where)
            return checks.check_row_counts(s, t)

        count_result = self._timed("row_counts", _counts)
        report.results.append(count_result)
        source_total = count_result.metrics.get("source_rows", 0) or 0
        target_total = count_result.metrics.get("target_rows", 0) or 0

        # 3. pull key+hash from both sides ------------------------------
        source_map: dict[tuple[str, ...], str] = {}
        target_map: dict[tuple[str, ...], str] = {}
        fetch_ok = True
        try:
            log.info("fetching source key hashes from %s", spec.source_table)
            for k, h in self.source.key_hashes(
                spec.source_table, spec.source_keys(), spec.source_compare(), self.source_where
            ):
                source_map[k] = h
            log.info("fetching target key hashes from %s", spec.target_table)
            for k, h in self.target.key_hashes(
                spec.target_table, spec.target_keys(), spec.target_compare(), self.target_where
            ):
                target_map[k] = h
        except Exception as exc:  # noqa: BLE001
            log.exception("key hash fetch failed")
            fetch_ok = False
            report.results.append(
                CheckResult("key_hash_fetch", CheckStatus.ERROR,
                            error=f"{type(exc).__name__}: {exc}")
            )

        if fetch_ok:
            # NOTE: key_hashes() yields into a dict, so duplicate keys collapse.
            # Detect them from the raw stream count vs dict size instead.
            src_keys = self._reread_keys(self.source, spec, "source")
            tgt_keys = self._reread_keys(self.target, spec, "target")

            src_integrity = self._timed(
                "key_integrity_source", checks.check_key_integrity, "source", src_keys
            )
            tgt_integrity = self._timed(
                "key_integrity_target", checks.check_key_integrity, "target", tgt_keys
            )
            report.results.append(src_integrity)
            report.results.append(tgt_integrity)

            keys_usable = (
                src_integrity.status is CheckStatus.PASS
                and tgt_integrity.status is CheckStatus.PASS
            )

            if not keys_usable:
                log.warning("key integrity failed; skipping row-level comparison")
                report.results.append(
                    CheckResult(
                        "key_sets", CheckStatus.SKIPPED,
                        error="skipped: declared key is not unique or contains NULLs",
                    )
                )
                report.results.append(
                    CheckResult(
                        "row_hashes", CheckStatus.SKIPPED,
                        error="skipped: cannot align rows without a reliable key",
                    )
                )
            else:
                # 4. set comparison -------------------------------------
                report.results.append(
                    self._timed("key_sets", checks.check_key_sets,
                                source_map.keys(), target_map.keys())
                )

                # 5. row hashes ----------------------------------------
                hash_result = self._timed("row_hashes", checks.check_row_hashes,
                                          source_map, target_map)
                report.results.append(hash_result)

                # 6. field-level drilldown -----------------------------
                mismatched = [
                    k for k in (source_map.keys() & target_map.keys())
                    if source_map[k] != target_map[k]
                ]
                if mismatched:
                    report.results.append(self._drilldown(mismatched))

        # 7. nulls ------------------------------------------------------
        def _nulls() -> CheckResult:
            s = self.source.null_counts(spec.source_table, spec.source_compare(), self.source_where)
            t = self.target.null_counts(spec.target_table, spec.target_compare(), self.target_where)
            return checks.check_null_handling(s, t, source_total, target_total, spec.compare_columns)

        report.results.append(self._timed("null_handling", _nulls))

        # 8. aggregates -------------------------------------------------
        def _aggs() -> CheckResult:
            s = self.source.aggregates(spec.source_table, spec.source_compare(), self.source_where)
            t_raw = self.target.aggregates(spec.target_table, spec.target_compare(), self.target_where)
            # Aggregates come back keyed by each side's own column names; realign
            # to source names so a renamed column still compares.
            rename = {m.target.name: m.source.name for m in spec.compare_columns}
            t = {rename.get(k, k): v for k, v in t_raw.items()}
            return checks.check_aggregates(s, t, self.aggregate_tolerance)

        report.results.append(self._timed("aggregates", _aggs))

        report.finished_at = datetime.now(timezone.utc)
        return report

    def _reread_keys(self, conn: Connector, spec: MappingSpec, side: str) -> list[tuple[str, ...]]:
        """Re-stream keys as a list so duplicates survive for the integrity check."""
        table = spec.source_table if side == "source" else spec.target_table
        keys = spec.source_keys() if side == "source" else spec.target_keys()
        compare = spec.source_compare() if side == "source" else spec.target_compare()
        where = self.source_where if side == "source" else self.target_where
        return [k for k, _ in conn.key_hashes(table, keys, compare, where)]

    def _drilldown(self, mismatched: list[tuple[str, ...]]) -> CheckResult:
        """Field-level attribution on a bounded sample of mismatched rows."""
        spec = self.spec
        sample = sorted(mismatched)[:MAX_SAMPLES]

        def _run() -> CheckResult:
            s_rows = self.source.rows_by_keys(
                spec.source_table, spec.source_keys(), spec.source_compare(), sample
            )
            t_rows = self.target.rows_by_keys(
                spec.target_table, spec.target_keys(), spec.target_compare(), sample
            )
            res = checks.check_field_level(sample, s_rows, t_rows, spec.compare_columns)
            res.metrics["total_mismatched_rows"] = len(mismatched)
            res.metrics["rows_sampled"] = len(sample)
            return res

        return self._timed("field_level", _run)
