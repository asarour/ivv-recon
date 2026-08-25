"""The checks themselves.

Every function here is pure: it takes already-fetched data and returns a
CheckResult. No I/O. That is what makes the reconciliation logic testable without
standing up SQL Server and Snowflake, and it is why the test suite runs in CI.

Fetching lives in connectors.py; orchestration lives in engine.py.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .findings import CheckResult, CheckStatus, Finding, Severity
from .schema import ColumnMapping, MappingSpec

#: Max sample rows carried into a finding. A report containing 4M keys is unusable
#: and will crash the browser rendering it.
MAX_SAMPLES = 50


def _sample(items: Sequence[Any], limit: int = MAX_SAMPLES) -> tuple[list[Any], bool]:
    return list(items[:limit]), len(items) > limit


# ---------------------------------------------------------------------------
# 1. Row count
# ---------------------------------------------------------------------------

def check_row_counts(source_count: int, target_count: int) -> CheckResult:
    """Cheapest possible check. Run it first -- if counts are wildly off, the
    expensive hash comparison is usually a waste of a warehouse credit."""
    delta = target_count - source_count
    metrics = {
        "source_rows": source_count,
        "target_rows": target_count,
        "delta": delta,
        "delta_pct": round(delta / source_count * 100, 4) if source_count else None,
    }
    if delta == 0:
        return CheckResult("row_counts", CheckStatus.PASS, metrics=metrics)

    # Direction matters. Missing rows are data loss. Extra rows are usually a
    # re-run that double-loaded, which is recoverable but still wrong.
    if delta < 0:
        sev = Severity.CRITICAL
        summary = f"Target is missing {abs(delta):,} rows ({abs(delta)/source_count*100:.4f}% of source)"
        detail = (
            "Row count shortfall in target. Treat as data loss until proven "
            "otherwise. Common causes: filtered extract, failed batch, rows "
            "rejected on constraint violation and silently logged."
        )
    else:
        sev = Severity.HIGH
        summary = f"Target has {delta:,} rows more than source"
        detail = (
            "Row surplus in target. Common causes: job re-run without truncate, "
            "duplicate load, or a join fan-out in the transform."
        )

    return CheckResult(
        "row_counts",
        CheckStatus.FAIL,
        findings=[Finding("row_counts", sev, summary, detail, total_affected=abs(delta))],
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# 2. Key integrity
# ---------------------------------------------------------------------------

def check_key_integrity(
    side: str,
    keys: Iterable[tuple[str, ...]],
    null_sentinel: str = "<<NULL>>",
) -> CheckResult:
    """Duplicate and NULL keys.

    Run this before set comparison. If keys are not unique the whole
    key-alignment model is invalid and every downstream result is noise.
    """
    seen: dict[tuple[str, ...], int] = {}
    null_keys: list[tuple[str, ...]] = []
    total = 0

    for k in keys:
        total += 1
        seen[k] = seen.get(k, 0) + 1
        if any(part == null_sentinel for part in k):
            null_keys.append(k)

    dupes = {k: n for k, n in seen.items() if n > 1}
    findings: list[Finding] = []

    if dupes:
        dupe_rows = sum(n - 1 for n in dupes.values())
        sample, trunc = _sample([{"key": list(k), "occurrences": n} for k, n in dupes.items()])
        findings.append(
            Finding(
                "key_integrity",
                Severity.CRITICAL,
                f"{len(dupes):,} duplicate key values in {side} ({dupe_rows:,} excess rows)",
                "The declared key is not unique. Row-level comparison cannot be "
                "trusted until this is resolved -- rows will be matched arbitrarily. "
                "Either the key declaration is wrong or the table has genuine dupes.",
                samples=sample,
                sample_truncated=trunc,
                total_affected=len(dupes),
            )
        )

    if null_keys:
        sample, trunc = _sample([{"key": list(k)} for k in null_keys])
        findings.append(
            Finding(
                "key_integrity",
                Severity.HIGH,
                f"{len(null_keys):,} rows in {side} have NULL in a key column",
                "NULL key components cannot be aligned across sides. These rows are "
                "excluded from row-level comparison and must be reconciled by hand.",
                samples=sample,
                sample_truncated=trunc,
                total_affected=len(null_keys),
            )
        )

    metrics = {
        f"{side}_total_keys": total,
        f"{side}_distinct_keys": len(seen),
        f"{side}_duplicate_keys": len(dupes),
        f"{side}_null_keys": len(null_keys),
    }
    status = CheckStatus.FAIL if findings else CheckStatus.PASS
    return CheckResult(f"key_integrity_{side}", status, findings=findings, metrics=metrics)


# ---------------------------------------------------------------------------
# 3. Set comparison -- the EXCEPT / MINUS equivalent
# ---------------------------------------------------------------------------

def check_key_sets(
    source_keys: Iterable[tuple[str, ...]],
    target_keys: Iterable[tuple[str, ...]],
) -> CheckResult:
    """Which keys exist on one side only.

    This is the set-based comparison an analyst would write as
    `SELECT k FROM src EXCEPT SELECT k FROM tgt` and its mirror, done in memory
    so it works across two engines that cannot see each other.
    """
    s = set(source_keys)
    t = set(target_keys)

    missing = sorted(s - t)   # in source, absent from target => data loss
    extra = sorted(t - s)     # in target, absent from source => phantom rows

    findings: list[Finding] = []

    if missing:
        sample, trunc = _sample([{"key": list(k)} for k in missing])
        findings.append(
            Finding(
                "key_sets",
                Severity.CRITICAL,
                f"{len(missing):,} keys present in source but missing from target",
                "Rows that exist in the source and never arrived. This is the "
                "definition of data loss. Check the extract filter and the reject "
                "log before anything else.",
                samples=sample,
                sample_truncated=trunc,
                total_affected=len(missing),
            )
        )

    if extra:
        sample, trunc = _sample([{"key": list(k)} for k in extra])
        findings.append(
            Finding(
                "key_sets",
                Severity.HIGH,
                f"{len(extra):,} keys present in target but not in source",
                "Rows in the target with no source counterpart. Usually a double "
                "load or stale rows from a prior run that were not truncated. Can "
                "also be legitimate if the target is a superset -- document it.",
                samples=sample,
                sample_truncated=trunc,
                total_affected=len(extra),
            )
        )

    metrics = {
        "source_distinct_keys": len(s),
        "target_distinct_keys": len(t),
        "keys_in_both": len(s & t),
        "missing_in_target": len(missing),
        "extra_in_target": len(extra),
    }
    status = CheckStatus.FAIL if findings else CheckStatus.PASS
    return CheckResult("key_sets", status, findings=findings, metrics=metrics)


# ---------------------------------------------------------------------------
# 4. Row hash comparison
# ---------------------------------------------------------------------------

def check_row_hashes(
    source: Mapping[tuple[str, ...], str],
    target: Mapping[tuple[str, ...], str],
) -> CheckResult:
    """For keys on both sides, does the row content agree?

    Only compares the intersection. Keys present on one side only are the set
    check's problem; reporting them here too would double-count.
    """
    common = source.keys() & target.keys()
    mismatched = sorted(k for k in common if source[k] != target[k])

    findings: list[Finding] = []
    if mismatched:
        sample, trunc = _sample(
            [
                {
                    "key": list(k),
                    "source_hash": source[k][:16] + "...",
                    "target_hash": target[k][:16] + "...",
                }
                for k in mismatched
            ]
        )
        pct = len(mismatched) / len(common) * 100 if common else 0
        findings.append(
            Finding(
                "row_hashes",
                Severity.CRITICAL,
                f"{len(mismatched):,} rows differ in content ({pct:.4f}% of matched rows)",
                "Key exists on both sides but the row content diverges. Run the "
                "field-level drilldown to see which columns. If every row mismatches, "
                "suspect a canonicalization bug before suspecting the migration -- "
                "check trailing spaces on CHAR columns and datetime precision first.",
                samples=sample,
                sample_truncated=trunc,
                total_affected=len(mismatched),
            )
        )

    metrics = {
        "rows_compared": len(common),
        "rows_matching": len(common) - len(mismatched),
        "rows_mismatched": len(mismatched),
        "match_rate_pct": round((len(common) - len(mismatched)) / len(common) * 100, 6)
        if common
        else None,
    }
    status = CheckStatus.FAIL if findings else CheckStatus.PASS
    return CheckResult("row_hashes", status, findings=findings, metrics=metrics)


# ---------------------------------------------------------------------------
# 5. Field-level drilldown
# ---------------------------------------------------------------------------

def check_field_level(
    mismatched_keys: Sequence[tuple[str, ...]],
    source_rows: Mapping[tuple[str, ...], Sequence[Any]],
    target_rows: Mapping[tuple[str, ...], Sequence[Any]],
    mappings: Sequence[ColumnMapping],
) -> CheckResult:
    """Given rows already known to mismatch, attribute the difference to columns.

    The output that matters is the per-column tally: when 100% of mismatches are
    in one column, you have one transform bug, not a broken migration.
    """
    per_column: dict[str, int] = {}
    examples: dict[str, list[dict[str, Any]]] = {}

    for k in mismatched_keys:
        if k not in source_rows or k not in target_rows:
            continue
        s_row, t_row = source_rows[k], target_rows[k]
        for idx, m in enumerate(mappings):
            if idx >= len(s_row) or idx >= len(t_row):
                continue
            sv, tv = s_row[idx], t_row[idx]
            if sv != tv:
                name = m.source.name
                per_column[name] = per_column.get(name, 0) + 1
                if len(examples.setdefault(name, [])) < 5:
                    examples[name].append(
                        {"key": list(k), "source": _trunc(sv), "target": _trunc(tv)}
                    )

    findings: list[Finding] = []
    total = len(mismatched_keys)
    for name, count in sorted(per_column.items(), key=lambda kv: -kv[1]):
        share = count / total * 100 if total else 0
        # A column responsible for nearly every mismatch is a systematic transform
        # defect. A column responsible for a handful is data-specific.
        sev = Severity.CRITICAL if share >= 90 else Severity.HIGH
        mapping = next((m for m in mappings if m.source.name == name), None)
        note = f" Expected transform: {mapping.transform_note}" if mapping and mapping.transform_note else ""
        findings.append(
            Finding(
                "field_level",
                sev,
                f"Column '{name}' differs in {count:,} of {total:,} mismatched rows ({share:.1f}%)",
                (
                    f"{'Systematic' if share >= 90 else 'Partial'} divergence in this column."
                    + note
                ),
                samples=examples.get(name, []),
                total_affected=count,
            )
        )

    metrics = {"mismatched_rows_analyzed": total, "columns_implicated": len(per_column),
               "per_column_counts": per_column}
    status = CheckStatus.FAIL if findings else CheckStatus.PASS
    return CheckResult("field_level", status, findings=findings, metrics=metrics)


def _trunc(v: Any, n: int = 80) -> str:
    s = "NULL" if v is None else str(v)
    return s if len(s) <= n else s[:n] + "..."


# ---------------------------------------------------------------------------
# 6. Null-handling analysis
# ---------------------------------------------------------------------------

def check_null_handling(
    source_nulls: Mapping[str, int],
    target_nulls: Mapping[str, int],
    source_total: int,
    target_total: int,
    mappings: Sequence[ColumnMapping],
) -> CheckResult:
    """Per-column NULL rate drift.

    The classic failure this catches: a NOT NULL target column with a DEFAULT,
    where NULLs silently became '' or 0. Row counts match, keys match, and the
    data is quietly wrong.
    """
    findings: list[Finding] = []
    rows: list[dict[str, Any]] = []

    for m in mappings:
        s_n = source_nulls.get(m.source.name, 0)
        t_n = target_nulls.get(m.target.name, 0)
        s_rate = s_n / source_total * 100 if source_total else 0
        t_rate = t_n / target_total * 100 if target_total else 0
        rows.append(
            {
                "column": m.source.name,
                "source_nulls": s_n,
                "target_nulls": t_n,
                "source_null_pct": round(s_rate, 4),
                "target_null_pct": round(t_rate, 4),
            }
        )

        if s_n == t_n:
            continue

        # NULLs disappearing is the dangerous direction: it means something
        # substituted a value. NULLs appearing means something dropped one.
        if s_n > 0 and t_n == 0:
            findings.append(
                Finding(
                    "null_handling",
                    Severity.HIGH,
                    f"Column '{m.source.name}': {s_n:,} NULLs in source, zero in target",
                    "Every NULL was replaced. Check for a DEFAULT on a NOT NULL "
                    "target column, or an ISNULL/COALESCE in the transform. The "
                    "substituted value is now indistinguishable from real data.",
                    total_affected=s_n,
                )
            )
        elif t_n > s_n:
            findings.append(
                Finding(
                    "null_handling",
                    Severity.HIGH,
                    f"Column '{m.source.name}': NULLs increased from {s_n:,} to {t_n:,}",
                    "The target lost values that were populated in source. Usually a "
                    "failed lookup or an outer join that did not match.",
                    total_affected=t_n - s_n,
                )
            )
        else:
            findings.append(
                Finding(
                    "null_handling",
                    Severity.MEDIUM,
                    f"Column '{m.source.name}': NULL count drift {s_n:,} -> {t_n:,}",
                    "NULL rate changed. Explain and document, or fix.",
                    total_affected=abs(t_n - s_n),
                )
            )

    status = CheckStatus.FAIL if findings else CheckStatus.PASS
    return CheckResult("null_handling", status, findings=findings,
                       metrics={"per_column": rows})


# ---------------------------------------------------------------------------
# 7. Aggregate checks
# ---------------------------------------------------------------------------

def check_aggregates(
    source_aggs: Mapping[str, Mapping[str, Any]],
    target_aggs: Mapping[str, Mapping[str, Any]],
    tolerance: float = 0.0,
) -> CheckResult:
    """SUM/MIN/MAX/COUNT DISTINCT per numeric column.

    Independent of the hash path. If hashes match but a SUM does not, the
    canonicalization is lying to you -- that is the real value of running both.
    """
    findings: list[Finding] = []
    rows: list[dict[str, Any]] = []

    for col, s_stats in source_aggs.items():
        t_stats = target_aggs.get(col)
        if t_stats is None:
            findings.append(
                Finding("aggregates", Severity.MEDIUM,
                        f"Column '{col}' has no target aggregate to compare",
                        "Column missing from target aggregate set.")
            )
            continue

        for stat, s_val in s_stats.items():
            t_val = t_stats.get(stat)
            rows.append({"column": col, "stat": stat, "source": s_val, "target": t_val})
            if s_val is None and t_val is None:
                continue
            if s_val is None or t_val is None:
                findings.append(
                    Finding("aggregates", Severity.HIGH,
                            f"{col}.{stat}: one side is NULL (source={s_val}, target={t_val})",
                            "An aggregate resolved to NULL on one side only.")
                )
                continue
            try:
                diff = abs(float(s_val) - float(t_val))
            except (TypeError, ValueError):
                if s_val != t_val:
                    findings.append(
                        Finding("aggregates", Severity.HIGH,
                                f"{col}.{stat} differs: {s_val!r} vs {t_val!r}",
                                "Non-numeric aggregate mismatch.")
                    )
                continue
            if diff > tolerance:
                findings.append(
                    Finding(
                        "aggregates",
                        Severity.HIGH,
                        f"{col}.{stat} differs by {diff:g} (source={s_val}, target={t_val})",
                        "Aggregate divergence. If row hashes matched but this did "
                        "not, suspect the canonicalization rules before the data.",
                    )
                )

    status = CheckStatus.FAIL if findings else CheckStatus.PASS
    return CheckResult("aggregates", status, findings=findings, metrics={"comparisons": rows})


# ---------------------------------------------------------------------------
# 8. Schema / type agreement
# ---------------------------------------------------------------------------

def check_schema_types(spec: MappingSpec) -> CheckResult:
    """Logical type disagreements between mapped columns.

    Not automatically a defect -- widening INT to NUMBER is routine -- but it is
    where silent truncation lives, so it is always surfaced.
    """
    findings: list[Finding] = []
    for m in spec.type_mismatches():
        findings.append(
            Finding(
                "schema_types",
                Severity.MEDIUM,
                f"'{m.source.name}' is {m.source.logical_type.value} in source, "
                f"'{m.target.name}' is {m.target.logical_type.value} in target",
                f"Native types: {m.source.native_type} -> {m.target.native_type}. "
                "Confirm the conversion is lossless, especially for DECIMAL scale "
                "and string length.",
            )
        )
    for m in spec.key_columns + spec.compare_columns:
        if not m.source.nullable and m.target.nullable:
            findings.append(
                Finding("schema_types", Severity.LOW,
                        f"'{m.target.name}' is nullable in target but NOT NULL in source",
                        "Nullability was relaxed. Not data loss, but the target no "
                        "longer enforces what the source did.")
            )
        elif m.source.nullable and not m.target.nullable:
            findings.append(
                Finding("schema_types", Severity.HIGH,
                        f"'{m.target.name}' is NOT NULL in target but nullable in source",
                        "Nullability was tightened. Any source NULL must have been "
                        "substituted or rejected. Cross-check null_handling.")
            )

    status = CheckStatus.FAIL if findings else CheckStatus.PASS
    return CheckResult("schema_types", status, findings=findings,
                       metrics={"mappings_checked": len(spec.key_columns) + len(spec.compare_columns)})
