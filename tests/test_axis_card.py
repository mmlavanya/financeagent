"""Tests for parsers/axis_card.py — runs against the real May fixture file.

May's statement is the canonical test fixture — its row counts, sign
breakdowns, and BBPS-row presence are baked into the assertions below.
If you replace or rename data/CC_Statement_2026_May.xlsx, these tests
either skip (file missing) or fail (assertions don't match the file).
"""

import os

import pytest

from finance.parsers.axis_card import SOURCE, parse

FIXTURE = "/Users/I061778/finance-agent/data/CC_Statement_2026_May.xlsx"

pytestmark = pytest.mark.skipif(
    not os.path.exists(FIXTURE),
    reason=f"canonical fixture not present at {FIXTURE}",
)


@pytest.fixture(scope="module")
def rows():
    return parse(FIXTURE)


def test_row_count(rows):
    # 78 transactions in the file; the "** End of Statement **" footer is skipped.
    assert len(rows) == 77


def test_every_row_has_four_keys(rows):
    expected = {"date", "merchant", "amount", "source"}
    for r in rows:
        assert set(r.keys()) == expected


def test_date_is_iso(rows):
    import re
    for r in rows:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["date"]), r["date"]


def test_amount_is_float(rows):
    for r in rows:
        assert isinstance(r["amount"], float)


def test_source_is_constant(rows):
    assert SOURCE == "axis_card"
    for r in rows:
        assert r["source"] == "axis_card"


def test_debit_credit_signs(rows):
    # We know from manual inspection: 67 debits (negative), 10 credits (positive).
    debits = [r for r in rows if r["amount"] < 0]
    credits = [r for r in rows if r["amount"] > 0]
    assert len(debits) == 67
    assert len(credits) == 10


def test_first_row_matches_known_value(rows):
    # First in file is 2026-05-21, ₹195.89 Credit, AMAZON PAY.
    first = rows[0]
    assert first["date"] == "2026-05-21"
    assert first["amount"] == 195.89
    assert "AMAZON" in first["merchant"]


def test_bbps_row_present(rows):
    # The skip-rules tests assume this row exists in the parsed output.
    bbps = [r for r in rows if "BBPS" in r["merchant"]]
    assert len(bbps) == 1
    assert bbps[0]["amount"] == 68584.98
