"""End-to-end test over the seeded demo dataset.

This is the acceptance test: every seeded defect must be found, and every
seeded false-positive trap must NOT be reported.
"""

from __future__ import annotations

from ivv_recon.demo import build_connectors, build_spec
from ivv_recon.engine import ReconEngine
from ivv_recon.findings import CheckStatus, Severity
from ivv_recon.report import render_html
from ivv_recon.runbook import render_runbook


def _run():
    spec = build_spec()
    s, t = build_connectors()
    return spec, ReconEngine(s, t, spec).run()


class TestDemoEndToEnd:
    def test_run_completes_and_fails(self):
        _, report = _run()
        assert report.passed() is False
        assert report.exit_code() == 1

    def test_no_check_errored(self):
        _, report = _run()
        assert report.errored == [], [r.error for r in report.errored]

    def test_detects_missing_row_1004(self):
        _, report = _run()
        r = next(x for x in report.results if x.name == "key_sets")
        assert r.metrics["missing_in_target"] == 1
        f = next(f for f in r.findings if "missing from target" in f.summary)
        assert f.severity is Severity.CRITICAL
        assert ["1004"] in [s["key"] for s in f.samples]

    def test_detects_phantom_row_9999(self):
        _, report = _run()
        r = next(x for x in report.results if x.name == "key_sets")
        assert r.metrics["extra_in_target"] == 1

    def test_detects_null_substitution_on_email(self):
        _, report = _run()
        r = next(x for x in report.results if x.name == "null_handling")
        f = next(f for f in r.findings if "EMAIL" in f.summary)
        assert f.severity is Severity.HIGH
        assert "zero in target" in f.summary

    def test_detects_wrong_balance_on_1006(self):
        _, report = _run()
        r = next(x for x in report.results if x.name == "row_hashes")
        assert r.metrics["rows_mismatched"] >= 1

    def test_field_level_blames_the_right_columns(self):
        _, report = _run()
        r = next(x for x in report.results if x.name == "field_level")
        implicated = r.metrics["per_column_counts"]
        # 1006 BALANCE is wrong; 1005 EMAIL was substituted. Nothing else.
        assert "BALANCE" in implicated
        assert "SURNAME" not in implicated, "CHAR padding must not be reported"
        assert "OPENED_AT" not in implicated, "sub-ms precision must not be reported"
        assert "IS_ACTIVE" not in implicated, "BIT vs BOOLEAN must not be reported"

    def test_padding_and_precision_are_not_false_positives(self):
        """The traps. 1002 (CHAR padding), 1003 (100.5 vs 100.50), 1007 (ms).

        These rows are identical in substance. If any is reported, the
        canonicalization is broken and the tool is worse than useless -- it
        would send an engineer chasing ghosts on every migration.
        """
        _, report = _run()
        r = next(x for x in report.results if x.name == "row_hashes")
        f = next((f for f in r.findings if f.samples), None)
        reported = {s["key"][0] for s in f.samples} if f else set()
        assert "1002" not in reported, "CHAR(20) padding reported as a mismatch"
        assert "1003" not in reported, "100.5 vs 100.50 reported as a mismatch"
        assert "1007" not in reported, "sub-millisecond precision reported as a mismatch"

    def test_only_genuine_mismatches_reported(self):
        _, report = _run()
        r = next(x for x in report.results if x.name == "row_hashes")
        # 1005 (EMAIL NULL->'') and 1006 (BALANCE wrong). Exactly two.
        assert r.metrics["rows_mismatched"] == 2

    def test_row_count_delta_is_zero_but_data_is_still_wrong(self):
        """The reason count checks alone are not assurance.

        7 source rows, 7 target rows. Counts reconcile perfectly. One row is
        missing and a phantom took its place. Anyone signing off on a count
        match here would be shipping data loss.
        """
        _, report = _run()
        r = next(x for x in report.results if x.name == "row_counts")
        assert r.metrics["delta"] == 0
        assert r.status is CheckStatus.PASS
        assert report.passed() is False


class TestArtifacts:
    def test_html_renders_and_is_self_contained(self):
        _, report = _run()
        html = render_html(report)
        assert html.startswith("<!DOCTYPE html>")
        assert "FAIL" in html
        # No external fetches -- must render offline in five years.
        assert "http://" not in html and "https://" not in html
        assert "<script" not in html.lower()

    def test_runbook_renders_with_required_sections(self):
        spec, report = _run()
        md = render_runbook(spec, report)
        for section in ("Scope", "Source-to-target mapping", "Validation procedure",
                        "Mock migration runs", "Cutover", "Rollback", "Sign-off"):
            assert section in md
        assert "CUSTOMER_ID" in md
