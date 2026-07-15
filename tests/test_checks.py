"""Check-level tests. Pure functions, no database."""

from __future__ import annotations

import pytest

from ivv_recon.checks import (
    check_aggregates,
    check_key_integrity,
    check_key_sets,
    check_null_handling,
    check_row_counts,
    check_row_hashes,
    check_schema_types,
)
from ivv_recon.findings import CheckStatus, Severity
from ivv_recon.schema import (
    Column,
    ColumnMapping,
    LogicalType,
    MappingSpec,
    TableRef,
    map_native_type,
    unify_types,
)


class TestRowCounts:
    def test_equal_passes(self):
        assert check_row_counts(1000, 1000).status is CheckStatus.PASS

    def test_missing_rows_are_critical(self):
        r = check_row_counts(1000, 999)
        assert r.status is CheckStatus.FAIL
        assert r.findings[0].severity is Severity.CRITICAL

    def test_extra_rows_are_high_not_critical(self):
        # Surplus is usually a double-load: wrong, but recoverable.
        r = check_row_counts(1000, 1001)
        assert r.findings[0].severity is Severity.HIGH

    def test_zero_source_does_not_divide_by_zero(self):
        r = check_row_counts(0, 0)
        assert r.status is CheckStatus.PASS
        assert r.metrics["delta_pct"] is None


class TestKeyIntegrity:
    def test_unique_keys_pass(self):
        r = check_key_integrity("source", [("1",), ("2",), ("3",)])
        assert r.status is CheckStatus.PASS

    def test_duplicates_are_critical(self):
        r = check_key_integrity("source", [("1",), ("1",), ("2",)])
        assert r.status is CheckStatus.FAIL
        assert r.findings[0].severity is Severity.CRITICAL
        assert r.metrics["source_duplicate_keys"] == 1

    def test_null_key_flagged(self):
        r = check_key_integrity("target", [("1",), ("<<NULL>>",)])
        assert any(f.severity is Severity.HIGH for f in r.findings)


class TestKeySets:
    def test_identical_sets_pass(self):
        assert check_key_sets([("1",), ("2",)], [("1",), ("2",)]).status is CheckStatus.PASS

    def test_missing_in_target_is_critical(self):
        r = check_key_sets([("1",), ("2",)], [("1",)])
        f = next(f for f in r.findings if "missing from target" in f.summary)
        assert f.severity is Severity.CRITICAL
        assert f.total_affected == 1

    def test_extra_in_target_is_high(self):
        r = check_key_sets([("1",)], [("1",), ("9",)])
        f = next(f for f in r.findings if "not in source" in f.summary)
        assert f.severity is Severity.HIGH


class TestRowHashes:
    def test_matching_passes(self):
        s = {("1",): "aaa", ("2",): "bbb"}
        assert check_row_hashes(s, dict(s)).status is CheckStatus.PASS

    def test_mismatch_is_critical(self):
        r = check_row_hashes({("1",): "aaa"}, {("1",): "zzz"})
        assert r.findings[0].severity is Severity.CRITICAL

    def test_only_intersection_compared(self):
        # Key 2 exists only in source -- that is the set check's job, not ours.
        r = check_row_hashes({("1",): "a", ("2",): "b"}, {("1",): "a"})
        assert r.status is CheckStatus.PASS
        assert r.metrics["rows_compared"] == 1


class TestNullHandling:
    def _m(self, name="EMAIL"):
        c = Column.from_native(name, "VARCHAR(100)")
        return [ColumnMapping(c, c)]

    def test_equal_null_counts_pass(self):
        r = check_null_handling({"EMAIL": 5}, {"EMAIL": 5}, 100, 100, self._m())
        assert r.status is CheckStatus.PASS

    def test_nulls_vanishing_is_flagged(self):
        # The silent killer: NULL -> '' via a DEFAULT on a NOT NULL column.
        r = check_null_handling({"EMAIL": 5}, {"EMAIL": 0}, 100, 100, self._m())
        assert r.status is CheckStatus.FAIL
        assert r.findings[0].severity is Severity.HIGH
        assert "zero in target" in r.findings[0].summary

    def test_nulls_appearing_is_flagged(self):
        r = check_null_handling({"EMAIL": 1}, {"EMAIL": 9}, 100, 100, self._m())
        assert r.findings[0].severity is Severity.HIGH


class TestAggregates:
    def test_equal_passes(self):
        a = {"BALANCE": {"sum": 100, "min": 1, "max": 99}}
        assert check_aggregates(a, dict(a)).status is CheckStatus.PASS

    def test_sum_drift_flagged(self):
        r = check_aggregates({"BALANCE": {"sum": 100}}, {"BALANCE": {"sum": 101}})
        assert r.status is CheckStatus.FAIL

    def test_tolerance_respected(self):
        r = check_aggregates({"B": {"sum": 100.0}}, {"B": {"sum": 100.005}}, tolerance=0.01)
        assert r.status is CheckStatus.PASS

    def test_one_sided_null_flagged(self):
        r = check_aggregates({"B": {"sum": 5}}, {"B": {"sum": None}})
        assert r.findings[0].severity is Severity.HIGH


class TestSchemaTypes:
    def test_int_to_number_scale_zero_is_equivalent_not_a_finding(self):
        """NUMBER(38,0) is Snowflake's landing type for INT. Not a conversion."""
        s = Column.from_native("ID", "INT", nullable=False)
        t = Column.from_native("ID", "NUMBER(38,0)", nullable=False)
        c = Column.from_native("V", "VARCHAR(10)")
        spec = MappingSpec(
            TableRef("sqlserver", table="A"), TableRef("snowflake", table="B"),
            key_columns=[ColumnMapping(s, t)], compare_columns=[ColumnMapping(c, c)],
        )
        assert check_schema_types(spec).status is CheckStatus.PASS

    def test_genuine_widening_is_surfaced_but_not_critical(self):
        s = Column.from_native("ID", "INT", nullable=False)
        t = Column.from_native("ID", "NUMBER(38,2)", nullable=False)
        c = Column.from_native("V", "VARCHAR(10)")
        spec = MappingSpec(
            TableRef("sqlserver", table="A"), TableRef("snowflake", table="B"),
            key_columns=[ColumnMapping(s, t)], compare_columns=[ColumnMapping(c, c)],
        )
        r = check_schema_types(spec)
        # INTEGER -> DECIMAL with real scale: worth a look, not a blocker.
        assert r.findings
        assert all(f.severity in (Severity.MEDIUM, Severity.LOW) for f in r.findings)

    def test_tightened_nullability_is_high(self):
        s = Column.from_native("K", "INT", nullable=False)
        c_s = Column.from_native("V", "VARCHAR(10)", nullable=True)
        c_t = Column.from_native("V", "VARCHAR(10)", nullable=False)
        spec = MappingSpec(
            TableRef("sqlserver", table="A"), TableRef("snowflake", table="B"),
            key_columns=[ColumnMapping(s, s)], compare_columns=[ColumnMapping(c_s, c_t)],
        )
        r = check_schema_types(spec)
        assert any(f.severity is Severity.HIGH for f in r.findings)


class TestMappingValidation:
    def test_no_keys_rejected(self):
        c = Column.from_native("V", "VARCHAR(10)")
        spec = MappingSpec(TableRef("sqlserver", table="A"), TableRef("snowflake", table="B"),
                           key_columns=[], compare_columns=[ColumnMapping(c, c)])
        with pytest.raises(ValueError, match="no key columns"):
            spec.validate()

    def test_unmapped_native_type_raises(self):
        with pytest.raises(ValueError, match="unmapped native type"):
            Column.from_native("X", "GEOGRAPHY")


class TestTypeUnification:
    """Both sides must canonicalize under one rule or nothing ever matches."""

    def test_int_and_decimal_unify_to_decimal(self):
        assert unify_types(LogicalType.INTEGER, LogicalType.DECIMAL) is LogicalType.DECIMAL

    def test_bit_and_boolean_unify(self):
        assert unify_types(LogicalType.BOOLEAN, LogicalType.INTEGER) is LogicalType.BOOLEAN

    def test_identical_types_pass_through(self):
        assert unify_types(LogicalType.STRING, LogicalType.STRING) is LogicalType.STRING

    def test_incomparable_pair_raises_rather_than_coercing(self):
        # Silently stringifying a timestamp to compare it to a name would "work"
        # and be meaningless. Refuse instead.
        with pytest.raises(ValueError, match="no safe common representation"):
            unify_types(LogicalType.STRING, LogicalType.TIMESTAMP)

    def test_mapping_exposes_unified_columns(self):
        s = Column.from_native("ID", "INT")
        t = Column.from_native("ID", "NUMBER(38,2)")
        m = ColumnMapping(s, t)
        assert m.comparison_type is LogicalType.DECIMAL
        assert m.source_for_compare().logical_type is LogicalType.DECIMAL
        assert m.target_for_compare().logical_type is LogicalType.DECIMAL
        # Declared types are preserved for reporting.
        assert m.source.logical_type is LogicalType.INTEGER

    def test_incomparable_mapping_fails_at_validate_not_mid_run(self):
        k = Column.from_native("K", "INT", nullable=False)
        bad_s = Column.from_native("V", "VARCHAR(10)")
        bad_t = Column.from_native("V", "DATETIME")
        spec = MappingSpec(
            TableRef("sqlserver", table="A"), TableRef("snowflake", table="B"),
            key_columns=[ColumnMapping(k, k)], compare_columns=[ColumnMapping(bad_s, bad_t)],
        )
        with pytest.raises(ValueError, match="V"):
            spec.validate()


class TestNativeTypeParsing:
    def test_number_scale_zero_is_integer(self):
        assert map_native_type("NUMBER(38,0)") is LogicalType.INTEGER
        assert map_native_type("DECIMAL(18,0)") is LogicalType.INTEGER

    def test_number_with_scale_is_decimal(self):
        assert map_native_type("NUMBER(38,10)") is LogicalType.DECIMAL
        assert map_native_type("DECIMAL(18,2)") is LogicalType.DECIMAL

    def test_whitespace_and_case_tolerated(self):
        assert map_native_type("  Decimal( 18 , 2 )  ") is LogicalType.DECIMAL

    def test_unbalanced_parens_rejected(self):
        """The YAML flow-mapping trap: {type: NUMBER(38,0)} -> 'NUMBER(38'.

        Unguarded this maps to DECIMAL instead of INTEGER and no key matches --
        a total, silent failure arriving through the config file.
        """
        with pytest.raises(ValueError, match="unbalanced parentheses"):
            map_native_type("NUMBER(38")
