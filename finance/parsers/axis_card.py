"""Parser for Axis Bank credit card statement Excel exports.

Layout (observed from a 2026-06-01 statement):

  rows 0-5    preamble (cardholder, payment summary, statement metadata)
  row 6       header: Date | Transaction Details | (blank) | Amount (INR) | Debit/Credit
  rows 7-N    transactions

Each transaction is normalized to a dict with the keys downstream code expects:
  date      ISO YYYY-MM-DD string
  merchant  raw "MERCHANT,CITY" string (kept verbatim per design decision)
  amount    float; negative for Debit (money spent), positive for Credit
  source    constant "axis_card"
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

SOURCE = "axis_card"

# Header row signature — when we find the row whose first cell is exactly "Date"
# we know the next row is the first transaction.
_HEADER_FIRST_CELL = "Date"

# Excel columns (0-indexed) inside the transactions table.
_COL_DATE = 0
_COL_MERCHANT = 1
_COL_AMOUNT = 3
_COL_SIGN = 4

# Axis prints dates like "21 May '26".
_DATE_FORMAT = "%d %b '%y"


def parse(filepath: str) -> list[dict]:
    """Parse one Axis credit-card Excel statement into normalized row dicts.

    Returns rows in the order they appear in the file.
    Raises ValueError if the header row can't be located.
    """
    df = pd.read_excel(filepath, sheet_name=0, header=None)

    header_idx = _find_header_row(df)
    rows = []
    for _, raw in df.iloc[header_idx + 1 :].iterrows():
        row = _parse_row(raw)
        if row is not None:
            rows.append(row)
    return rows


def _find_header_row(df: pd.DataFrame) -> int:
    """Locate the row whose first cell is 'Date' — the column header row."""
    for idx, value in enumerate(df.iloc[:, 0]):
        if isinstance(value, str) and value.strip() == _HEADER_FIRST_CELL:
            return idx
    raise ValueError(
        f"Could not find header row (first cell == {_HEADER_FIRST_CELL!r}). "
        "Statement layout may have changed."
    )


def _parse_row(raw: pd.Series) -> dict | None:
    """Convert one Excel row into a normalized dict, or None to skip it."""
    date_cell = raw.iloc[_COL_DATE]
    merchant_cell = raw.iloc[_COL_MERCHANT]
    amount_cell = raw.iloc[_COL_AMOUNT]
    sign_cell = raw.iloc[_COL_SIGN]

    # Skip blank/footer rows — any of these missing means this isn't a transaction.
    if pd.isna(date_cell) or pd.isna(amount_cell) or pd.isna(sign_cell):
        return None

    date_iso = _parse_date(str(date_cell))
    merchant = str(merchant_cell).strip()
    amount = _parse_amount(str(amount_cell), str(sign_cell))

    return {
        "date": date_iso,
        "merchant": merchant,
        "amount": amount,
        "source": SOURCE,
    }


def _parse_date(cell: str) -> str:
    """'21 May \\'26' -> '2026-05-21'."""
    return datetime.strptime(cell.strip(), _DATE_FORMAT).date().isoformat()


def _parse_amount(amount_cell: str, sign_cell: str) -> float:
    """'₹ 1,562.00' + 'Debit' -> -1562.00.  '₹ 195.89' + 'Credit' -> 195.89."""
    cleaned = amount_cell.replace("₹", "").replace(",", "").strip()
    value = float(cleaned)

    sign = sign_cell.strip().lower()
    if sign == "debit":
        return -value
    if sign == "credit":
        return value
    raise ValueError(f"Unexpected Debit/Credit value: {sign_cell!r}")
