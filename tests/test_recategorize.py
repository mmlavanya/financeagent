"""Tests for the cli.py recategorize command."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from finance import categorize as categorize_mod
from cli import cli


@pytest.fixture(autouse=True)
def reset_categorize_cache():
    categorize_mod.reset_cache()
    yield
    categorize_mod.reset_cache()


def _master_rows():
    return [
        {"txn_hash": "h1", "merchant": "SWIGGY,Bangalore", "category": "Other"},
        {"txn_hash": "h2", "merchant": "AMAZON PAY,Bangalore", "category": "Card Payment"},
        {"txn_hash": "h3", "merchant": "MYSTERIOUS,X", "category": "Other"},
    ]


def _rules_worksheet():
    """Mock for the categorize._load_rules() call (still goes via _worksheet)."""
    ws = MagicMock()
    ws.get_all_records.return_value = [
        {"pattern": "SWIGGY", "category": "Food & Dining"},
        {"pattern": "AMAZON PAY", "category": "Shopping"},
    ]
    return ws


def test_dry_run_does_not_call_update():
    runner = CliRunner()
    with patch("finance.sheets.read_tab", return_value=_master_rows()), \
         patch("finance.sheets._worksheet", return_value=_rules_worksheet()), \
         patch("finance.sheets.update_category") as mock_update:
        result = runner.invoke(cli, ["recategorize", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "3 rows" in result.output
    assert "changes: 3 rows" in result.output  # all 3 differ from old
    assert "still Uncategorized: 1 rows" in result.output  # MYSTERIOUS
    mock_update.assert_not_called()


def test_full_run_writes_changed_rows():
    runner = CliRunner()
    with patch("finance.sheets.read_tab", return_value=_master_rows()), \
         patch("finance.sheets._worksheet", return_value=_rules_worksheet()), \
         patch("finance.sheets.update_category") as mock_update:
        result = runner.invoke(cli, ["recategorize"])

    assert result.exit_code == 0, result.output
    # 3 rows changed: SWIGGY ('Other' -> 'Food & Dining'),
    # AMAZON ('Card Payment' -> 'Shopping'),
    # MYSTERIOUS ('Other' -> 'Uncategorized').
    assert mock_update.call_count == 3

    by_hash = {c.kwargs["txn_hash"]: c.kwargs for c in mock_update.call_args_list}
    assert by_hash["h1"]["category"] == "Food & Dining"
    assert by_hash["h1"]["is_flagged"] == "FALSE"
    assert by_hash["h2"]["category"] == "Shopping"
    assert by_hash["h3"]["category"] == "Uncategorized"
    assert by_hash["h3"]["is_flagged"] == "TRUE"


def test_no_changes_skips_writes():
    """If categories already match what the rules would produce, no updates happen."""
    pre_categorized = [
        {"txn_hash": "h1", "merchant": "SWIGGY,Bangalore", "category": "Food & Dining"},
        {"txn_hash": "h2", "merchant": "AMAZON PAY,Bangalore", "category": "Shopping"},
    ]
    runner = CliRunner()
    with patch("finance.sheets.read_tab", return_value=pre_categorized), \
         patch("finance.sheets._worksheet", return_value=_rules_worksheet()), \
         patch("finance.sheets.update_category") as mock_update:
        result = runner.invoke(cli, ["recategorize"])

    assert result.exit_code == 0, result.output
    assert "Nothing to update" in result.output
    mock_update.assert_not_called()
