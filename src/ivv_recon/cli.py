"""Command line interface."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .config import load_spec
from .engine import ReconEngine
from .findings import Severity
from .report import write_html, write_json
from .runbook import write_runbook


def _log(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _summary(report) -> None:
    c = report.counts_by_severity()
    print()
    print(f"  {report.source_table}  ->  {report.target_table}")
    print("  " + "-" * 60)
    for r in report.results:
        mark = {"pass": "PASS", "fail": "FAIL", "error": "ERR ", "skipped": "SKIP"}[r.status.value]
        print(f"  [{mark}] {r.name:<24} {len(r.findings):>3} finding(s)  {r.duration_ms:>7.0f} ms")
    print("  " + "-" * 60)
    print(f"  critical={c['critical']}  high={c['high']}  medium={c['medium']}  low={c['low']}")
    print(f"  VERDICT: {'PASS' if report.passed() else 'FAIL'}")
    if report.errored:
        print("  NOTE: one or more checks errored. Result is inconclusive, not clean.")
    print()
    for f in report.all_findings[:10]:
        print(f"  {f.severity.value.upper():>8}  {f.summary}")
    if len(report.all_findings) > 10:
        print(f"  ... and {len(report.all_findings) - 10} more (see the HTML report)")
    print()


def cmd_run(args: argparse.Namespace) -> int:

    # Real connections are built by the operator and injected. This CLI path
    # deliberately does not read credentials -- wire your own connection factory
    # here, or import ReconEngine and drive it from your own code.
    print(
        "ERROR: 'run' needs live connections.\n"
        "  This build ships the engine, not a credential store. Either:\n"
        "    - import ivv_recon.engine.ReconEngine and pass your own DBAPI connections, or\n"
        "    - try 'ivv-recon demo' to see the full output with no database.",
        file=sys.stderr,
    )
    return 2


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import build_connectors, build_spec

    spec = build_spec()
    source, target = build_connectors()
    engine = ReconEngine(source, target, spec,
                         fail_threshold=Severity(args.fail_threshold))
    report = engine.run()
    _summary(report)

    out = Path(args.out)
    html = write_html(report, out / "report.html")
    js = write_json(report, out / "report.json")
    rb = write_runbook(spec, out / "runbook.md", report)
    print(f"  HTML report : {html}")
    print(f"  JSON report : {js}")
    print(f"  Runbook     : {rb}")
    print()
    return report.exit_code()


def cmd_runbook(args: argparse.Namespace) -> int:
    spec = load_spec(args.config)
    p = write_runbook(spec, args.out)
    print(f"runbook written: {p}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Parse and validate a mapping file without touching a database."""
    spec = load_spec(args.config)
    print(f"OK: {spec.source_table} -> {spec.target_table}")
    print(f"    {len(spec.key_columns)} key column(s), {len(spec.compare_columns)} compared")
    mm = spec.type_mismatches()
    if mm:
        print(f"    {len(mm)} type conversion(s) needing review:")
        for m in mm:
            print(f"      {m.source.name}: {m.source.logical_type.value} -> {m.target.logical_type.value}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ivv-recon",
        description="Source-to-target reconciliation for ETL migrations.",
    )
    p.add_argument("--version", action="version", version=f"ivv-recon {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="reconcile using a mapping config")
    r.add_argument("--config", required=True)
    r.add_argument("--html", default="report.html")
    r.add_argument("--json", dest="json_out", default="report.json")
    r.set_defaults(func=cmd_run)

    d = sub.add_parser("demo", help="run against the seeded in-memory dataset, no DB needed")
    d.add_argument("--out", default="./out")
    d.add_argument("--fail-threshold", default="high",
                   choices=[s.value for s in Severity])
    d.set_defaults(func=cmd_demo)

    b = sub.add_parser("runbook", help="generate a migration runbook from a mapping")
    b.add_argument("--config", required=True)
    b.add_argument("--out", default="runbook.md")
    b.set_defaults(func=cmd_runbook)

    v = sub.add_parser("validate", help="check a mapping file for errors")
    v.add_argument("--config", required=True)
    v.set_defaults(func=cmd_validate)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _log(args.verbose)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
