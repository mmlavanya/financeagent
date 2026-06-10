# Family Financial Tracker — System Design

**Version:** 2.0
**Status:** Reflects shipped POC (current scope)

This document describes the system as built. It supersedes earlier design notes that proposed a broader scope (multi-bank, UPI, vision-fallback parsing, LLM categorization). Those remain valid future scope but are not in the current implementation.

---

## 1. Overview

A local-only personal-finance tool for an Indian household. It ingests Axis Bank credit-card statements (Excel), deduplicates rows by hash, applies rule-based categorization, persists everything in Google Sheets, and answers natural-language questions over the consolidated data via a tool-using LLM agent.

Single-household, single-machine, command-line. No web server, no auth, no cloud bill. The user runs it on their laptop; the spreadsheet is the user-facing artifact.

---

## 2. Architecture

```
┌──────────────────── CLI (local: click) ────────────────────┐
│  ingest <file>          qa "<question>"          eval      │
└─────┬──────────────────────────┬────────────────────┬──────┘
      │                          │                    │
      ▼                          ▼                    ▼
┌─────────────┐         ┌────────────────────┐  ┌────────────┐
│ Axis parser │         │  Q&A Agent         │  │ Eval       │
│ (Excel)     │         │  (OpenAI tool-use) │  │ runner     │
└──────┬──────┘         │  loop: plan→call→  │  │ (JSONL     │
       ▼                │  observe→answer    │  │  trace)    │
┌─────────────┐         └─────────┬──────────┘  └─────┬──────┘
│ Skip rules  │                   │  4 tools:         │
└──────┬──────┘                   │   query_sheet     │
       ▼                          │   aggregate       │
┌─────────────┐                   │   get_rules       │
│ Deduper     │ ← reads existing  │   flag_for_review │
│ (sha1 8c)   │   txn_hashes      │                   │
└──────┬──────┘                   │                   │
       ▼                          │                   │
┌──────────────────┐              │                   │
│ Categorizer      │ ← reads      │                   │
│ (rule-based)     │   `rules` tab│                   │
└──────┬───────────┘              │                   │
       ▼                          ▼                   ▼
┌────────────────────────────────────────────────────────────┐
│            Google Sheets — gspread, batch append           │
│   tabs: master · rules · axis_card (per-source audit)      │
└────────────────────────────────────────────────────────────┘
                                  ▲
                                  │ FINANCE_FIXTURE_DIR override
                                  │ swaps Sheets reads for a local
                                  │ CSV fixture during eval runs
                                  │
                          eval/fixtures/master.csv
                          eval/fixtures/rules.csv
```

Three CLI entrypoints share the same data layer. The agent never touches Sheets directly — only through the four tools in `finance/tools.py`.

---

## 3. Components

| Component | Responsibility | Module | Tech |
|---|---|---|---|
| **Axis parser** | Excel → normalized rows `{date, merchant, amount, source}` | `finance/parsers/axis_card.py` | `pandas.read_excel` |
| **Skip-rules** | Drop UPI duplicates / IGST / payments per `skip_rules.txt` | `finance/skip_rules.py` | stdlib |
| **Deduper** | `sha1(date + amount + last4(merchant))[:8]`; collision → keep both, mark `is_duplicate` | `finance/dedup.py` | `hashlib` |
| **Categorizer** | Substring match against `rules` tab; unmatched → `Uncategorized` + flagged | `finance/categorize.py` | none (deterministic) |
| **Sheets layer** | Read/write the master + rules + per-source tabs; preserves hash-string typing | `finance/sheets.py` | `gspread` + Google OAuth |
| **Tools** | Four functions exposed to the agent: `query_sheet`, `aggregate`, `get_rules`, `flag_for_review` | `finance/tools.py` | stdlib + `sheets` |
| **Agent** | OpenAI tool-using loop: plan → call → observe → answer; max 5 tool calls, identical-call abort, per-run token/latency capture | `finance/agent.py` | `openai` (chat completions, function calling) |
| **CLI** | `ingest` / `qa` / `eval` / `snapshot-master` / `recategorize` / `repair-hashes` | `cli.py` | `click` |
| **Eval runner** | Score the agent against `eval_qa.json`; persist per-question JSONL trace | `finance/eval_runner.py` | stdlib + `agent` |

---

## 4. Data model

**Master tab** (and per-source audit tabs) — frozen 10-column schema:

```
txn_hash | date | merchant | amount | category | confidence | source | is_flagged | is_duplicate | notes
```

- `txn_hash` is the dedup primary key. Always 8 hex chars; populated at write time. The leading `'` prefix on write forces Sheets to store it as text — a workaround for Sheets' silent type-coercion of hash-like strings (e.g. `inf12345` → `float('inf')`). The `repair-hashes` subcommand recovers rows where this slipped through.
- `amount` uses **negative = debit, positive = credit**. The agent's semantic ops (`total_spent`, `total_income`, `most_spent`) hide this convention from the model.
- One per-source audit tab per parser (currently just `axis_card`) holds *every* parsed row including ones the skip-rules dropped. Master holds only the post-skip, post-dedup, categorized rows.

**Rules tab** — two columns: `pattern | category`. Substring match; first-rule-wins; case-insensitive. The user owns this tab and edits it in the browser.

---

## 5. Key design decisions

1. **Rule-based categorization, not LLM.** Substring match against a user-owned `rules` tab. Deterministic, free, trivially auditable. The cost is that new merchant patterns require a manual rules-tab edit; unmatched rows are surfaced to the user (not silently dropped) by being flagged `Uncategorized`.

2. **Tool-using agent for Q&A only.** Categorization and parsing are bulk-shaped, single-shot work — a tool loop adds cost without changing output. The agent earns its keep on multi-step questions ("groceries: April vs May?"), branching on empty results, and avoiding in-token arithmetic.

3. **Send only the 10 schema columns to the LLM, never raw cells.** `query_sheet` filters server-side (Python) before returning rows; the agent sees only what it asked for. Reduces prompt-injection surface — the merchant string is the only attacker-controllable field, and it's short.

4. **Conservative dedup.** False removals are worse than missed duplicates. Hash collisions keep both rows and flag `is_duplicate=TRUE` rather than silently delete.

5. **No framework (no LangChain / LangGraph).** The pipeline is four sequential steps; raw OpenAI API calls keep the code surface small and the trace honest. Every tool call is visible in the terminal during `qa` and persisted in `eval/runs/<ts>.jsonl` during `eval`.

6. **Google Sheets as the datastore.** Family members can sort, filter, and read transactions in the browser without learning SQL or installing anything. No DB setup, no auth server. The cost is read latency (each `query_sheet` re-reads the whole tab) and Sheets' silent type-coercion (mitigated above).

7. **Local-only, CLI-driven.** No multi-tenant, no auth, no deployment, no web server. Eliminates an entire class of POC concerns and keeps the demo focused on the agent's behavior rather than UI polish.

8. **Frozen synthetic CSV fixture for evals.** `eval/fixtures/master.csv` (23 hand-built rows with fake merchants like `AMAZON FIXTURE`, `TESTCO GROCERS`) is the eval ground-truth. `cli.py eval` sets `FINANCE_FIXTURE_DIR=eval/fixtures` so reads come from the CSV, not Sheets — answers don't drift as live data grows, and the entire eval suite contains zero real PII.

---

## 6. Agent design

The Q&A surface is the **only** agent in the system.

### 6.1 Tools (4)

| Tool | Signature | Purpose |
|---|---|---|
| `query_sheet` | `(filters: {date_from, date_to, source, category, merchant_contains, is_flagged}) → list[row]` | Read filtered rows from the master tab. All filters AND-combined. |
| `aggregate` | `(rows, op, group_by?) → dict` | Numeric reduction over rows. `op ∈ {sum, count, avg, min, max, most_spent, most_income, total_spent, total_income}`; the semantic ops (`total_spent`, etc.) hide the sign convention. `group_by ∈ column-name` or `'month'` / `'year'`. |
| `get_rules` | `() → list[dict]` | Returns the `rules` tab so the agent knows what categories exist. |
| `flag_for_review` | `(txn_hash, reason) → dict` | Sets `is_flagged=TRUE` and appends a note when the agent spots an anomaly. |

The split between `query_sheet` (returns rows) and `aggregate` (reduces them) is deliberate — it keeps the reasoning visible in the trace (the agent shows what rows it operated on) and forces the model out of doing math in-token.

### 6.2 Loop and stop conditions

```
plan ──► call tool ──► observe ──► answer  OR  re-plan
                                       │
                                       └─ stop on: final text response,
                                                   max 5 tool calls,
                                                   or repeated identical call
```

- **Max 5 tool calls per question.** Hard ceiling; covers all current eval questions with headroom.
- **Identical-call guard.** Same tool + same args twice in a row → emit `error` event with "agent looped" and abort, rather than burn the budget.
- **Exception-handled dispatch.** A tool exception becomes `{"error": str(e)}` returned to the model — the model can see the error and recover, rather than crashing the loop.
- **Per-run summary event.** Every run (success or failure) emits a final `{"type": "summary", ...}` event with token counts, wall-clock latency, model name, and a hash of the system prompt. The CLI prints this; the eval runner persists it.

### 6.3 System prompt

`SYSTEM_PROMPT` (in `agent.py`) defines:
- The agent's scope (transactions only — refuse off-topic questions in one sentence; no tool calls for off-topic).
- Mixed-scope behavior (when a prompt mixes in-scope + out-of-scope, answer the in-scope half and decline the rest).
- The arithmetic rule (`NEVER` compute sums in-token; always use `aggregate`).
- The sign convention (negative = debit; map "biggest spend" to `most_spent`).
- The recipe for common shapes (single-step, comparison, group-by-period).
- The category-discovery rule (call `get_rules` if a category name in the question is unfamiliar or vague).

A SHA-1 hash of this prompt is recorded in every JSONL trace as `prompt_version` — making prompt changes diffable.

---

## 7. Evals

11 questions in `eval/eval_qa.json` covering single-step lookup, multi-step comparison, no-data, full out-of-scope refusal, mixed-scope, biggest-purchase / superlative, total-income (with category disambiguation), category comparison, top-merchant grouping, transaction count, and uncategorized-row detection.

Each question is scored on:
1. **Required tools.** Tools that must appear at least once (order-agnostic; extra tools allowed).
2. **Max tool-call cap.** Per-question budget on total tool calls.
3. **Substring assertions on the answer.** Specific known totals (`"11,833.57"`, `"42,500"`) must appear — catches silent aggregate bugs.
4. **Tool-argument assertions.** For each declared `(tool, args_contain)` pair, at least one actual call to that tool must have args matching every required (dotted-path → value) pair. Catches the "right tool, wrong filter" failure mode (e.g. `category: 'dining'` instead of `'Food & Dining'`).
5. **Per-id qualitative checks.** No-data answers must contain a "no matching" phrase; out-of-scope questions must trigger a refusal phrase; mixed-scope answers must not leak off-topic content (with a refusal-phrase escape hatch so naming the topic in a refusal is fine).

Every eval run writes `eval/runs/<unix_ts>.jsonl` — one line per question with `{timestamp, id, model, prompt_version, question, answer, tool_calls (full args), tokens, latency_s, model_calls, passed, failures, error}`. Two runs are diffable as plain text.

Current pass rate: **10/11** on both `gpt-5.4-mini` and `gpt-5.4-nano`. Both models fail the same `uncategorized-check` question (the agent passes `category=""` to `query_sheet` instead of `"Uncategorized"` — the eval correctly catches the wrong-strategy/right-answer case).

---

## 8. Non-functional concerns

| Concern | Status | Approach |
|---|---|---|
| **Cost** | Tracked, ~$0.01 per full eval run on `gpt-5.4-mini` | `PRICING` table + `cost_usd()` helper in `agent.py`; live print + JSONL persist |
| **Latency** | Tracked, ~5s per question on `gpt-5.4-mini` | `time.perf_counter()` in `agent.run()`; per-question + suite total reported |
| **Throughput (ingest)** | Untested at scale; designed for ~200–500 txns / upload | Single batch-append to Sheets per upload |
| **Rate limits** | No retries / backoff (known limitation) | A future exponential-backoff wrapper around the chat completion call would close this |
| **Privacy** | See README → "What goes to the LLM (and what doesn't)" | Real statements + credentials gitignored; eval suite uses synthetic data; `snapshot-master` refuses to overwrite the public fixture |

---

## 9. Failure modes and mitigations

- **Sheets type-coerces hash strings** (e.g. `inf12345` → `float('inf')`) → write with leading `'` prefix to force text storage; `repair-hashes` subcommand recovers rows that slipped through.
- **Agent loops on the same tool call** → identical-call guard aborts with a clear error.
- **Agent burns the budget on a hard question** → `MAX_TOOL_CALLS` ceiling.
- **Tool raises an exception** → caught and returned as `{"error": ...}`, not propagated; the model can recover.
- **Model returns malformed tool args** → caught by `json.JSONDecodeError`; treated as empty args.
- **Eval fixture drifts from live Sheets** → fixture is decoupled (`FINANCE_FIXTURE_DIR`); eval reads CSV, never the live Sheet, so live changes can't break tests.
- **Personal data accidentally written to the committed fixture** → `snapshot-master` defaults to `eval/fixtures-private/` (gitignored) and refuses to overwrite `eval/fixtures/`.

---

## 10. Out of scope (current)

Multi-bank parsers (HDFC, SBI, ICICI, Kotak), UPI exports, debit-card statements, vision-fallback parsing for scanned PDFs, Streamlit UI, manual cash entry, charts, budgets/alerts, account-aggregator integration, multi-tenant / cloud deployment, LLM-based auto-categorization, automatic retry / backoff on OpenAI calls, behavioral coverage of `flag_for_review` in the eval.

---

## 11. Future scope

The decoupled architecture (parsers in `finance/parsers/`, the rules tab as the categorization source of truth, the eval runner as a separable component) is intended to absorb these without redesign:

- **More parsers.** New module under `finance/parsers/`, normalizes to the same `(date, merchant, amount, source)` shape, plugs into the existing `dedup → categorize → sheets` pipeline.
- **Vision fallback.** A multimodal-model call for unknown PDF formats; existing format detector would route here on `pdfplumber` failure.
- **LLM-suggested categorization rules.** When ingesting unmatched merchants, propose new `rules`-tab entries for the user to approve.
- **`flag_for_review` eval coverage.** A question like "this row looks suspicious, flag it" with an `expected_tool_args` constraint on `flag_for_review`.
- **OpenAI rate-limit handling.** Exponential-backoff wrapper around `_get_client().chat.completions.create`.
- **Charts / aggregations layer over the master sheet** (separate from the agent — the user sees them in the spreadsheet directly).
