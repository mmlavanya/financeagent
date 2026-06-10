"""Pre-dedup row filter — drops rows from the master tab by merchant substring.

Reads skip_rules.txt (one pattern per line, '#' for comments, case-insensitive).
Used by ingest BEFORE dedup, so skipped rows never get categorized or written
to master. They DO still flow into the per-source audit tab — that decision
lives in cli.py, not here.

Three functions:
    load_skip_rules(path)              -> list of patterns
    should_skip(row, rules)            -> bool
    apply_skip_rules(rows, rules)      -> (kept, skipped)
"""

from __future__ import annotations

import os

DEFAULT_PATH = "skip_rules.txt"


def load_skip_rules(path: str = DEFAULT_PATH) -> list[str]:
    """Read patterns from `path`. Missing file -> [] (no rules)."""
    if not os.path.exists(path):
        return []
    rules: list[str] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                rules.append(stripped.lower())
    return rules


def should_skip(row: dict, rules: list[str]) -> bool:
    """True if the row's merchant contains any rule pattern (case-insensitive)."""
    merchant = str(row.get("merchant", "")).lower()
    return any(rule in merchant for rule in rules)


def apply_skip_rules(
    rows: list[dict],
    rules: list[str],
) -> tuple[list[dict], list[dict]]:
    """Partition rows into (kept, skipped) by the skip rules."""
    kept: list[dict] = []
    skipped: list[dict] = []
    for row in rows:
        (skipped if should_skip(row, rules) else kept).append(row)
    return kept, skipped
