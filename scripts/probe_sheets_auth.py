"""One-shot probe: verify FY26-master is bootstrapped with the right tabs and header.

Throwaway — not part of the project. Delete after Day 2 verification.
"""

import sys

import gspread

EXPECTED_HEADER = [
    "txn_hash",
    "date",
    "merchant",
    "amount",
    "category",
    "confidence",
    "source",
    "is_flagged",
    "is_duplicate",
    "notes",
]
EXPECTED_TABS = {"master", "axis"}


def main() -> int:
    gc = gspread.oauth(
        credentials_filename="credentials.json",
        authorized_user_filename="token.json",
    )
    sh = gc.open("FY26-master")

    actual_tabs = {ws.title for ws in sh.worksheets()}
    print(f"Tabs in FY26-master: {sorted(actual_tabs)}")

    missing = EXPECTED_TABS - actual_tabs
    if missing:
        print(f"✗ Missing tabs: {sorted(missing)}")
        return 1
    print(f"✓ Both expected tabs present.")

    bad = False
    for tab in sorted(EXPECTED_TABS):
        ws = sh.worksheet(tab)
        header = ws.row_values(1)
        if header == EXPECTED_HEADER:
            print(f"✓ '{tab}' header matches the frozen 10-col schema.")
        else:
            print(f"✗ '{tab}' header mismatch.")
            print(f"  expected: {EXPECTED_HEADER}")
            print(f"  actual:   {header}")
            bad = True
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
