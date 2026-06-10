"""Deduplication for ingested transactions.

Hash recipe (per design doc and 5-day plan):
    sha1(date + str(amount) + last4(merchant.lower()))[:8]

Truncated to 8 hex chars — collision-resistant enough at personal scale
(~6,000 txns/year) and short enough to scan visually in Sheets.

Two functions:
    compute_hash(row)            -> 8-char hex string
    filter_new(parsed, existing) -> (new_rows, dup_rows)

filter_new mutates each row by adding 'txn_hash'. Duplicates also get
'is_duplicate' = "TRUE" so they're visibly tagged in the master tab.
"""

from __future__ import annotations

import hashlib


def compute_hash(row: dict) -> str:
    """Stable 8-char hash for a parsed row dict.

    Uses the *normalized* form: lowercased merchant tail, amount as repr.
    Two runs of the parser on the same statement must produce the same hash.
    """
    merchant = str(row["merchant"]).lower()
    last4 = merchant[-4:] if len(merchant) >= 4 else merchant
    key = f"{row['date']}|{row['amount']}|{last4}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]


def filter_new(
    parsed: list[dict],
    existing: set[str],
) -> tuple[list[dict], list[dict]]:
    """Split parsed rows into (new, duplicates) by hash collision with existing.

    Each row gets 'txn_hash' added in place. Duplicates additionally get
    is_duplicate = "TRUE"; new rows get is_duplicate = "FALSE".

    A duplicate within the same parsed batch (date+amount+merchant repeats)
    is also counted as a duplicate from the second occurrence onwards —
    matches the conservative 'keep both, flag' policy.
    """
    new_rows: list[dict] = []
    dup_rows: list[dict] = []
    seen_in_batch: set[str] = set()

    for row in parsed:
        h = compute_hash(row)
        row["txn_hash"] = h
        if h in existing or h in seen_in_batch:
            row["is_duplicate"] = "TRUE"
            dup_rows.append(row)
        else:
            row["is_duplicate"] = "FALSE"
            new_rows.append(row)
            seen_in_batch.add(h)
    return new_rows, dup_rows
