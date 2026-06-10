"""Run the agent against eval/eval_qa.json and score each question.

Each question has:
  question         — the natural-language input to the agent
  expected_tools   — tools that MUST appear at least once (order-agnostic).
                     Extra tools are allowed; missing required tools fail.
  max_tool_calls   — optional cap on total tool calls (defaults to 5,
                     matching the agent's hard ceiling).
  expected_answer_contains — optional list of substrings that MUST appear
                     in the agent's final answer (case-insensitive). Use
                     this to assert specific values (e.g. "11,833.57") so
                     a silently broken aggregate doesn't pass.
  expected_tool_args — optional list of {"tool": ..., "args_contain": {dotted: val}}
                     constraints. For each item, at least one call to that tool
                     must have args matching EVERY required path. Substring +
                     case-insensitive on strings, exact equality otherwise.
                     Catches "called the right tool with the wrong filter".
  expected_behavior — free-text description (informational; not scored)

We run the live agent (real OpenAI call) but reads come from a frozen CSV
fixture (see cli.py `eval` command) so eval answers don't drift as the
real master sheet grows. Scoring is rule-based:

  - required-tools: every name in expected_tools must appear in actual_tools.
  - max_tool_calls: actual_tools count must be <= the cap.
  - expected_answer_contains: every substring must appear in the answer.
  - expected_tool_args: every declared tool/args constraint must be satisfied
    by at least one actual call. Catches the "right tool, wrong filter" bug.
  - for the no-data branch (id="no-data-branch"): the answer must
    contain one of NO_DATA_PHRASES (case-insensitive).
  - for the out-of-scope branch (id="out-of-scope-question"): the answer
    must contain one of REFUSAL_PHRASES — i.e. plausibly refused or
    redirected the user back to finance topics.
  - for the mixed-scope branch (id="mixed-scope-question"): the answer
    must NOT contain any ROMAN_LEAK_PHRASES — the off-topic half of a
    mixed prompt should be dropped, not answered.
  - if the agent errored ("agent looped" / "exceeded N tool calls"),
    the question fails outright.

The looser tool-sequence check (vs the original strict-prefix match) lets
the agent take SHORTER paths to the same answer when new features land —
e.g. when group_by='month' was added, the multi-month question collapsed
from 4 calls to 2. That's an improvement, not a regression.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from finance import agent

NO_DATA_PHRASES = (
    "no matching",
    "no transactions",
    "0 transactions",
    "no data",
    "did not find",
    "didn't find",
    "couldn't find",
    "could not find",
    "no spending",
    "no purchases",
    "no records",
    "spend on yachts was",       # the agent loves "your spend on X was ₹0" phrasing
    "₹0",
)

# An out-of-scope refusal should sound like a refusal — pointing the user back
# to finance topics. We're lenient: any one of these phrases counts.
REFUSAL_PHRASES = (
    "can only help",
    "can only answer",
    "only help with",
    "only answer",
    "finance assistant",
    "out of scope",
    "outside",          # "outside my scope", "outside what I can help with"
    "can't help",
    "can't teach",
    "cannot help",
    "cannot teach",
    "i'm not able",
    "not able to help",
    "stick to",
    "transaction",      # "your transactions" / "transaction data" — pivot back to scope
    "your spending",
    "your finances",
)

# Words that would indicate the agent answered the off-topic half of a mixed
# prompt instead of declining it. Case-insensitive substring match.
ROMAN_LEAK_PHRASES = (
    "roman empire",
    "roman republic",
    "caesar",
    "augustus",
    "bce",
    " bc ",
    "constantinople",
    "byzantine",
    "punic",
)

DEFAULT_MAX_TOOL_CALLS = 5


@dataclass
class QuestionResult:
    id: str
    question: str
    expected_tools: list[str]
    actual_tools: list[str]
    answer: str
    error: str | None = None
    failures: list[str] = field(default_factory=list)
    # Full tool calls with arguments — superset of `actual_tools` (which is
    # just the names). Use this when you want to assert HOW a tool was called,
    # not just THAT it was called.
    tool_calls: list[dict] = field(default_factory=list)
    # The agent's per-question observability summary (tokens / latency / model).
    # None if the agent died before yielding it (rare).
    summary: dict | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and not self.failures


def _check_required_tools(expected: list[str], actual: list[str]) -> str | None:
    """Return None if every name in `expected` appears in `actual` (any order).

    Extra tools in actual are fine — the agent may have done more than the
    minimum. Required tools that are missing fail loudly.
    """
    actual_set = set(actual)
    missing = [name for name in expected if name not in actual_set]
    if missing:
        return (
            f"missing required tool(s) {missing}: actual tools were {actual}"
        )
    return None


def _check_max_tool_calls(cap: int, actual: list[str]) -> str | None:
    """Return None if actual tool-call count is within the cap."""
    if len(actual) > cap:
        return f"too many tool calls: {len(actual)} > cap {cap}"
    return None


def _check_no_data_branch(answer: str) -> str | None:
    """Return None if the answer plausibly says 'no data'.

    Normalize curly apostrophes/quotes to straight ones first — the model
    sometimes writes "couldn't" with U+2019, and the phrase list uses U+0027.
    """
    lowered = answer.lower().replace("’", "'").replace("‘", "'")
    if any(phrase in lowered for phrase in NO_DATA_PHRASES):
        return None
    return f"answer doesn't acknowledge missing data: {answer!r}"


def _check_refusal(answer: str) -> str | None:
    """Return None if the answer plausibly refused / pointed back to finance.

    Same curly-quote normalization as `_check_no_data_branch`.
    """
    lowered = answer.lower().replace("’", "'").replace("‘", "'")
    if any(phrase in lowered for phrase in REFUSAL_PHRASES):
        return None
    return f"answer doesn't refuse off-topic question: {answer!r}"


def _check_no_off_topic_leak(answer: str) -> str | None:
    """Return None if the answer DOESN'T leak the off-topic content.

    For mixed-scope prompts the agent should answer the in-scope half and
    decline the rest. We allow the agent to *name* the off-topic topic when
    declining ("I can't teach about the Roman Empire") — that's correct
    behavior, not a leak. Only fail if Roman-Empire vocabulary appears
    WITHOUT a refusal phrase, which signals the agent actually answered it.
    """
    lowered = answer.lower().replace("’", "'").replace("‘", "'")
    leaked = [p for p in ROMAN_LEAK_PHRASES if p in lowered]
    if not leaked:
        return None
    if any(phrase in lowered for phrase in REFUSAL_PHRASES):
        return None  # named but refused — fine
    return f"answer leaked off-topic content (matched {leaked!r}): {answer!r}"


def _check_answer_contains(expected: list[str], answer: str) -> str | None:
    """Return None if every substring in `expected` appears in `answer`.

    Case-insensitive. Use this to assert specific values (e.g. "11,833.57")
    so a silently broken aggregate doesn't pass. Empty/missing list -> None.
    """
    if not expected:
        return None
    lowered = answer.lower()
    missing = [s for s in expected if s.lower() not in lowered]
    if missing:
        return f"answer missing expected substring(s) {missing}: {answer!r}"
    return None


def _get_path(d: dict, path: str):
    """Walk a dotted path into a dict. Missing keys -> None.

    >>> _get_path({"filters": {"merchant_contains": "amazon"}}, "filters.merchant_contains")
    'amazon'
    """
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _arg_matches(actual: Any, required: Any) -> bool:
    """Substring + case-insensitive match for strings; equality otherwise.

    A `query_sheet` arg of "Amazon" should satisfy a `required` of "amazon".
    Non-string equality is exact (dates are strings, but ints/bools/None compare strictly).
    """
    if isinstance(required, str) and isinstance(actual, str):
        return required.lower() in actual.lower()
    return actual == required


def _check_tool_args(
    expected_calls: list[dict] | None,
    tool_calls: list[dict],
) -> str | None:
    """Return None if every expected call was matched by some actual call.

    Each `expected_calls` item: {"tool": str, "args_contain": {dotted_path: value, ...}}.
    For each item, at least one actual call to `tool` must have args matching
    EVERY required (path -> value) pair. Substring + case-insensitive on strings.

    Empty/missing list -> None (the check is opt-in per question).
    """
    if not expected_calls:
        return None
    for expected in expected_calls:
        tool = expected.get("tool")
        required = expected.get("args_contain") or {}
        candidates = [c for c in tool_calls if c.get("name") == tool]
        if not candidates:
            return f"no call to {tool!r} found in trace"
        if not required:
            continue  # tool was called, no specific args required — pass
        # At least one of the candidate calls must satisfy every required path.
        ok = False
        for c in candidates:
            args = c.get("args", {})
            if all(_arg_matches(_get_path(args, p), v) for p, v in required.items()):
                ok = True
                break
        if not ok:
            actual_args = [c.get("args") for c in candidates]
            return (
                f"no {tool!r} call had args matching {required!r}; "
                f"actual args were {actual_args!r}"
            )
    return None


def run_question(question_spec: dict, run_fn=agent.run) -> QuestionResult:
    """Execute one eval question end-to-end and score it."""
    qid = question_spec["id"]
    question = question_spec["question"]
    expected_tools: list[str] = question_spec.get("expected_tools", [])
    max_calls: int = question_spec.get("max_tool_calls", DEFAULT_MAX_TOOL_CALLS)
    expected_contains: list[str] = question_spec.get("expected_answer_contains", [])
    expected_tool_args: list[dict] = question_spec.get("expected_tool_args", [])

    actual_tools: list[str] = []
    tool_calls: list[dict] = []
    summary: dict | None = None
    answer = ""
    error: str | None = None

    for event in run_fn(question):
        kind = event.get("type")
        if kind == "tool_call":
            actual_tools.append(event["name"])
            tool_calls.append({"name": event["name"], "args": event.get("args", {})})
        elif kind == "answer":
            answer = event.get("text", "")
        elif kind == "summary":
            summary = {k: v for k, v in event.items() if k != "type"}
        elif kind == "error":
            error = event.get("message", "unknown error")

    failures: list[str] = []
    if error is None:
        for check in (
            _check_required_tools(expected_tools, actual_tools),
            _check_max_tool_calls(max_calls, actual_tools),
            _check_answer_contains(expected_contains, answer),
            _check_tool_args(expected_tool_args, tool_calls),
        ):
            if check:
                failures.append(check)
        if qid == "no-data-branch":
            no_data_msg = _check_no_data_branch(answer)
            if no_data_msg:
                failures.append(no_data_msg)
        if qid == "out-of-scope-question":
            refusal_msg = _check_refusal(answer)
            if refusal_msg:
                failures.append(refusal_msg)
        if qid == "mixed-scope-question":
            leak_msg = _check_no_off_topic_leak(answer)
            if leak_msg:
                failures.append(leak_msg)

    return QuestionResult(
        id=qid,
        question=question,
        expected_tools=expected_tools,
        actual_tools=actual_tools,
        answer=answer,
        error=error,
        failures=failures,
        tool_calls=tool_calls,
        summary=summary,
    )


def run_all(spec: dict, run_fn=agent.run) -> list[QuestionResult]:
    """Run every question in the spec; return a list of results."""
    return [run_question(q, run_fn=run_fn) for q in spec.get("questions", [])]


def load_spec(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def format_summary(results: list[QuestionResult]) -> str:
    """Pretty multi-line report. Caller decides where to print it."""
    lines: list[str] = []
    for r in results:
        mark = "✓" if r.passed else "✗"
        lines.append(f"{mark} {r.id}: {r.question}")
        lines.append(f"    expected tools: {r.expected_tools}")
        lines.append(f"    actual tools:   {r.actual_tools}")
        if r.answer:
            ans = r.answer.replace("\n", " ")
            lines.append(f"    answer:         {ans[:200]}")
        if r.summary:
            s = r.summary
            tokens = s.get("tokens", {})
            cost = agent.cost_usd(
                s.get("model", ""),
                tokens.get("prompt", 0),
                tokens.get("completion", 0),
            )
            cost_str = f"~${cost:.4f}" if cost is not None else "$?"
            lines.append(
                f"    metrics:        {tokens.get('total', 0):,} tokens · "
                f"{cost_str} · {s.get('latency_s', 0)}s · "
                f"{s.get('tool_calls', 0)} tool calls · "
                f"{s.get('model_calls', 0)} model calls"
            )
        if r.error:
            lines.append(f"    ERROR: {r.error}")
        for fail in r.failures:
            lines.append(f"    FAIL:  {fail}")
        lines.append("")

    passed = sum(1 for r in results if r.passed)
    lines.append(f"{passed}/{len(results)} passed")

    # Suite-level totals across every question that emitted a summary.
    summaries = [r.summary for r in results if r.summary]
    if summaries:
        total_prompt = sum(s["tokens"]["prompt"] for s in summaries)
        total_completion = sum(s["tokens"]["completion"] for s in summaries)
        total_tokens = sum(s["tokens"]["total"] for s in summaries)
        total_latency = sum(s["latency_s"] for s in summaries)
        # Cost: aggregated across questions, per-question rates come from each
        # summary's model (in case a future run mixes models per question).
        total_cost = 0.0
        any_priced = False
        for s in summaries:
            c = agent.cost_usd(s["model"], s["tokens"]["prompt"], s["tokens"]["completion"])
            if c is not None:
                total_cost += c
                any_priced = True
        cost_str = f"~${total_cost:.4f}" if any_priced else "$?"
        n = len(summaries)
        lines.append(
            f"[{total_tokens:,} tokens · {cost_str} · {total_latency:.1f}s · "
            f"avg {total_tokens // n:,} tokens / {total_latency / n:.1f}s per question]"
        )

    return "\n".join(lines)
