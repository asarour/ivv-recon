"""HTML reconciliation report and JSON export.

Self-contained HTML: no CDN, no external CSS. An audit artifact has to still
render in five years on a machine with no internet, attached to a ticket.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from .findings import CheckStatus, ReconReport, Severity

_SEV_COLOR = {
    Severity.CRITICAL: "#c0392b",
    Severity.HIGH: "#e67e22",
    Severity.MEDIUM: "#b9770e",
    Severity.LOW: "#5d6d7e",
    Severity.INFO: "#7f8c8d",
}

_CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
       margin: 0; padding: 32px; background: #f4f6f7; color: #1c2833; line-height: 1.5; }
.wrap { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 32px 0 12px; padding-bottom: 6px; border-bottom: 2px solid #d5dbdb; }
.sub { color: #566573; font-size: 13px; margin-bottom: 20px; }
.verdict { padding: 16px 20px; border-radius: 4px; color: #fff; font-size: 18px;
           font-weight: 600; margin: 20px 0; }
.pass { background: #1e8449; }
.fail { background: #c0392b; }
.cards { display: flex; gap: 12px; flex-wrap: wrap; margin: 16px 0; }
.card { background: #fff; border: 1px solid #d5dbdb; border-radius: 4px;
        padding: 12px 16px; min-width: 120px; }
.card .n { font-size: 24px; font-weight: 700; }
.card .l { font-size: 11px; text-transform: uppercase; color: #566573; letter-spacing: .5px; }
table { border-collapse: collapse; width: 100%; background: #fff; font-size: 13px; }
th, td { border: 1px solid #d5dbdb; padding: 7px 10px; text-align: left; vertical-align: top; }
th { background: #eaeded; font-weight: 600; }
.finding { background: #fff; border: 1px solid #d5dbdb; border-left: 5px solid #ccc;
           border-radius: 3px; padding: 12px 16px; margin-bottom: 10px; }
.finding .hdr { display: flex; align-items: center; gap: 10px; }
.badge { color: #fff; font-size: 10px; font-weight: 700; padding: 2px 7px;
         border-radius: 3px; text-transform: uppercase; letter-spacing: .5px; }
.finding .sum { font-weight: 600; }
.finding .det { color: #566573; font-size: 13px; margin: 6px 0 0; }
.mono { font-family: SFMono-Regular, Consolas, monospace; font-size: 12px; }
.status-pass { color: #1e8449; font-weight: 600; }
.status-fail { color: #c0392b; font-weight: 600; }
.status-error { color: #6c3483; font-weight: 700; }
.status-skipped { color: #7f8c8d; font-style: italic; }
details { margin-top: 8px; }
summary { cursor: pointer; font-size: 12px; color: #2874a6; }
footer { margin-top: 40px; color: #85929e; font-size: 11px; text-align: center; }
"""


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _samples_table(samples: list[dict[str, Any]]) -> str:
    if not samples:
        return ""
    cols = list(samples[0].keys())
    head = "".join(f"<th>{_e(c)}</th>" for c in cols)
    body = "".join(
        "<tr>" + "".join(f'<td class="mono">{_e(r.get(c))}</td>' for c in cols) + "</tr>"
        for r in samples
    )
    return f"<table><tr>{head}</tr>{body}</table>"


def render_html(report: ReconReport) -> str:
    counts = report.counts_by_severity()
    passed = report.passed()

    cards = "".join(
        f'<div class="card"><div class="n" style="color:{_SEV_COLOR[s]}">{counts[s.value]}</div>'
        f'<div class="l">{s.value}</div></div>'
        for s in Severity
        if counts[s.value] or s in (Severity.CRITICAL, Severity.HIGH)
    )

    rows = "".join(
        f"<tr><td>{_e(r.name)}</td>"
        f'<td class="status-{r.status.value}">{r.status.value.upper()}</td>'
        f"<td>{len(r.findings)}</td>"
        f"<td>{r.duration_ms:.0f} ms</td>"
        f'<td class="mono">{_e(r.error or "")}</td></tr>'
        for r in report.results
    )

    findings_html = []
    for f in report.all_findings:
        color = _SEV_COLOR[f.severity]
        affected = (
            f'<div class="det">Rows affected: <b>{f.total_affected:,}</b></div>'
            if f.total_affected is not None
            else ""
        )
        samples = ""
        if f.samples:
            trunc = (
                f"<p class='det'>Showing first {len(f.samples)} of {f.total_affected:,}.</p>"
                if f.sample_truncated and f.total_affected
                else ""
            )
            samples = (
                f"<details><summary>Sample rows ({len(f.samples)})</summary>"
                f"{trunc}{_samples_table(f.samples)}</details>"
            )
        findings_html.append(
            f'<div class="finding" style="border-left-color:{color}">'
            f'<div class="hdr"><span class="badge" style="background:{color}">'
            f'{f.severity.value}</span><span class="sum">{_e(f.summary)}</span></div>'
            f'<p class="det">{_e(f.detail)}</p>{affected}{samples}</div>'
        )

    if not findings_html:
        findings_html.append(
            '<div class="finding" style="border-left-color:#1e8449">'
            '<div class="sum">No findings. Source and target reconcile.</div></div>'
        )

    errored = ""
    if report.errored:
        names = ", ".join(_e(r.name) for r in report.errored)
        errored = (
            f'<div class="verdict fail">CHECKS ERRORED: {names}. '
            "The run is inconclusive, not clean.</div>"
        )

    duration = ""
    if report.finished_at:
        duration = f"{(report.finished_at - report.started_at).total_seconds():.1f}s"

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Reconciliation Report - {_e(report.source_table)}</title>
<style>{_CSS}</style></head><body><div class="wrap">
<h1>Source-to-Target Reconciliation Report</h1>
<div class="sub">
  <b>{_e(report.source_table)}</b> ({_e(report.source_dialect)})
  &rarr; <b>{_e(report.target_table)}</b> ({_e(report.target_dialect)})<br>
  Started {_e(report.started_at.strftime('%Y-%m-%d %H:%M:%S UTC'))}
  {'&middot; Duration ' + duration if duration else ''}
  &middot; Fail threshold: {_e(report.fail_threshold.value)}
</div>
{errored}
<div class="verdict {'pass' if passed else 'fail'}">
  {'PASS &mdash; target reconciles to source' if passed else 'FAIL &mdash; discrepancies require resolution before sign-off'}
</div>
<div class="cards">{cards}</div>
<h2>Checks</h2>
<table><tr><th>Check</th><th>Status</th><th>Findings</th><th>Duration</th><th>Error</th></tr>
{rows}</table>
<h2>Findings</h2>
{''.join(findings_html)}
<footer>Generated by ivv-recon &middot; This report is an audit artifact. Retain with the migration record.</footer>
</div></body></html>"""


def write_html(report: ReconReport, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_html(report), encoding="utf-8")
    return p


def write_json(report: ReconReport, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report.as_dict(), indent=2, default=str), encoding="utf-8")
    return p
