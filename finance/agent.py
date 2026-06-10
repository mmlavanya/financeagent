"""Tool-using OpenAI agent for natural-language Q&A over the master tab.

Loop:
    1. Send question + system prompt + tool schemas to the model (see MODEL constant)
    2. If response includes tool_calls -> execute, append results, repeat
    3. If response is plain text -> that's the final answer, stop
    4. Hard ceilings: max 5 tool calls; same (tool, args) twice in a row aborts

The agent yields events as it works so the CLI can stream the tool-call trace.
Each event is a dict with a 'type': 'tool_call' | 'tool_result' | 'answer' | 'error'.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Generator

from openai import OpenAI

from finance import tools

MODEL = "gpt-5.4-mini"
#MODEL = "gpt-5.4-nano"
MAX_TOOL_CALLS = 5

# Per-model pricing in USD per 1M tokens, as (prompt, completion).
# Keep this synced with provider pricing pages; values here are public
# OpenAI list prices used as a sensible default for the gpt-5.4 alias.
# Missing models -> cost_usd() returns None and the CLI shows "?" for $.
PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o-mini":   (0.15, 0.60),
    "gpt-4o":        (2.50, 10.00),
    "gpt-5.4-mini":  (0.15, 0.60),
    "gpt-5.4-nano":  (0.05, 0.20),
}


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """Estimate USD cost for a model run. None if model isn't priced."""
    rates = PRICING.get(model)
    if rates is None:
        return None
    p_rate, c_rate = rates
    return (prompt_tokens * p_rate + completion_tokens * c_rate) / 1_000_000

SYSTEM_PROMPT = """You answer financial questions about the user's transactions.

You are a finance assistant ONLY. Your scope is the user's transaction data: \
spending, income, categories, merchants, dates, totals, comparisons over their \
own data. If asked about anything outside that scope (general knowledge, world \
history, coding help, recipes, advice unrelated to their finances, etc.), \
politely decline in one short sentence and remind the user what you can help \
with. Do NOT call any tools for off-topic questions — just refuse and stop.

If a single prompt mixes an in-scope question with an out-of-scope one (e.g. \
"how much did I spend on X? Also tell me about Y"), ANSWER the in-scope part \
fully (call tools as normal) and add one short sentence declining the \
out-of-scope part. Do NOT refuse the whole prompt just because part of it is \
off-topic.

Always use the provided tools to fetch and aggregate data. NEVER compute sums, \
averages, or totals in your head — always call the `aggregate` tool. The \
tools' return values are authoritative; trust them over your own arithmetic.

The user's transactions live in a Google Sheet with these columns:
  txn_hash, date (YYYY-MM-DD), merchant, amount (negative=debit, positive=credit),
  category, confidence, source, is_flagged, is_duplicate, notes.

IMPORTANT: amounts use a sign convention where DEBITS (money spent) are NEGATIVE \
and CREDITS (income/refunds) are POSITIVE. So:
  - "biggest/highest spend" or "most expensive purchase" -> use op="min" \
(most-negative value, e.g. -3705 is more spent than -100)
  - "biggest/highest income" or "largest refund" -> use op="max"
  - "total spend" -> sum of negatives (the result will be negative; report it \
to the user as a positive amount, e.g. "₹13,146 spent" not "-₹13,146")

When reporting amounts to the user, drop the negative sign — say "₹3,705 spent on \
restaurants" rather than "spent -₹3,705".

Categories are user-defined; call `get_rules` if you need to know the available \
categories. To answer a question:
  1. Use `query_sheet` to fetch the relevant rows (filter by date/category/merchant).
  2. Use `aggregate` on those rows to compute totals/counts/averages.
  3. For "which month/year did I X" questions, use `aggregate` with \
`group_by="month"` or `group_by="year"` to bucket rows by date.
  4. For comparing periods (e.g. "April vs May", "this year vs last year"), \
fetch the full span in ONE `query_sheet` call covering all periods, then \
`aggregate` with `group_by="month"` (or `"year"`). Do not query each period \
separately — one query_sheet + one aggregate is enough.
  5. If a category name in the question is unfamiliar, vague, or might map \
to a different label in the data (e.g. user says "salary" or "rent" or \
"transportation"), CALL `get_rules` FIRST to see what categories actually \
exist, THEN call `query_sheet` with the matching one. Do NOT guess and \
return an empty result — verify the taxonomy first.
  6. If no rows match after a verified-correct filter, say so plainly — \
never invent transactions.

Keep answers concise. Show the numbers you computed and what they mean. \
Use ₹ for Indian rupees and format large numbers with commas (e.g. ₹8,420.50).
"""

# Short hash of SYSTEM_PROMPT — automatically changes whenever the prompt
# changes. Recorded in every eval JSONL line so two runs with different
# prompts are trivially distinguishable.
PROMPT_VERSION = hashlib.sha1(SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:8]

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "query_sheet",
            "description": "Fetch transaction rows from the master tab. Returns "
                           "a list of row dicts. All filters are optional and AND-combined.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filters": {
                        "type": "object",
                        "properties": {
                            "date_from": {"type": "string", "description": "ISO YYYY-MM-DD inclusive"},
                            "date_to": {"type": "string", "description": "ISO YYYY-MM-DD inclusive"},
                            "category": {"type": "string", "description": "Exact category name"},
                            "merchant_contains": {"type": "string", "description": "Case-insensitive substring of merchant"},
                            "source": {"type": "string", "description": "e.g. axis_card"},
                            "is_flagged": {"type": "string", "enum": ["TRUE", "FALSE"]},
                        },
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aggregate",
            "description": "Reduce a list of rows over the amount column. "
                           "ALWAYS use this for sums/totals/averages — never compute in your head.",
            "parameters": {
                "type": "object",
                "properties": {
                    "rows": {"type": "array", "items": {"type": "object"}, "description": "Rows from query_sheet"},
                    "op": {
                        "type": "string",
                        "enum": [
                            "sum", "count", "avg", "min", "max",
                            "most_spent", "most_income",
                            "total_spent", "total_income",
                        ],
                        "description": "Use 'most_spent' for biggest single purchase, "
                                       "'total_spent' for total spending, 'most_income' for "
                                       "biggest single credit, 'total_income' for total credits. "
                                       "These return positive magnitudes regardless of sign. "
                                       "Use sum/min/max only when you want signed math.",
                    },
                    "group_by": {
                        "type": "string",
                        "description": "Optional. Column name like 'category', 'merchant', "
                                       "'source'. Special values 'month' (buckets by YYYY-MM) "
                                       "and 'year' (buckets by YYYY) bucket rows by date.",
                    },
                },
                "required": ["rows", "op"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_rules",
            "description": "Return the categorization rules so you know the available categories.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "flag_for_review",
            "description": "Mark a row as needing human review. Use sparingly — only when "
                           "you spot a genuine anomaly while answering a question.",
            "parameters": {
                "type": "object",
                "properties": {
                    "txn_hash": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["txn_hash", "reason"],
            },
        },
    },
]

_TOOL_FNS = {
    "query_sheet": lambda args: tools.query_sheet(args.get("filters") or {}),
    "aggregate": lambda args: tools.aggregate(
        args.get("rows", []), args.get("op"), args.get("group_by")
    ),
    "get_rules": lambda args: tools.get_rules(),
    "flag_for_review": lambda args: tools.flag_for_review(
        args.get("txn_hash"), args.get("reason")
    ),
}


_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI()
    return _client


def run(question: str) -> Generator[dict, None, None]:
    """Run the agent loop. Yields trace events.

    Event shapes:
      {"type": "tool_call",   "name": str, "args": dict, "call_id": str}
      {"type": "tool_result", "name": str, "summary": str}
      {"type": "answer",      "text": str}
      {"type": "error",       "message": str}
      {"type": "summary",     "tokens": {prompt, completion, total},
                              "latency_s": float, "tool_calls": int,
                              "model": str, "model_calls": int}
        Always emitted last, even after an answer or error.
    """
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    last_call: tuple[str, str] | None = None

    # Observability counters. Accumulate across every model turn so a
    # multi-step question reports the TOTAL spend, not just the final turn.
    started = time.perf_counter()
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    model_calls = 0
    tool_calls_count = 0

    def _summary_event() -> dict:
        return {
            "type": "summary",
            "tokens": {
                "prompt": prompt_tokens,
                "completion": completion_tokens,
                "total": total_tokens,
            },
            "latency_s": round(time.perf_counter() - started, 3),
            "tool_calls": tool_calls_count,
            "model_calls": model_calls,
            "model": MODEL,
        }

    for _ in range(MAX_TOOL_CALLS + 1):
        resp = _get_client().chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOL_SCHEMAS,
            temperature=0,
        )
        model_calls += 1
        # resp.usage can be None if the API stripped it (rare); guard anyway.
        if resp.usage is not None:
            prompt_tokens += resp.usage.prompt_tokens or 0
            completion_tokens += resp.usage.completion_tokens or 0
            total_tokens += resp.usage.total_tokens or 0
        msg = resp.choices[0].message

        # Final answer (no tool calls).
        if not msg.tool_calls:
            yield {"type": "answer", "text": msg.content or ""}
            yield _summary_event()
            return

        # The assistant message must be appended verbatim before tool results.
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ],
        })

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            # Identical-call guard: same tool + same args twice in a row -> stop.
            sig = (name, json.dumps(args, sort_keys=True))
            if last_call == sig:
                yield {"type": "error", "message": "agent looped — same tool called twice in a row"}
                yield _summary_event()
                return
            last_call = sig

            tool_calls_count += 1
            yield {"type": "tool_call", "name": name, "args": args, "call_id": tc.id}

            try:
                fn = _TOOL_FNS.get(name)
                if fn is None:
                    raise ValueError(f"unknown tool {name!r}")
                result = fn(args)
            except Exception as e:
                result = {"error": str(e)}

            yield {"type": "tool_result", "name": name, "summary": _summarize(result)}

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, default=str),
            })

    yield {
        "type": "error",
        "message": f"agent exceeded {MAX_TOOL_CALLS} tool calls without producing an answer",
    }
    yield _summary_event()


def _summarize(result: Any) -> str:
    """One-line description of a tool result for the trace."""
    if isinstance(result, list):
        return f"{len(result)} rows"
    if isinstance(result, dict):
        if "value" in result:
            return f"{result['op']}={result['value']}"
        if "groups" in result:
            return f"{len(result['groups'])} groups"
        if "ok" in result:
            return "ok"
        if "error" in result:
            return f"ERROR: {result['error']}"
    return str(result)[:80]
