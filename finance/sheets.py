"""Google Sheets datastore for the family financial tracker.

Frozen 10-column schema, set by the user manually in FY26-master:

    txn_hash | date | merchant | amount | category | confidence | source |
    is_flagged | is_duplicate | notes

Public functions:
    existing_hashes()                              -> set of every txn_hash already in `master`
    read_tab(tab="master")                         -> list of dicts, all values as displayed text
    append_rows(rows, tab="master")                -> batch-append rows in schema order
    update_flag(txn_hash, reason)                  -> set is_flagged=TRUE on a row, append reason to notes
    update_category(txn_hash, category, ...)       -> overwrite category/confidence/is_flagged on a row
    update_hash(old_hash, new_hash, tab="master")  -> repair a row whose stored hash got type-corrupted

Reads must use read_tab(), NOT gspread's get_all_records(). Sheets type-coerces
cells whose displayed text looks number-ish: 'inf12345' -> float('inf'),
'5e8a23bc' -> 5e8 in scientific notation, '71937982' -> int. get_all_records()
returns those *typed* values, silently corrupting our hash primary keys.
read_tab() reads via get_all_values() which always returns the displayed text.
"""

from __future__ import annotations

import csv
import os
from functools import lru_cache

import gspread

# Frozen schema — column order matches the header row in both tabs.
SCHEMA = (
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
)

# 1-indexed column positions for the cells update_flag rewrites.
_COL_TXN_HASH = SCHEMA.index("txn_hash") + 1
_COL_CATEGORY = SCHEMA.index("category") + 1
_COL_CONFIDENCE = SCHEMA.index("confidence") + 1
_COL_IS_FLAGGED = SCHEMA.index("is_flagged") + 1
_COL_NOTES = SCHEMA.index("notes") + 1

SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME", "FY26-master")


@lru_cache(maxsize=1)
def _spreadsheet() -> gspread.Spreadsheet:
    """Open FY26-master once per process; gspread caches the auth token."""
    gc = gspread.oauth(
        credentials_filename="credentials.json",
        authorized_user_filename="token.json",
    )
    return gc.open(SPREADSHEET_NAME)


def _worksheet(tab: str) -> gspread.Worksheet:
    return _spreadsheet().worksheet(tab)


def _row_values(row: dict) -> list:
    """Project a row dict onto the schema's column order. Missing keys -> empty cell.

    The txn_hash is prefixed with a single quote so Sheets stores it as plain
    text. Without this, hashes like '5e8a23bc' get parsed as scientific notation
    and 'inf12345' becomes float infinity — silently corrupting the lookup key.
    """
    values = [row.get(col, "") for col in SCHEMA]
    raw_hash = values[SCHEMA.index("txn_hash")]
    if raw_hash and not str(raw_hash).startswith("'"):
        values[SCHEMA.index("txn_hash")] = f"'{raw_hash}"
    return values


def existing_hashes(tab: str = "master") -> set[str]:
    """Return every txn_hash currently in `tab`. Skips the header row."""
    column = _worksheet(tab).col_values(_COL_TXN_HASH)
    # column[0] is the header "txn_hash"; drop it and any blank trailing cells.
    # str() coerces any number-typed cells (Sheets type-coercion artefacts) back
    # to strings so set membership tests work consistently.
    return {str(h) for h in column[1:] if h}


def read_tab(tab: str = "master") -> list[dict]:
    """Read every data row in `tab` as a dict, with all values as displayed text.

    DO NOT use ws.get_all_records() — it returns Sheets-typed values, which
    silently corrupt hash strings that look like numbers (see module docstring).
    This function uses get_all_values() so what you get back matches what's
    displayed in the browser.

    If the env var FINANCE_FIXTURE_DIR is set, read <dir>/<tab>.csv instead
    of hitting Sheets. The eval command sets this so eval answers don't drift
    as the live master sheet grows. Writes (append/update) always go to the
    real sheet — fixtures are read-only by design.
    """
    fixture_dir = os.getenv("FINANCE_FIXTURE_DIR")
    if fixture_dir:
        return _read_fixture(fixture_dir, tab)
    rows = _worksheet(tab).get_all_values()
    if not rows:
        return []
    header = rows[0]
    return [dict(zip(header, row)) for row in rows[1:]]


def _read_fixture(fixture_dir: str, tab: str) -> list[dict]:
    """Load <fixture_dir>/<tab>.csv as a list of dicts. Empty file -> []."""
    path = os.path.join(fixture_dir, f"{tab}.csv")
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def append_rows(rows: list[dict], tab: str = "master") -> int:
    """Batch-append rows to `tab` in schema order. Returns count appended."""
    if not rows:
        return 0
    values = [_row_values(r) for r in rows]
    _worksheet(tab).append_rows(values, value_input_option="USER_ENTERED")
    return len(values)


def update_flag(txn_hash: str, reason: str, tab: str = "master") -> None:
    """Mark a row as flagged and append a reason to its notes cell.

    Idempotent on is_flagged (already TRUE stays TRUE).
    Notes accumulate: "; reason" appended to whatever's there, so multiple
    flag events on the same row preserve every reason.
    """
    ws = _worksheet(tab)
    hashes = [str(h) for h in ws.col_values(_COL_TXN_HASH)]
    try:
        # +1 because col_values is 0-indexed in Python but Sheets rows are 1-indexed,
        # and the header row is at index 0 / row 1.
        row_idx = hashes.index(txn_hash) + 1
    except ValueError:
        raise KeyError(f"txn_hash {txn_hash!r} not found in tab {tab!r}")

    existing_note = ws.cell(row_idx, _COL_NOTES).value or ""
    new_note = f"{existing_note}; {reason}" if existing_note else reason

    ws.update_cells([
        gspread.cell.Cell(row_idx, _COL_IS_FLAGGED, "TRUE"),
        gspread.cell.Cell(row_idx, _COL_NOTES, new_note),
    ])


def update_category(
    txn_hash: str,
    category: str,
    confidence: str | float = "",
    is_flagged: str = "FALSE",
    tab: str = "master",
) -> None:
    """Overwrite category/confidence/is_flagged for the row with `txn_hash`.

    Used by the 'recategorize' command after rules tab edits. Does NOT touch
    notes or any other column. One Sheets API call (batch update).
    Raises KeyError if the hash isn't in the tab.
    """
    ws = _worksheet(tab)
    hashes = [str(h) for h in ws.col_values(_COL_TXN_HASH)]
    try:
        row_idx = hashes.index(txn_hash) + 1
    except ValueError:
        raise KeyError(f"txn_hash {txn_hash!r} not found in tab {tab!r}")

    ws.update_cells([
        gspread.cell.Cell(row_idx, _COL_CATEGORY, str(category)),
        gspread.cell.Cell(row_idx, _COL_CONFIDENCE, str(confidence)),
        gspread.cell.Cell(row_idx, _COL_IS_FLAGGED, str(is_flagged)),
    ])


def update_hash(row_idx: int, new_hash: str, tab: str = "master") -> None:
    """Overwrite the txn_hash cell at `row_idx` with `new_hash` (1-indexed row).

    Used by the 'repair-hashes' command to heal rows where Sheets had silently
    type-coerced the original hash (e.g. 'inf3a2b1' -> float('inf')). The
    leading "'" forces Sheets to store the value as plain text so the same
    bug doesn't recur on the next round-trip.
    """
    if row_idx < 2:
        raise ValueError(f"row_idx must be >= 2 (row 1 is the header), got {row_idx}")
    ws = _worksheet(tab)
    ws.update_cell(row_idx, _COL_TXN_HASH, f"'{new_hash}")
