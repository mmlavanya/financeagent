"""Smoke test for sheets.py — exercises all three functions end-to-end against
FY26-master. Throwaway. After running successfully once, you can delete the
3 dummy rows from the master tab in the browser if you want a clean slate
before Day 4.
"""

import sys
from pathlib import Path

# Make the project root importable when run as `python scripts/probe_sheets_smoke.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from finance import sheets


DUMMY_ROWS = [
    {
        "txn_hash": "smoke_001",
        "date": "2026-05-14",
        "merchant": "SWIGGY BANGALORE",
        "amount": -450.00,
        "category": "Food & Dining",
        "confidence": 0.92,
        "source": "axis",
        "is_flagged": "FALSE",
        "is_duplicate": "FALSE",
        "notes": "",
    },
    {
        "txn_hash": "smoke_002",
        "date": "2026-05-15",
        "merchant": "BIG BAZAAR",
        "amount": -2340.50,
        "category": "Groceries",
        "confidence": 0.88,
        "source": "axis",
        "is_flagged": "FALSE",
        "is_duplicate": "FALSE",
        "notes": "",
    },
    {
        "txn_hash": "smoke_003",
        "date": "2026-05-15",
        "merchant": "UNKNOWN MERCHANT",
        "amount": -89.00,
        "category": "Unknown",
        "confidence": 0.42,
        "source": "axis",
        "is_flagged": "TRUE",
        "is_duplicate": "FALSE",
        "notes": "low confidence (0.42)",
    },
]


def main() -> int:
    print("1. existing_hashes() before append:")
    before = sheets.existing_hashes("master")
    print(f"   {len(before)} hashes already present")

    if any(r["txn_hash"] in before for r in DUMMY_ROWS):
        print("   smoke rows already exist — delete them in the browser and rerun.")
        return 1

    print("\n2. append_rows() — writing 3 dummy rows to 'master':")
    n = sheets.append_rows(DUMMY_ROWS, tab="master")
    print(f"   appended {n} rows ✓")

    print("\n3. existing_hashes() after append:")
    after = sheets.existing_hashes("master")
    new = after - before
    print(f"   {len(after)} hashes now present, {len(new)} new")
    expected_new = {r["txn_hash"] for r in DUMMY_ROWS}
    if new != expected_new:
        print(f"   ✗ unexpected diff. expected {expected_new}, got {new}")
        return 1
    print(f"   ✓ all 3 smoke hashes round-tripped")

    print("\n4. update_flag('smoke_001', '...') — flagging the Swiggy row:")
    sheets.update_flag("smoke_001", "agent flagged: smoke test reason A", tab="master")
    print("   ✓ first flag applied")

    print("\n5. update_flag('smoke_001', '...') again — testing notes accumulation:")
    sheets.update_flag("smoke_001", "agent flagged: smoke test reason B", tab="master")
    print("   ✓ second flag applied (notes should now contain BOTH reasons)")

    print("\n6. Verify in your browser:")
    print(f"   {sheets._spreadsheet().url}")
    print("   - master tab should have 3 new rows")
    print("   - smoke_001 row should have is_flagged=TRUE and notes containing 'reason A; ...; reason B'")
    print("   - smoke_003 row should still show is_flagged=TRUE with note 'low confidence (0.42)'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
