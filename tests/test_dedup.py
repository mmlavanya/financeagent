"""Tests for dedup.py — hash determinism, intra-batch dedup, existing-set."""

from finance.dedup import compute_hash, filter_new


def _row(date="2026-05-10", merchant="SWIGGY,Bangalore", amount=-450.0):
    return {"date": date, "merchant": merchant, "amount": amount, "source": "axis_card"}


class TestComputeHash:
    def test_returns_8_char_hex(self):
        h = compute_hash(_row())
        assert len(h) == 8
        assert all(c in "0123456789abcdef" for c in h)

    def test_deterministic(self):
        assert compute_hash(_row()) == compute_hash(_row())

    def test_changes_on_any_field(self):
        base = compute_hash(_row())
        assert compute_hash(_row(date="2026-05-11")) != base
        assert compute_hash(_row(amount=-451.0)) != base
        # Merchant only contributes its last 4 chars to the hash, so the
        # change has to land in those 4 chars to flip the hash.
        assert compute_hash(_row(merchant="SWIGGY,Mumbai")) != base

    def test_uses_only_last_4_chars_of_merchant(self):
        # "SWIGGY,Bangalore" and "ZOMATO,Bangalore" share last4='alor'
        # so they collide intentionally (conservative dedup).
        h1 = compute_hash(_row(merchant="SWIGGY,Bangalore"))
        h2 = compute_hash(_row(merchant="ZOMATO,Bangalore"))
        assert h1 == h2

    def test_short_merchant_handled(self):
        # Less than 4 chars shouldn't crash.
        h = compute_hash(_row(merchant="abc"))
        assert len(h) == 8

    def test_case_insensitive_on_merchant(self):
        # Hash uses .lower(), so case shouldn't matter.
        assert compute_hash(_row(merchant="SWIGGY")) == compute_hash(_row(merchant="swiggy"))


class TestFilterNew:
    def test_all_new_when_existing_empty(self):
        rows = [_row(amount=-100), _row(amount=-200), _row(amount=-300)]
        new, dups = filter_new(rows, existing=set())
        assert len(new) == 3
        assert len(dups) == 0
        for r in new:
            assert r["is_duplicate"] == "FALSE"
            assert "txn_hash" in r

    def test_all_dup_when_existing_full(self):
        rows = [_row(amount=-100), _row(amount=-200)]
        existing = {compute_hash(r) for r in rows}
        new, dups = filter_new(rows, existing)
        assert len(new) == 0
        assert len(dups) == 2
        for r in dups:
            assert r["is_duplicate"] == "TRUE"

    def test_intra_batch_dedup(self):
        # Two identical rows in one batch: first is new, second is dup.
        rows = [_row(amount=-100), _row(amount=-100)]
        new, dups = filter_new(rows, existing=set())
        assert len(new) == 1
        assert len(dups) == 1

    def test_mixed_new_and_dup(self):
        existing = {compute_hash(_row(amount=-100))}
        rows = [_row(amount=-100), _row(amount=-200), _row(amount=-300)]
        new, dups = filter_new(rows, existing)
        assert len(new) == 2
        assert len(dups) == 1
        # Order preserved: the dup is the first one.
        assert dups[0]["amount"] == -100

    def test_mutates_in_place(self):
        # filter_new adds txn_hash and is_duplicate to the dict the caller passed.
        rows = [_row(amount=-100)]
        original_id = id(rows[0])
        new, _ = filter_new(rows, existing=set())
        assert id(new[0]) == original_id
        assert "txn_hash" in rows[0]
