"""Rule-based transaction categorizer.

Reads the 'rules' tab of FY26-master once per process. For each row, scans
the rules in tab order and assigns the category of the first rule whose
pattern is a case-insensitive substring of the merchant. Unmatched rows are
marked Uncategorized + flagged for the user to triage.

No LLM call. Deterministic. Same merchant + same rules => same category, forever.

Public surface (signature unchanged from the LLM version it replaces):
    categorize(rows: list[dict]) -> list[dict]
"""

from __future__ import annotations

from typing import NamedTuple

from finance import sheets

UNCATEGORIZED = "Uncategorized"
RULES_TAB = "rules"


class Rule(NamedTuple):
    pattern_lower: str
    category: str


_rules_cache: list[Rule] | None = None


def categorize(rows: list[dict]) -> list[dict]:
    """Apply rules to each row in place. Returns the same list."""
    if not rows:
        return rows

    rules = _load_rules()
    for row in rows:
        _apply_rules(row, rules)
    return rows


def _load_rules() -> list[Rule]:
    """Read the 'rules' tab once and cache.

    Header row 1 is `pattern | category`. Rows 2+ are rules. Blank rows
    (no pattern) are silently skipped so accidental empty rows in the
    sheet don't match every merchant.
    """
    global _rules_cache
    if _rules_cache is not None:
        return _rules_cache

    records = sheets._worksheet(RULES_TAB).get_all_records()
    rules: list[Rule] = []
    for rec in records:
        pattern = str(rec.get("pattern", "")).strip()
        category = str(rec.get("category", "")).strip()
        if not pattern or not category:
            continue
        rules.append(Rule(pattern_lower=pattern.lower(), category=category))
    _rules_cache = rules
    return rules


def reset_cache() -> None:
    """Clear the cached rules (for tests, and for reloading after edits)."""
    global _rules_cache
    _rules_cache = None


def _apply_rules(row: dict, rules: list[Rule]) -> None:
    """Set category, confidence, is_flagged, notes on a single row."""
    merchant = str(row.get("merchant", "")).lower()
    for rule in rules:
        if rule.pattern_lower in merchant:
            row["category"] = rule.category
            row["confidence"] = ""        # not meaningful for rule-based
            row["is_flagged"] = "FALSE"
            row.setdefault("notes", "")
            return

    # No rule matched — mark for the user to triage.
    row["category"] = UNCATEGORIZED
    row["confidence"] = ""
    row["is_flagged"] = "TRUE"
    row.setdefault("notes", "")
