"""Tests for skip_rules.py — file parsing, matching, partition."""

from pathlib import Path

import pytest

from finance.skip_rules import apply_skip_rules, load_skip_rules, should_skip


def _row(merchant="SWIGGY,Bangalore"):
    return {"date": "2026-05-10", "merchant": merchant, "amount": -100.0}


class TestLoadSkipRules:
    def test_missing_file_returns_empty(self, tmp_path):
        path = tmp_path / "nope.txt"
        assert load_skip_rules(str(path)) == []

    def test_strips_comments_and_blanks(self, tmp_path: Path):
        path = tmp_path / "rules.txt"
        path.write_text(
            "# leading comment\n"
            "BBPS\n"
            "\n"
            "  # indented comment ignored too? actually no, we only ignore '#' at col 0 after strip\n"
            "Payment Received\n"
            "\n"
        )
        rules = load_skip_rules(str(path))
        assert rules == ["bbps", "payment received"]

    def test_lowercases_for_case_insensitive_match(self, tmp_path: Path):
        path = tmp_path / "rules.txt"
        path.write_text("BBPS\nPAYMENT received\n")
        assert load_skip_rules(str(path)) == ["bbps", "payment received"]


class TestShouldSkip:
    def test_no_rules_keeps_everything(self):
        assert should_skip(_row(), []) is False

    def test_substring_match(self):
        assert should_skip(_row(merchant="BBPS Payment Received"), ["bbps"]) is True

    def test_case_insensitive(self):
        assert should_skip(_row(merchant="bbps payment"), ["BBPS"]) is False  # rule passed pre-lower
        # Real-world callers always pass already-lowered rules from load_skip_rules,
        # so this is the contract: should_skip lowercases the merchant, not the rule.
        assert should_skip(_row(merchant="bbps payment"), ["bbps"]) is True
        assert should_skip(_row(merchant="BBPS PAYMENT"), ["bbps"]) is True

    def test_no_match(self):
        assert should_skip(_row(merchant="SWIGGY"), ["bbps"]) is False

    def test_any_rule_matches(self):
        assert should_skip(_row(merchant="Auto Debit"), ["bbps", "auto debit"]) is True


class TestApplySkipRules:
    def test_partitions_correctly(self):
        rows = [
            _row(merchant="SWIGGY"),
            _row(merchant="BBPS Payment Received"),
            _row(merchant="ZOMATO"),
            _row(merchant="Auto Debit ICICI"),
        ]
        kept, skipped = apply_skip_rules(rows, ["bbps", "auto debit"])
        assert [r["merchant"] for r in kept] == ["SWIGGY", "ZOMATO"]
        assert [r["merchant"] for r in skipped] == ["BBPS Payment Received", "Auto Debit ICICI"]

    def test_empty_rules_keeps_all(self):
        rows = [_row(), _row()]
        kept, skipped = apply_skip_rules(rows, [])
        assert len(kept) == 2 and len(skipped) == 0
