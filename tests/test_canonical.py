"""Canonicalization tests.

These are the tests that matter. Every case below is a real cross-platform
gotcha; if one of these regresses, the tool reports clean data as broken or,
far worse, broken data as clean.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from ivv_recon.canonical import canonical_row, canonical_value, row_hash
from ivv_recon.dialects import NULL_SENTINEL, get_dialect
from ivv_recon.schema import Column, LogicalType


class TestStringCanonicalization:
    def test_char_padding_is_stripped(self):
        # SQL Server CHAR(20) pads. Snowflake VARCHAR does not. Same value.
        assert canonical_value("Nakamura           ", LogicalType.STRING) == "Nakamura"
        assert canonical_value("Nakamura", LogicalType.STRING) == "Nakamura"

    def test_leading_whitespace_is_preserved(self):
        # Leading space is data, not padding. Stripping it would destroy values.
        assert canonical_value("  Smith", LogicalType.STRING) == "  Smith"

    def test_empty_string_is_not_null(self):
        # The distinction that catches NULL-substitution defects.
        assert canonical_value("", LogicalType.STRING) == ""
        assert canonical_value(None, LogicalType.STRING) == NULL_SENTINEL
        assert canonical_value("", LogicalType.STRING) != canonical_value(None, LogicalType.STRING)


class TestNumericCanonicalization:
    def test_trailing_zeros_do_not_matter(self):
        # DECIMAL(18,2) '100.50' vs NUMBER(38,10) '100.5000000000'
        assert canonical_value(Decimal("100.5"), LogicalType.DECIMAL) == \
               canonical_value(Decimal("100.50"), LogicalType.DECIMAL)
        assert canonical_value(Decimal("100.5000000000"), LogicalType.DECIMAL) == \
               canonical_value(Decimal("100.5"), LogicalType.DECIMAL)

    def test_scale_is_fixed(self):
        assert canonical_value(Decimal("1.5"), LogicalType.DECIMAL) == "1.5000000000"

    def test_int_and_decimal_of_same_value_agree_as_text(self):
        assert canonical_value(42, LogicalType.INTEGER) == "42"
        assert canonical_value(Decimal("42.00"), LogicalType.DECIMAL) == "42.0000000000"

    def test_genuine_difference_still_differs(self):
        assert canonical_value(Decimal("9100.00"), LogicalType.DECIMAL) != \
               canonical_value(Decimal("9100.99"), LogicalType.DECIMAL)

    def test_non_finite_is_rejected_not_guessed(self):
        with pytest.raises(ValueError):
            canonical_value(float("nan"), LogicalType.FLOAT)
        with pytest.raises(ValueError):
            canonical_value(float("inf"), LogicalType.FLOAT)


class TestBooleanCanonicalization:
    def test_bit_and_boolean_agree(self):
        # SQL Server BIT 1 vs Snowflake BOOLEAN True
        assert canonical_value(1, LogicalType.BOOLEAN) == canonical_value(True, LogicalType.BOOLEAN)
        assert canonical_value(0, LogicalType.BOOLEAN) == canonical_value(False, LogicalType.BOOLEAN)

    def test_string_forms(self):
        assert canonical_value("true", LogicalType.BOOLEAN) == "1"
        assert canonical_value("N", LogicalType.BOOLEAN) == "0"

    def test_garbage_rejected(self):
        with pytest.raises(ValueError):
            canonical_value("maybe", LogicalType.BOOLEAN)


class TestTimestampCanonicalization:
    def test_sub_millisecond_truncated_not_rounded(self):
        # SQL Server DATETIME cannot hold microseconds. Truncate to ms so the two
        # sides agree. Rounding could roll a value into the next second.
        a = dt.datetime(2024, 3, 14, 9, 26, 53, 123000)
        b = dt.datetime(2024, 3, 14, 9, 26, 53, 123999)
        assert canonical_value(a, LogicalType.TIMESTAMP) == canonical_value(b, LogicalType.TIMESTAMP)
        assert canonical_value(a, LogicalType.TIMESTAMP) == "2024-03-14 09:26:53.123"

    def test_rounding_would_cross_a_second_boundary(self):
        # 999_600us rounds to 1.000s (wrong second). Truncation keeps .999.
        v = dt.datetime(2024, 3, 14, 9, 26, 53, 999600)
        assert canonical_value(v, LogicalType.TIMESTAMP) == "2024-03-14 09:26:53.999"

    def test_genuine_ms_difference_still_differs(self):
        a = dt.datetime(2024, 3, 14, 9, 26, 53, 123000)
        b = dt.datetime(2024, 3, 14, 9, 26, 53, 456000)
        assert canonical_value(a, LogicalType.TIMESTAMP) != canonical_value(b, LogicalType.TIMESTAMP)

    def test_date_only(self):
        assert canonical_value(dt.date(2024, 3, 14), LogicalType.DATE) == "2024-03-14"


class TestRowHashing:
    def _cols(self):
        return [
            Column("A", LogicalType.STRING),
            Column("B", LogicalType.STRING),
        ]

    def test_separator_prevents_field_shifting(self):
        # Without a separator, ('ab','c') and ('a','bc') would hash identically.
        cols = self._cols()
        assert row_hash(["ab", "c"], cols) != row_hash(["a", "bc"], cols)

    def test_null_does_not_collapse_the_row(self):
        # The nasty one: in SQL, NULL || 'x' = NULL. Two rows both collapse to
        # NULL and compare equal. The sentinel prevents that.
        cols = self._cols()
        assert row_hash([None, "x"], cols) != row_hash([None, "y"], cols)
        assert row_hash([None, "x"], cols) != row_hash(["x", None], cols)

    def test_null_sentinel_distinct_from_literal_text(self):
        cols = self._cols()
        assert row_hash([None, "x"], cols) != row_hash(["", "x"], cols)

    def test_hash_is_stable(self):
        cols = self._cols()
        assert row_hash(["a", "b"], cols) == row_hash(["a", "b"], cols)
        assert len(row_hash(["a", "b"], cols)) == 64

    def test_length_mismatch_is_an_error(self):
        with pytest.raises(ValueError):
            canonical_row(["only-one"], self._cols())


class TestCrossPlatformEquivalence:
    """The whole point: SQL Server row X and Snowflake row X hash the same."""

    def test_sqlserver_and_snowflake_rows_agree(self):
        src_cols = [
            Column.from_native("SURNAME", "CHAR(20)"),
            Column.from_native("BALANCE", "DECIMAL(18,2)"),
            Column.from_native("IS_ACTIVE", "BIT"),
            Column.from_native("OPENED_AT", "DATETIME"),
        ]
        tgt_cols = [
            Column.from_native("SURNAME", "VARCHAR(20)"),
            Column.from_native("BALANCE", "NUMBER(38,10)"),
            Column.from_native("IS_ACTIVE", "BOOLEAN"),
            Column.from_native("OPENED_AT", "TIMESTAMP_NTZ(9)"),
        ]
        src_row = ["Nakamura           ", Decimal("18400.75"), 1,
                   dt.datetime(2024, 3, 14, 9, 26, 53, 123000)]
        tgt_row = ["Nakamura", Decimal("18400.7500000000"), True,
                   dt.datetime(2024, 3, 14, 9, 26, 53, 123456)]
        assert row_hash(src_row, src_cols) == row_hash(tgt_row, tgt_cols)

    def test_real_difference_survives_canonicalization(self):
        src_cols = [Column.from_native("BALANCE", "DECIMAL(18,2)")]
        tgt_cols = [Column.from_native("BALANCE", "NUMBER(38,10)")]
        assert row_hash([Decimal("9100.00")], src_cols) != \
               row_hash([Decimal("9100.99")], tgt_cols)


class TestDialectSQL:
    def test_hex_case_is_folded(self):
        col = [Column.from_native("X", "VARCHAR(10)")]
        ts = get_dialect("sqlserver").row_hash_expr(col)
        sf = get_dialect("snowflake").row_hash_expr(col)
        # T-SQL CONVERT(...,2) is uppercase; must be LOWER()ed to match SHA2().
        assert "LOWER(" in ts
        assert "SHA2(" in sf

    def test_null_sentinel_present_in_generated_sql(self):
        col = Column.from_native("X", "VARCHAR(10)")
        for name in ("sqlserver", "snowflake", "synapse", "azuresql"):
            assert NULL_SENTINEL in get_dialect(name).canonical_expr(col)

    def test_synapse_refuses_pushdown(self):
        # Synapse caps HASHBYTES at 8000 bytes -> would produce false matches.
        assert get_dialect("synapse").supports_pushdown_hash() is False
        assert get_dialect("sqlserver").supports_pushdown_hash() is True

    def test_identifier_quoting_is_injection_safe(self):
        assert get_dialect("sqlserver").quote_ident("a]b") == "[a]]b]"
        assert get_dialect("snowflake").quote_ident('a"b') == '"a""b"'

    def test_unknown_dialect_raises(self):
        with pytest.raises(KeyError):
            get_dialect("oracle")
