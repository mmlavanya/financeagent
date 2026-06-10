"""End-to-end ingest pipeline test — Sheets boundary mocked.

Verifies the wiring in cli.py:
  parse -> skip_rules -> dedup -> categorize (rules-based) -> sheets.append_rows

The Sheets read (rules tab) and writes (master/axis_card tabs) are mocked
so the test runs offline.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from finance import categorize as categorize_mod
from cli import cli

FIXTURE = "/Users/I061778/finance-agent/data/CC_Statement_2026_May.xlsx"

pytestmark = pytest.mark.skipif(
    not os.path.exists(FIXTURE),
    reason=f"canonical fixture not present at {FIXTURE}",
)


@pytest.fixture(autouse=True)
def reset_categorize_cache():
    categorize_mod.reset_cache()
    yield
    categorize_mod.reset_cache()


def _fake_rules_worksheet():
    """Stand-in for the 'rules' tab read by categorize._load_rules."""
    ws = MagicMock()
    ws.get_all_records.return_value = [
        {"pattern": "SWIGGY", "category": "Food & Dining"},
        {"pattern": "AMAZON PAY", "category": "Shopping"},
        {"pattern": "ZOMATO", "category": "Food & Dining"},
        {"pattern": "ZEPTO", "category": "Groceries"},
    ]
    return ws


def test_dry_run_does_not_touch_sheets():
    runner = CliRunner()
    with patch("finance.sheets.existing_hashes") as mock_hashes, \
         patch("finance.sheets.append_rows") as mock_append, \
         patch.object(categorize_mod.sheets, "_worksheet") as mock_ws:
        result = runner.invoke(cli, ["ingest", FIXTURE, "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "parsed: 77 rows" in result.output
    assert "skipped by rules: 1" in result.output
    assert "BBPS" in result.output
    assert "would dedup, categorize, and write 76 rows" in result.output
    mock_hashes.assert_not_called()
    mock_append.assert_not_called()
    mock_ws.assert_not_called()


def test_full_ingest_writes_to_both_tabs():
    runner = CliRunner()
    rules_ws = _fake_rules_worksheet()

    with patch("finance.sheets.existing_hashes", return_value=set()), \
         patch("finance.sheets.append_rows") as mock_append, \
         patch.object(categorize_mod.sheets, "_worksheet", return_value=rules_ws):
        result = runner.invoke(cli, ["ingest", FIXTURE])

    assert result.exit_code == 0, result.output

    calls_by_tab = {}
    for call in mock_append.call_args_list:
        rows = call.args[0]
        tab = call.kwargs.get("tab", call.args[1] if len(call.args) > 1 else None)
        calls_by_tab[tab] = rows

    assert "master" in calls_by_tab
    assert "axis_card" in calls_by_tab
    assert len(calls_by_tab["master"]) == 76
    assert len(calls_by_tab["axis_card"]) == 77

    # Verify rules actually applied: AMAZON PAY rows should be 'Shopping'.
    master_rows = calls_by_tab["master"]
    amazon = [r for r in master_rows if "AMAZON PAY" in r["merchant"]]
    assert len(amazon) > 0
    for r in amazon:
        assert r["category"] == "Shopping"

    # Every master row has the full schema.
    for row in master_rows:
        for key in ("txn_hash", "date", "merchant", "amount",
                    "category", "confidence", "source",
                    "is_flagged", "is_duplicate", "notes"):
            assert key in row, f"missing {key} in master row"


def test_re_ingest_marks_duplicates():
    """Second ingest of the same file: every kept row should be flagged duplicate."""
    runner = CliRunner()

    from finance.parsers.axis_card import parse
    from finance.skip_rules import apply_skip_rules, load_skip_rules
    from finance import dedup
    parsed = parse(FIXTURE)
    kept, _ = apply_skip_rules(parsed, load_skip_rules())
    existing = {dedup.compute_hash(r) for r in kept}

    rules_ws = _fake_rules_worksheet()
    with patch("finance.sheets.existing_hashes", return_value=existing), \
         patch("finance.sheets.append_rows") as mock_append, \
         patch.object(categorize_mod.sheets, "_worksheet", return_value=rules_ws) as mock_ws:
        result = runner.invoke(cli, ["ingest", FIXTURE])

    assert result.exit_code == 0, result.output
    assert "duplicates of master: 76" in result.output
    # No new rows means rules tab never gets read.
    mock_ws.assert_not_called()


def test_unmatched_rows_marked_uncategorized_and_flagged():
    """Rows with no matching rule should be flagged for triage."""
    runner = CliRunner()

    # Rules that match almost nothing in this statement.
    sparse_rules = MagicMock()
    sparse_rules.get_all_records.return_value = [
        {"pattern": "NETFLIX", "category": "Entertainment"},
    ]

    with patch("finance.sheets.existing_hashes", return_value=set()), \
         patch("finance.sheets.append_rows") as mock_append, \
         patch.object(categorize_mod.sheets, "_worksheet", return_value=sparse_rules):
        result = runner.invoke(cli, ["ingest", FIXTURE])

    assert result.exit_code == 0, result.output

    master_rows = next(
        call.args[0] for call in mock_append.call_args_list
        if call.kwargs.get("tab") == "master"
        or (len(call.args) > 1 and call.args[1] == "master")
    )
    uncategorized = [r for r in master_rows if r["category"] == "Uncategorized"]
    flagged = [r for r in master_rows if r["is_flagged"] == "TRUE"]
    # Almost every row should be uncategorized given only NETFLIX rule.
    assert len(uncategorized) >= 70
    assert len(flagged) == len(uncategorized)
