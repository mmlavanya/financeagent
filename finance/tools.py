"""Four tools the Q&A agent can call.

The agent never reads sheets directly — only through these. Three reasons:

1. Privacy. Only structured fields (date, merchant, amount, category) leave
   the local process; we never dump arbitrary cells into LLM prompts.

2. Reliability. The agent's role is to *decide what to compute*, not to
   compute it. aggregate() does real arithmetic in Python so a sum of
   100 rows is exactly correct, not "approximately ₹8,420".

3. Auditability. cli.py prints each tool call so you can see the agent's
   reasoning trace as it happens.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from finance import sheets


def query_sheet(filters: dict | None = None) -> list[dict]:
    """Return rows from the master tab matching all filters.

    Supported filter keys (all optional, all AND-combined):
        date_from           ISO 'YYYY-MM-DD' inclusive
        date_to             ISO 'YYYY-MM-DD' inclusive
        category            exact match (case-insensitive)
        merchant_contains   case-insensitive substring on merchant
        source              exact match (case-insensitive)
        is_flagged          'TRUE' or 'FALSE'

    Always re-reads master fresh — slow but always-correct.
    Returns a list of dicts with all 10 schema columns as strings.
    """
    rows = sheets.read_tab("master")
    if not filters:
        return rows

    f_from = filters.get("date_from")
    f_to = filters.get("date_to")
    f_cat = (filters.get("category") or "").lower()
    f_merch = (filters.get("merchant_contains") or "").lower()
    f_src = (filters.get("source") or "").lower()
    f_flag = filters.get("is_flagged")

    out = []
    for r in rows:
        if f_from and r.get("date", "") < f_from:
            continue
        if f_to and r.get("date", "") > f_to:
            continue
        if f_cat and r.get("category", "").lower() != f_cat:
            continue
        if f_merch and f_merch not in r.get("merchant", "").lower():
            continue
        if f_src and r.get("source", "").lower() != f_src:
            continue
        if f_flag is not None and r.get("is_flagged", "") != f_flag:
            continue
        out.append(r)
    return out


def aggregate(
    rows: list[dict],
    op: str,
    group_by: str | None = None,
) -> dict[str, Any]:
    """Reduce a list of rows over the `amount` column.

    op in {sum, count, avg, min, max, most_spent, most_income, total_spent, total_income}.

    The amount column uses negative=debit, positive=credit. The semantic ops
    hide that sign convention from callers:
      - most_spent     -> the single largest debit (e.g. biggest purchase)
      - most_income    -> the single largest credit
      - total_spent    -> sum of debits only, reported as a positive number
      - total_income   -> sum of credits only

    Use the raw ops (min/max/sum) only when you genuinely want signed math.
    'count' ignores the amount column.

    group_by accepts:
      - any column name (e.g. 'category', 'merchant', 'source')
      - the special values 'month' or 'year' which bucket by the date column.
    """
    op = op.lower()
    valid = {"sum", "count", "avg", "min", "max",
             "most_spent", "most_income", "total_spent", "total_income"}
    if op not in valid:
        raise ValueError(f"unknown op {op!r}; must be one of {sorted(valid)}")

    if group_by:
        groups: dict[str, list[float]] = defaultdict(list)
        counts: dict[str, int] = defaultdict(int)
        for r in rows:
            key = _group_key(r, group_by)
            counts[key] += 1
            if op != "count":
                amt = _coerce_amount(r.get("amount"))
                if amt is not None:
                    groups[key].append(amt)
        result = {}
        for key in counts:
            result[key] = _reduce(groups.get(key, []), op, counts[key])
        return {"groups": result, "op": op, "group_by": group_by}

    if op == "count":
        return {"value": len(rows), "op": op}

    amounts = [a for r in rows if (a := _coerce_amount(r.get("amount"))) is not None]
    return {"value": _reduce(amounts, op, len(rows)), "op": op}


def get_rules() -> list[dict]:
    """Return the categorization rules so the agent knows the taxonomy."""
    return sheets.read_tab("rules")


def flag_for_review(txn_hash: str, reason: str) -> dict:
    """Annotate a row in master as needing review. Returns a confirmation dict."""
    sheets.update_flag(txn_hash, reason, tab="master")
    return {"ok": True, "txn_hash": txn_hash, "reason": reason}


# --- helpers ---------------------------------------------------------------


def _group_key(row: dict, group_by: str) -> str:
    """Compute the grouping key for one row.

    Special values 'month' and 'year' slice the ISO date string. Any other
    value is looked up as a column name. Missing values return ''.
    """
    if group_by == "month":
        date = str(row.get("date", ""))
        return date[:7] if len(date) >= 7 else date  # 'YYYY-MM'
    if group_by == "year":
        date = str(row.get("date", ""))
        return date[:4] if len(date) >= 4 else date
    return str(row.get(group_by, ""))


def _coerce_amount(raw: Any) -> float | None:
    """Best-effort float of a Sheets-stringified amount. None on failure."""
    if raw is None or raw == "":
        return None
    try:
        return float(str(raw).replace(",", ""))
    except ValueError:
        return None


def _reduce(values: list[float], op: str, count: int) -> Any:
    """Apply op to a list of floats. count is needed for 'count' op only."""
    if op == "count":
        return count
    if not values:
        return None
    if op == "sum":
        return round(sum(values), 2)
    if op == "avg":
        return round(sum(values) / len(values), 2)
    if op == "min":
        return min(values)
    if op == "max":
        return max(values)
    if op == "most_spent":
        # biggest debit: most-negative value, returned as a positive magnitude
        return round(-min(values), 2) if min(values) < 0 else 0.0
    if op == "most_income":
        # biggest credit: largest positive value
        return round(max(values), 2) if max(values) > 0 else 0.0
    if op == "total_spent":
        return round(-sum(v for v in values if v < 0), 2)
    if op == "total_income":
        return round(sum(v for v in values if v > 0), 2)
    raise ValueError(op)  # unreachable
