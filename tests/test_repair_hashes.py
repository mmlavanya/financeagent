"""Tests for the cli.py repair-hashes command.

Verifies it identifies corrupted hashes (where stored != recomputed),
reports them clearly, and writes only when not in dry-run.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli import cli
from finance import dedup


def _row(date, merchant, amount, stored_hash):
    """Build a master-tab row dict like read_tab() returns: all-string values."""
    return {
        "txn_hash": stored_hash,
        "date": date,
        "merchant": merchant,
        "amount": str(amount),
    }


def _make_rows():
    """3 rows: 1 healthy, 1 with float-typed corruption, 1 with int-typed."""
    healthy = {"date": "2026-05-14", "merchant": "SWIGGY,Bangalore", "amount": -450.0}
    corrupt_inf = {"date": "2026-05-14", "merchant": "AMAZON PAY,Bangalore", "amount": -100.0}
    corrupt_int = {"date": "2026-05-15", "merchant": "BIG BAZAAR,Bangalore", "amount": -200.0}

    rows = [
        _row(healthy["date"], healthy["merchant"], healthy["amount"], dedup.compute_hash(healthy)),
        _row(corrupt_inf["date"], corrupt_inf["merchant"], corrupt_inf["amount"], "inf"),
        _row(corrupt_int["date"], corrupt_int["merchant"], corrupt_int["amount"], "12345678"),
    ]
    expected_inf = dedup.compute_hash(corrupt_inf)
    expected_int = dedup.compute_hash(corrupt_int)
    return rows, expected_inf, expected_int


def test_dry_run_reports_corruption_without_writing():
    rows, _, _ = _make_rows()
    runner = CliRunner()
    with patch("finance.sheets.read_tab", return_value=rows), \
         patch("finance.sheets.update_hash") as mock_update:
        result = runner.invoke(cli, ["repair-hashes", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "corrupted hashes: 2" in result.output
    assert "AMAZON PAY" in result.output
    assert "BIG BAZAAR" in result.output
    assert "[dry-run]" in result.output
    mock_update.assert_not_called()


def test_full_run_writes_repairs_in_correct_rows():
    rows, expected_inf, expected_int = _make_rows()
    runner = CliRunner()
    with patch("finance.sheets.read_tab", return_value=rows), \
         patch("finance.sheets.update_hash") as mock_update:
        result = runner.invoke(cli, ["repair-hashes"])

    assert result.exit_code == 0, result.output
    assert "corrupted hashes: 2" in result.output
    assert mock_update.call_count == 2

    # row indices in Sheets are list_index + 2 (header at row 1, list 0-indexed)
    calls = sorted(mock_update.call_args_list, key=lambda c: c.kwargs.get("row_idx", c.args[0]))
    first = calls[0].kwargs if calls[0].kwargs else dict(zip(["row_idx", "new_hash"], calls[0].args))
    second = calls[1].kwargs if calls[1].kwargs else dict(zip(["row_idx", "new_hash"], calls[1].args))

    # rows[1] (corrupt_inf) is sheet row 3; rows[2] (corrupt_int) is sheet row 4
    row_indices = sorted([calls[0].kwargs.get("row_idx") or calls[0].args[0],
                          calls[1].kwargs.get("row_idx") or calls[1].args[0]])
    assert row_indices == [3, 4]

    # And the new hashes must be the recomputed values.
    new_hashes = sorted([calls[0].kwargs.get("new_hash") or calls[0].args[1],
                         calls[1].kwargs.get("new_hash") or calls[1].args[1]])
    assert new_hashes == sorted([expected_inf, expected_int])


def test_no_corruption_skips_writes():
    """All hashes already match what compute_hash would produce."""
    healthy_a = {"date": "2026-05-14", "merchant": "SWIGGY", "amount": -450.0}
    healthy_b = {"date": "2026-05-15", "merchant": "BIG BAZAAR", "amount": -200.0}
    rows = [
        _row(healthy_a["date"], healthy_a["merchant"], healthy_a["amount"], dedup.compute_hash(healthy_a)),
        _row(healthy_b["date"], healthy_b["merchant"], healthy_b["amount"], dedup.compute_hash(healthy_b)),
    ]
    runner = CliRunner()
    with patch("finance.sheets.read_tab", return_value=rows), \
         patch("finance.sheets.update_hash") as mock_update:
        result = runner.invoke(cli, ["repair-hashes"])

    assert result.exit_code == 0, result.output
    assert "corrupted hashes: 0" in result.output
    assert "Nothing to repair" in result.output
    mock_update.assert_not_called()


def test_handles_amounts_with_thousand_separator_commas():
    """Sheets renders amounts as strings like '-1,562.00' via get_all_values."""
    row = {"date": "2026-05-14", "merchant": "BIG BAZAAR", "amount": -1562.0}
    expected = dedup.compute_hash(row)
    rows = [_row(row["date"], row["merchant"], "-1,562.00", "inf")]

    runner = CliRunner()
    with patch("finance.sheets.read_tab", return_value=rows), \
         patch("finance.sheets.update_hash") as mock_update:
        result = runner.invoke(cli, ["repair-hashes"])

    assert result.exit_code == 0, result.output
    assert mock_update.call_count == 1
    new_hash_arg = mock_update.call_args.kwargs.get("new_hash") or mock_update.call_args.args[1]
    assert new_hash_arg == expected
