"""Tests for finance/tools.py — pure logic, sheets reads mocked."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from finance import tools


def _row(**kwargs):
    base = {
        "txn_hash": "h1",
        "date": "2026-05-10",
        "merchant": "SWIGGY,Bangalore",
        "amount": "-450.00",
        "category": "Food & Dining",
        "confidence": "",
        "source": "axis_card",
        "is_flagged": "FALSE",
        "is_duplicate": "FALSE",
        "notes": "",
    }
    base.update(kwargs)
    return base


SAMPLE_ROWS = [
    _row(merchant="SWIGGY,Bangalore", amount="-450", category="Food & Dining", date="2026-04-15"),
    _row(merchant="ZOMATO,Gurgaon",   amount="-300", category="Food & Dining", date="2026-04-18"),
    _row(merchant="ZEPTO,Bangalore",  amount="-1200", category="Groceries",     date="2026-04-20"),
    _row(merchant="ZEPTO,Bangalore",  amount="-800",  category="Groceries",     date="2026-05-02"),
    _row(merchant="AMAZON,Mumbai",    amount="-2500", category="Shopping",      date="2026-05-10"),
    _row(merchant="UBER INDIA",       amount="-180",  category="Fuel & Transport", date="2026-05-12"),
    _row(merchant="GOOGLEPLAY",       amount="50",    category="Income",        date="2026-05-15", is_flagged="TRUE"),
]


class TestQuerySheet:
    def test_no_filters_returns_all(self):
        with patch("finance.tools.sheets.read_tab", return_value=SAMPLE_ROWS):
            assert len(tools.query_sheet()) == len(SAMPLE_ROWS)
            assert len(tools.query_sheet({})) == len(SAMPLE_ROWS)

    def test_date_range_inclusive(self):
        with patch("finance.tools.sheets.read_tab", return_value=SAMPLE_ROWS):
            april = tools.query_sheet({"date_from": "2026-04-01", "date_to": "2026-04-30"})
            assert len(april) == 3
            for r in april:
                assert r["date"].startswith("2026-04")

    def test_category_match_case_insensitive(self):
        with patch("finance.tools.sheets.read_tab", return_value=SAMPLE_ROWS):
            assert len(tools.query_sheet({"category": "groceries"})) == 2
            assert len(tools.query_sheet({"category": "GROCERIES"})) == 2

    def test_merchant_contains_substring(self):
        with patch("finance.tools.sheets.read_tab", return_value=SAMPLE_ROWS):
            zepto = tools.query_sheet({"merchant_contains": "zepto"})
            assert len(zepto) == 2

    def test_filters_combine_as_AND(self):
        with patch("finance.tools.sheets.read_tab", return_value=SAMPLE_ROWS):
            april_food = tools.query_sheet({
                "category": "Food & Dining",
                "date_from": "2026-04-01",
                "date_to": "2026-04-30",
            })
            assert len(april_food) == 2

    def test_is_flagged_filter(self):
        with patch("finance.tools.sheets.read_tab", return_value=SAMPLE_ROWS):
            flagged = tools.query_sheet({"is_flagged": "TRUE"})
            assert len(flagged) == 1
            assert flagged[0]["merchant"] == "GOOGLEPLAY"


class TestAggregate:
    def test_sum_of_groceries(self):
        with patch("finance.tools.sheets.read_tab", return_value=SAMPLE_ROWS):
            rows = tools.query_sheet({"category": "Groceries"})
        result = tools.aggregate(rows, op="sum")
        assert result == {"value": -2000.0, "op": "sum"}

    def test_count(self):
        result = tools.aggregate(SAMPLE_ROWS, op="count")
        assert result == {"value": 7, "op": "count"}

    def test_avg(self):
        rows = [_row(amount="100"), _row(amount="200"), _row(amount="300")]
        result = tools.aggregate(rows, op="avg")
        assert result == {"value": 200.0, "op": "avg"}

    def test_min_max(self):
        rows = [_row(amount="-1000"), _row(amount="-100"), _row(amount="500")]
        assert tools.aggregate(rows, op="min")["value"] == -1000.0
        assert tools.aggregate(rows, op="max")["value"] == 500.0

    def test_group_by_category(self):
        result = tools.aggregate(SAMPLE_ROWS, op="sum", group_by="category")
        assert result["op"] == "sum"
        assert result["group_by"] == "category"
        groups = result["groups"]
        assert groups["Food & Dining"] == -750.0
        assert groups["Groceries"] == -2000.0
        assert groups["Shopping"] == -2500.0
        assert groups["Fuel & Transport"] == -180.0
        assert groups["Income"] == 50.0

    def test_group_by_with_count(self):
        result = tools.aggregate(SAMPLE_ROWS, op="count", group_by="category")
        assert result["groups"]["Food & Dining"] == 2
        assert result["groups"]["Groceries"] == 2

    def test_group_by_month_buckets_dates(self):
        result = tools.aggregate(SAMPLE_ROWS, op="sum", group_by="month")
        # 3 rows in 2026-04 (food + food + groceries), 4 rows in 2026-05.
        assert result["groups"]["2026-04"] == round(-450 + -300 + -1200, 2)
        assert result["groups"]["2026-05"] == round(-800 + -2500 + -180 + 50, 2)
        assert result["op"] == "sum"
        assert result["group_by"] == "month"

    def test_group_by_year_buckets_dates(self):
        result = tools.aggregate(SAMPLE_ROWS, op="count", group_by="year")
        # All 7 SAMPLE_ROWS are 2026.
        assert result["groups"] == {"2026": 7}

    def test_group_by_month_with_count_op(self):
        result = tools.aggregate(SAMPLE_ROWS, op="count", group_by="month")
        assert result["groups"]["2026-04"] == 3
        assert result["groups"]["2026-05"] == 4

    def test_unknown_op_raises(self):
        with pytest.raises(ValueError):
            tools.aggregate(SAMPLE_ROWS, op="median")

    def test_empty_rows(self):
        # sum/avg/min/max on empty list -> None (so the agent can detect it)
        for op in ("sum", "avg", "min", "max"):
            result = tools.aggregate([], op=op)
            assert result["value"] is None
        # count on empty list -> 0 (a real number)
        assert tools.aggregate([], op="count")["value"] == 0

    def test_handles_thousand_separator_amounts(self):
        rows = [_row(amount="-1,562.00"), _row(amount="-500.00")]
        result = tools.aggregate(rows, op="sum")
        assert result["value"] == -2062.0

    def test_skips_unparseable_amounts(self):
        rows = [_row(amount="-100"), _row(amount=""), _row(amount="garbage"), _row(amount="-200")]
        result = tools.aggregate(rows, op="sum")
        assert result["value"] == -300.0

    def test_most_spent_returns_positive_magnitude(self):
        # Biggest debit (most-negative) returned as a positive number.
        rows = [_row(amount="-100"), _row(amount="-3705"), _row(amount="50")]
        result = tools.aggregate(rows, op="most_spent")
        assert result["value"] == 3705.0

    def test_most_spent_with_only_credits(self):
        rows = [_row(amount="100"), _row(amount="50")]
        result = tools.aggregate(rows, op="most_spent")
        assert result["value"] == 0.0

    def test_most_income_returns_largest_credit(self):
        rows = [_row(amount="-100"), _row(amount="68584"), _row(amount="50")]
        result = tools.aggregate(rows, op="most_income")
        assert result["value"] == 68584.0

    def test_total_spent_sums_debits_only_as_positive(self):
        rows = [_row(amount="-100"), _row(amount="-200"), _row(amount="50")]
        result = tools.aggregate(rows, op="total_spent")
        assert result["value"] == 300.0

    def test_total_income_sums_credits_only(self):
        rows = [_row(amount="-100"), _row(amount="50"), _row(amount="200")]
        result = tools.aggregate(rows, op="total_income")
        assert result["value"] == 250.0


class TestGetRules:
    def test_passes_through_to_sheets_read_tab(self):
        canned = [{"pattern": "SWIGGY", "category": "Food & Dining"}]
        with patch("finance.tools.sheets.read_tab", return_value=canned) as mock_read:
            assert tools.get_rules() == canned
            mock_read.assert_called_once_with("rules")


class TestFlagForReview:
    def test_calls_sheets_update_flag(self):
        with patch("finance.tools.sheets.update_flag") as mock_update:
            result = tools.flag_for_review("h1", "looks suspicious")
        mock_update.assert_called_once_with("h1", "looks suspicious", tab="master")
        assert result == {"ok": True, "txn_hash": "h1", "reason": "looks suspicious"}
