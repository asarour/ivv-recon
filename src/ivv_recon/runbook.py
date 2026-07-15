"""Migration runbook generator.

The runbook is the thing an auditor asks for and nobody has written. It is
generated from the same spec and results as the report so it cannot drift from
what was actually validated.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .findings import ReconReport, Severity
from .schema import MappingSpec


def render_runbook(spec: MappingSpec, report: ReconReport | None = None) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    keys = ", ".join(f"`{m.source.name}`" for m in spec.key_columns)

    lines: list[str] = [
        f"# Migration Runbook: {spec.source_table} to {spec.target_table}",
        "",
        f"Generated {now} by ivv-recon.",
        "",
        "## 1. Scope",
        "",
        f"| | |",
        f"|---|---|",
        f"| Source | `{spec.source_table}` ({spec.source_table.dialect}) |",
        f"| Target | `{spec.target_table}` ({spec.target_table.dialect}) |",
        f"| Business key | {keys} |",
        f"| Columns compared | {len(spec.compare_columns)} |",
        f"| Columns excluded | {len(spec.excluded)} |",
        "",
    ]

    if spec.excluded:
        lines += [
            "### Excluded columns",
            "",
            "Excluded columns are **not validated**. Each needs a documented reason,",
            "because an excluded column is a hole in the assurance.",
            "",
        ]
        lines += [f"- `{c}`" for c in spec.excluded]
        lines.append("")

    lines += [
        "## 2. Source-to-target mapping",
        "",
        "| Source column | Source type | Target column | Target type | Role | Note |",
        "|---|---|---|---|---|---|",
    ]
    for m in spec.key_columns:
        lines.append(
            f"| `{m.source.name}` | {m.source.native_type} | `{m.target.name}` | "
            f"{m.target.native_type} | KEY | {m.transform_note or ''} |"
        )
    for m in spec.compare_columns:
        lines.append(
            f"| `{m.source.name}` | {m.source.native_type} | `{m.target.name}` | "
            f"{m.target.native_type} | compare | {m.transform_note or ''} |"
        )

    mismatches = spec.type_mismatches()
    if mismatches:
        lines += [
            "",
            "### Type conversions requiring sign-off",
            "",
            "Each of these changes logical type between source and target. Confirm",
            "the conversion is lossless before go-live.",
            "",
        ]
        for m in mismatches:
            lines.append(
                f"- `{m.source.name}` {m.source.logical_type.value} "
                f"({m.source.native_type}) to `{m.target.name}` "
                f"{m.target.logical_type.value} ({m.target.native_type})"
            )

    lines += [
        "",
        "## 3. Validation procedure",
        "",
        "Run in this order. Each step gates the next.",
        "",
        "1. **Schema check** - confirm types and nullability, resolve conversions above.",
        "2. **Row counts** - source vs target. Any shortfall is data loss; stop.",
        "3. **Key integrity** - keys unique and non-NULL on both sides. If this fails,",
        "   the key declaration is wrong and every later result is noise. Stop.",
        "4. **Key set comparison** - the EXCEPT/MINUS in both directions.",
        "5. **Row hash comparison** - content of rows present on both sides.",
        "6. **Field-level drilldown** - attribute mismatches to columns.",
        "7. **NULL handling** - per-column NULL rate drift.",
        "8. **Aggregates** - SUM/MIN/MAX/COUNT DISTINCT as an independent cross-check",
        "   of the hash path.",
        "",
        "```bash",
        "ivv-recon run --config mapping.yml --html report.html --json report.json",
        "```",
        "",
        "Exit codes: `0` pass, `1` findings at or above threshold, `2` a check errored",
        "(inconclusive - never treat as a pass).",
        "",
        "## 4. Mock migration runs",
        "",
        "Run the full validation against a non-production target at least three times",
        "before cutover:",
        "",
        "| Run | Purpose | Exit criteria |",
        "|---|---|---|",
        "| Mock 1 | Shake out mapping and canonicalization defects | Report generates cleanly |",
        "| Mock 2 | Validate fixes from Mock 1 | No CRITICAL findings |",
        "| Mock 3 | Dress rehearsal at production volume | PASS, timing recorded |",
        "",
        "Record wall-clock duration of each. That number is your cutover window.",
        "",
        "## 5. Cutover",
        "",
        "1. Freeze writes to source. Record the freeze timestamp.",
        "2. Final delta extract and load.",
        "3. Run validation. **Do not proceed on anything other than exit 0.**",
        "4. Archive `report.html` and `report.json` to the migration record.",
        "5. Obtain written sign-off from the data owner.",
        "6. Release writes to target.",
        "",
        "## 6. Rollback",
        "",
        "Triggers - roll back on any of:",
        "",
        "- Validation exits non-zero at cutover.",
        "- Any CRITICAL finding not accepted in writing by the data owner.",
        "- Cutover window exceeded by more than 50% of the Mock 3 timing.",
        "",
        "Procedure:",
        "",
        "1. Halt the load. Do not attempt a partial fix under time pressure.",
        "2. Keep writes frozen on source; source remains system of record.",
        "3. Truncate or restore the target to its pre-load state.",
        "4. Retain the failing report as the rollback justification.",
        "5. Release the freeze on source only.",
        "",
        "## 7. Sign-off",
        "",
        "| Role | Name | Date | Signature |",
        "|---|---|---|---|",
        "| Data owner | | | |",
        "| Migration lead | | | |",
        "| IV&V / validation | | | |",
        "",
    ]

    if report is not None:
        counts = report.counts_by_severity()
        lines += [
            "## 8. Last validation result",
            "",
            f"- Run: {report.started_at.strftime('%Y-%m-%d %H:%M UTC')}",
            f"- Verdict: **{'PASS' if report.passed() else 'FAIL'}**",
            f"- Critical: {counts['critical']}, High: {counts['high']}, "
            f"Medium: {counts['medium']}, Low: {counts['low']}",
            "",
        ]
        blocking = [
            f for f in report.all_findings
            if f.severity.rank <= Severity.HIGH.rank
        ]
        if blocking:
            lines += ["### Blocking findings", ""]
            lines += [f"- **{f.severity.value.upper()}** - {f.summary}" for f in blocking]
            lines.append("")

    return "\n".join(lines)


def write_runbook(spec: MappingSpec, path: str | Path,
                  report: ReconReport | None = None) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_runbook(spec, report), encoding="utf-8")
    return p
