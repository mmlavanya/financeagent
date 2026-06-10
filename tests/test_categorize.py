"""Tests for the rules-based categorize.py.

The Sheets read is mocked so tests run offline.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from finance import categorize
from finance.categorize import UNCATEGORIZED, Rule, _apply_rules, categorize as do_categorize


def _row(merchant="SWIGGY,Bangalore", amount=-450.0):
    return {
        "date": "2026-05-10",
        "merchant": merchant,
        "amount": amount,
        "source": "axis_card",
        "txn_hash": "abc123",
        "is_duplicate": "FALSE",
    }


@pytest.fixture(autouse=True)
def reset_module_cache():
    categorize.reset_cache()
    yield
    categorize.reset_cache()


class TestApplyRules:
    """Pure logic — no Sheets, no mocks."""

    def test_first_match_wins(self):
        row = _row(merchant="SWIGGY,Bangalore")
        rules = [
            Rule(pattern_lower="swiggy", category="Food & Dining"),
            Rule(pattern_lower="bangalore", category="Travel"),
        ]
        _apply_rules(row, rules)
        assert row["category"] == "Food & Dining"
        assert row["is_flagged"] == "FALSE"

    def test_case_insensitive_match(self):
        row = _row(merchant="swiggy,bangalore")
        rules = [Rule(pattern_lower="swiggy", category="Food & Dining")]
        _apply_rules(row, rules)
        assert row["category"] == "Food & Dining"

    def test_substring_match(self):
        # 'AMAZON PAY' is a substring of 'AMAZON PAY INDIA PRIVA,Bangalore'
        row = _row(merchant="AMAZON PAY INDIA PRIVA,Bangalore")
        rules = [Rule(pattern_lower="amazon pay", category="Shopping")]
        _apply_rules(row, rules)
        assert row["category"] == "Shopping"

    def test_no_match_marks_uncategorized_and_flagged(self):
        row = _row(merchant="MYSTERIOUS MERCHANT")
        _apply_rules(row, [])
        assert row["category"] == UNCATEGORIZED
        assert row["is_flagged"] == "TRUE"
        assert row["confidence"] == ""

    def test_match_clears_confidence(self):
        # Rule-based categorization doesn't have a confidence concept.
        row = _row()
        rules = [Rule(pattern_lower="swiggy", category="Food & Dining")]
        _apply_rules(row, rules)
        assert row["confidence"] == ""

    def test_match_initializes_notes(self):
        row = _row()
        rules = [Rule(pattern_lower="swiggy", category="Food & Dining")]
        _apply_rules(row, rules)
        assert row["notes"] == ""

    def test_existing_notes_preserved(self):
        row = _row()
        row["notes"] = "manually annotated"
        rules = [Rule(pattern_lower="swiggy", category="Food & Dining")]
        _apply_rules(row, rules)
        assert row["notes"] == "manually annotated"


class TestLoadRules:
    """Verify the rules-tab read parses correctly."""

    def test_loads_and_lowercases_patterns(self):
        fake_ws = MagicMock()
        fake_ws.get_all_records.return_value = [
            {"pattern": "SWIGGY", "category": "Food & Dining"},
            {"pattern": "Zepto", "category": "Groceries"},
        ]
        with patch.object(categorize.sheets, "_worksheet", return_value=fake_ws):
            rules = categorize._load_rules()
        assert rules == [
            Rule(pattern_lower="swiggy", category="Food & Dining"),
            Rule(pattern_lower="zepto", category="Groceries"),
        ]

    def test_skips_blank_rows(self):
        fake_ws = MagicMock()
        fake_ws.get_all_records.return_value = [
            {"pattern": "SWIGGY", "category": "Food & Dining"},
            {"pattern": "", "category": "Bug"},        # blank pattern
            {"pattern": "ZEPTO", "category": ""},      # blank category
            {"pattern": "  ", "category": "Whitespace"},
        ]
        with patch.object(categorize.sheets, "_worksheet", return_value=fake_ws):
            rules = categorize._load_rules()
        assert len(rules) == 1
        assert rules[0].category == "Food & Dining"

    def test_caches_result(self):
        fake_ws = MagicMock()
        fake_ws.get_all_records.return_value = [
            {"pattern": "SWIGGY", "category": "Food & Dining"},
        ]
        with patch.object(categorize.sheets, "_worksheet", return_value=fake_ws):
            categorize._load_rules()
            categorize._load_rules()
            categorize._load_rules()
        # Worksheet should only be opened once across the three calls.
        assert fake_ws.get_all_records.call_count == 1

    def test_reset_cache_forces_reload(self):
        fake_ws = MagicMock()
        fake_ws.get_all_records.return_value = [
            {"pattern": "SWIGGY", "category": "Food & Dining"},
        ]
        with patch.object(categorize.sheets, "_worksheet", return_value=fake_ws):
            categorize._load_rules()
            categorize.reset_cache()
            categorize._load_rules()
        assert fake_ws.get_all_records.call_count == 2


class TestCategorizeFull:
    """End-to-end test of the public function."""

    def test_categorizes_a_batch(self):
        rows = [
            _row(merchant="SWIGGY,Bangalore"),
            _row(merchant="AMAZON PAY INDIA PRIVA,Bangalore"),
            _row(merchant="MYSTERIOUS"),
        ]
        fake_ws = MagicMock()
        fake_ws.get_all_records.return_value = [
            {"pattern": "SWIGGY", "category": "Food & Dining"},
            {"pattern": "AMAZON PAY", "category": "Shopping"},
        ]
        with patch.object(categorize.sheets, "_worksheet", return_value=fake_ws):
            do_categorize(rows)

        assert rows[0]["category"] == "Food & Dining"
        assert rows[1]["category"] == "Shopping"
        assert rows[2]["category"] == UNCATEGORIZED
        assert rows[2]["is_flagged"] == "TRUE"

    def test_empty_input_short_circuits(self):
        # Should not even open the rules tab on empty input.
        with patch.object(categorize.sheets, "_worksheet") as mock_ws:
            do_categorize([])
        mock_ws.assert_not_called()
