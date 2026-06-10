"""Tests for sheets.py — pure logic + thin gspread-mocked tests.

The pure-logic tests exercise _row_values (no network).
The mocked tests verify read_tab / update_hash use the right gspread APIs
so the get_all_records type-coercion bug can never sneak back in.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from finance import sheets


def test_row_values_prefixes_hash_with_quote():
    """Sheets silently coerces hex strings like 'inf12345' or '5e8a23bc' into
    floats. Prefixing with ' tells Sheets to treat the cell as forced text.
    Regression test for the hash-corruption bug discovered on 2026-06-03."""
    row = {
        "txn_hash": "abc12345",
        "date": "2026-05-10",
        "merchant": "SWIGGY",
        "amount": -450.0,
    }
    values = sheets._row_values(row)
    assert values[0] == "'abc12345"


def test_row_values_does_not_double_prefix():
    """If a row's hash is already quoted (e.g. read from a source that already
    handled this), don't add a second quote."""
    row = {"txn_hash": "'abc12345"}
    values = sheets._row_values(row)
    assert values[0] == "'abc12345"


def test_row_values_handles_inf_like_hash():
    """The exact failure mode we hit in production: a hash starting with 'inf'."""
    row = {"txn_hash": "inf3a2b1"}
    values = sheets._row_values(row)
    assert values[0] == "'inf3a2b1"


def test_row_values_handles_scientific_notation_hash():
    """Hex strings with 'e' between digits get parsed as scientific notation."""
    row = {"txn_hash": "5e8a23bc"}
    values = sheets._row_values(row)
    assert values[0] == "'5e8a23bc"


def test_row_values_skips_prefix_when_hash_empty():
    """Audit-tab rows may not have hashes pre-computed; empty stays empty."""
    row = {"merchant": "SWIGGY"}
    values = sheets._row_values(row)
    assert values[0] == ""


def test_row_values_preserves_schema_order():
    """A row in any dict-key order must come out as the canonical 10-col list."""
    row = {
        "merchant": "SWIGGY",
        "amount": -450.0,
        "txn_hash": "abc12345",
        "date": "2026-05-10",
    }
    values = sheets._row_values(row)
    assert len(values) == len(sheets.SCHEMA)
    assert values[sheets.SCHEMA.index("merchant")] == "SWIGGY"
    assert values[sheets.SCHEMA.index("amount")] == -450.0
    assert values[sheets.SCHEMA.index("date")] == "2026-05-10"
    # Unspecified keys default to empty string.
    assert values[sheets.SCHEMA.index("notes")] == ""


# ---------- read_tab regression tests --------------------------------------
# These guard the 2026-06-04 bug: get_all_records returns Sheets-typed values,
# silently corrupting hash strings. read_tab uses get_all_values (displayed
# text) which is immune. If anyone replaces the implementation with
# get_all_records, these tests fail.


def test_read_tab_uses_get_all_values_not_get_all_records():
    """The whole point of read_tab is that get_all_records is forbidden here."""
    fake_ws = MagicMock()
    fake_ws.get_all_values.return_value = [
        ["txn_hash", "merchant", "amount"],
        ["inf3a2b1", "AMAZON PAY", "-450.00"],
        ["5e8a23bc", "SWIGGY", "-200.00"],
    ]
    with patch.object(sheets, "_worksheet", return_value=fake_ws):
        rows = sheets.read_tab("master")

    fake_ws.get_all_values.assert_called_once()
    fake_ws.get_all_records.assert_not_called()
    assert rows == [
        {"txn_hash": "inf3a2b1", "merchant": "AMAZON PAY", "amount": "-450.00"},
        {"txn_hash": "5e8a23bc", "merchant": "SWIGGY", "amount": "-200.00"},
    ]


def test_read_tab_returns_string_typed_inf_hashes():
    """The headline failure mode: 'inf'-prefixed hashes survive the round-trip."""
    fake_ws = MagicMock()
    fake_ws.get_all_values.return_value = [
        ["txn_hash"],
        ["inf3a2b1"],
    ]
    with patch.object(sheets, "_worksheet", return_value=fake_ws):
        rows = sheets.read_tab("master")
    assert rows[0]["txn_hash"] == "inf3a2b1"
    assert isinstance(rows[0]["txn_hash"], str)


def test_read_tab_handles_empty_tab():
    fake_ws = MagicMock()
    fake_ws.get_all_values.return_value = []
    with patch.object(sheets, "_worksheet", return_value=fake_ws):
        assert sheets.read_tab("master") == []


def test_read_tab_handles_header_only():
    fake_ws = MagicMock()
    fake_ws.get_all_values.return_value = [["txn_hash", "merchant"]]
    with patch.object(sheets, "_worksheet", return_value=fake_ws):
        assert sheets.read_tab("master") == []


# ---------- update_hash regression tests -----------------------------------


def test_update_hash_writes_with_text_prefix():
    """The repaired hash must be stored as forced text or the bug recurs."""
    fake_ws = MagicMock()
    with patch.object(sheets, "_worksheet", return_value=fake_ws):
        sheets.update_hash(row_idx=5, new_hash="inf3a2b1", tab="master")

    # Column 1 is txn_hash; row 5 is the target; value must start with "'".
    fake_ws.update_cell.assert_called_once()
    args = fake_ws.update_cell.call_args.args
    assert args[0] == 5      # row
    assert args[1] == 1      # column (txn_hash)
    assert args[2] == "'inf3a2b1"


def test_update_hash_rejects_header_row():
    """row_idx 1 is the header — overwriting it would destroy the schema."""
    with patch.object(sheets, "_worksheet"):
        with pytest.raises(ValueError):
            sheets.update_hash(row_idx=1, new_hash="abc", tab="master")
