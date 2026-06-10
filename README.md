# Family Financial Tracker

Local CLI tool for an Indian family to consolidate Axis Bank credit-card statements into Google Sheets, categorize them via deterministic rules, and answer natural-language questions over the data via a tool-using LLM agent.

## Success criteria

What "done" looks like for this POC. Each is currently met — the references point to the file or output that proves it.

- **Ingest is reproducible.** Running `cli.py ingest` on the same Axis statement twice produces zero new rows the second time. Dedup uses `sha1(date + amount + last4(merchant))[:8]` (`finance/dedup.py`), so the same input always hashes the same way; duplicates are flagged, not appended.
- **The agent answers the eval suite.** `cli.py eval` reports **10/10 passing** on the active question set on `gpt-5.4-mini` (one question is currently parked under `skipped_questions`; see Limitations and the Model comparison below), with substring assertions on specific known totals (₹11,833.57, ₹4,952.39, ₹42,500, etc.) — not just "the agent answered something."
- **Cost and latency are observable.** Every `qa` and `eval` run prints token count, USD cost, wall-clock latency, model name, and a hash of the system prompt. Per-question results land in `eval/runs/<unix_ts>.jsonl` so two runs are diffable as plain text — including across model swaps and prompt edits.
- **Privacy is enforceable, not just promised.** Real statements (`data/*.xlsx`) and credentials are git-ignored; the eval suite runs entirely on synthetic data so anyone cloning the repo gets the same answers; `cli.py snapshot-master` refuses to overwrite the public fixture with personal data. See "What goes to the LLM" below for the full data-flow story.
- **Guardrails, not just happy paths.** Off-topic prompts are refused with no tool calls; mixed-scope prompts answer the in-scope half only; tool-argument assertions catch the "right tool, wrong filter" failure mode (`finance/eval_runner.py`); the agent loop has a max-tool-call ceiling, an identical-call abort, and exception-handled tool dispatch — and failed runs still report their cost.

## What it does

- `python cli.py ingest <file>` — parse an Axis Bank Excel/CSV statement, dedupe against existing rows, rule-categorize by merchant pattern, and append to Google Sheets.
- `python cli.py qa "<question>"` — ask a natural-language question; an LLM agent answers using 4 tools (`query_sheet`, `aggregate`, `get_rules`, `flag_for_review`) and prints its tool-call trace plus token/cost/latency metrics.
- `python cli.py eval` — run the agent against a frozen synthetic test fixture and score it against expected behavior. Each run writes a JSONL trace to `eval/runs/<unix_ts>.jsonl`.
- `python cli.py snapshot-master` — dump the live `master` + `rules` tabs to CSV for ad-hoc analysis or eval against real data. Writes to `eval/fixtures-private/` (git-ignored) by default.
- `python cli.py recategorize` / `repair-hashes` — utility commands for re-applying rules after a taxonomy edit, and for repairing `txn_hash` cells that Sheets silently type-coerced.

## Evals

The agent is scored by `python cli.py eval` against an 11-question suite (`eval/eval_qa.json`) covering:
single-step lookup, multi-step comparison, no-data, full out-of-scope refusal, mixed in-scope + out-of-scope prompts,
biggest-purchase / superlative, total-income (with category disambiguation), category comparison on Transport,
top-merchant grouping, transaction count, and uncategorized-row detection.

Reads come from a frozen synthetic CSV (`eval/fixtures/master.csv`, 23 hand-built rows) so answers don't drift as live data grows.
Each question is scored on (1) which tools must fire, (2) max tool-call cap, (3) substring assertions on specific values
(e.g. `"11,833.57"`, `"42,500"`), (4) tool-argument assertions (e.g. `query_sheet` must be called with the right
`merchant_contains` AND date range), plus per-id checks for refusal / no-data / off-topic-leak phrasing.

Every run captures token count, USD cost, latency, and full tool-call args per question — both printed live and persisted as JSONL,
so two runs are diffable as plain text.

### Model comparison

Same eval, same prompt (`prompt_version: f17d4dc5`), two models. The suite has 11 questions defined; 1 is currently parked under `skipped_questions` in `eval_qa.json` (see Limitations), leaving **10 active questions** scored below.

| Model         | Pass  | Total tokens | Cost     | Total latency | Avg / question |
|---------------|-------|--------------|----------|---------------|----------------|
| gpt-5.4-mini  | 10/10 | 61,055       | $0.0113  | 46.6s         | 4.7s           |
| gpt-5.4-nano  | 9/10  | 64,658       | $0.0042  | 82.4s         | 8.2s           |

Nano is **~63% cheaper** but **~77% slower** and now also **fails one question that mini gets right** — `total-income-2026`. Mini correctly filters by `category=Income` (returning the ₹150,000 in salary credits); nano sums all positive amounts including small Amazon refunds (returning ₹151,050). At this eval's difficulty the trade-off is no longer purely cost-vs-latency: nano is also *less precise* about category disambiguation. Run `python cli.py eval` and inspect `eval/runs/<ts>.jsonl` to verify.

> Note on model availability: `MODEL` in `finance/agent.py` defaults to `gpt-5.4-mini`. The `PRICING` table also covers
> `gpt-4o-mini` / `gpt-4o` for reference; substitute one of those if running with a public OpenAI key that doesn't have
> `gpt-5.4` access.

## Architecture

- **Datastore:** Google Sheets — spreadsheet `FY26-master` with a `master` tab (deduped, categorized rows), a `rules` tab (merchant-pattern → category map driving `categorize.py`), and per-source audit tabs (e.g. `axis_card`) holding every parsed row including those dropped by skip rules.
- **Schema (frozen, 10 cols):** `txn_hash, date, merchant, amount, category, confidence, source, is_flagged, is_duplicate, notes`.
- **Amount sign:** negative = debit, positive = credit. The agent's `total_spent` / `total_income` ops hide this convention from the model.
- **Categorization:** rule-based (deterministic substring match against the `rules` tab — no LLM call). Unmatched rows are flagged `Uncategorized` for human triage.
- **LLM:** OpenAI `gpt-5.4-mini` for the Q&A agent (default; nano works too — see Evals table above). Categorization does NOT use an LLM.
- **Dedup:** `sha1(date + amount + last4(merchant))[:8]`, conservative (keep both on collision, mark `is_duplicate=TRUE`).
- **Agent guards:** max 5 tool calls per question, identical-call abort, refusal of off-topic prompts, system prompt forbids in-token arithmetic.

### What goes to the LLM (and what doesn't)

A reviewer should know exactly what data leaves the local machine, since the agent calls OpenAI.

- **What never leaves the machine.** Real bank statements (`data/*.xlsx`), Google OAuth credentials (`credentials.json`, `token.json`), the `.env` file, and any private snapshot under `eval/fixtures-private/`. All of these are git-ignored and never serialized into a prompt.

- **What goes to OpenAI during a `cli.py qa` query.** Whatever rows match the user's question — `query_sheet` filters by date / category / merchant on the local Python side first, then returns only the matching rows to the agent, which sends them as a `tool_result` message in the next OpenAI call. So the LLM does see merchants, amounts, dates, and categories *for the rows relevant to the asked question* — that's unavoidable for it to answer "how much did I spend on Amazon in May." It does NOT see: rows outside the filter, columns outside the 10-column schema, raw spreadsheet cells, or any credentials.

- **What goes to OpenAI during a `cli.py eval` run.** Only synthetic data. The eval command sets `FINANCE_FIXTURE_DIR=eval/fixtures` so reads come from the committed synthetic CSV (`AMAZON FIXTURE`, `TESTCO GROCERS`, etc.) — never from your real Sheet. The JSONL traces written to `eval/runs/` therefore contain zero real PII, and can be safely shared if needed.

- **Architectural defense.** The agent has no direct access to Sheets — it can only see what the four tools in `finance/tools.py` return. Every tool call is printed to the terminal during `cli.py qa` (filter args + row count), and persisted in the JSONL trace during `cli.py eval`, so the user can audit every byte the agent saw. `cli.py snapshot-master` refuses to overwrite the public synthetic fixture with personal data, so a careless re-snapshot can't accidentally promote real rows into a committable file.

## Tradeoffs

Concrete design choices, what was rejected, and why.

- **Google Sheets vs SQLite/Postgres for the datastore.** Sheets wins because the user (an Indian household) already trusts it, the spreadsheet is shareable with family in one click, and the categorized data is *directly browsable* — anyone in the household can sort, filter, search, or pivot the rows in the browser without learning SQL or installing anything. The cost is read latency (each `query_sheet` re-reads the whole tab via gspread) and Sheets' silent type-coercion of hash-like strings (the reason `repair-hashes` exists — see `cli.py`).

- **Tool-calling agent vs hand-coded Q&A pipeline.** The agent picks which tool to call, so the system generalizes to questions we didn't anticipate (e.g. "biggest single purchase in May" works with no new code). The cost is unpredictability — mitigated by the eval suite, the identical-call abort, and the max-tool-call ceiling in `agent.py`. A hard-coded pipeline would be cheaper and more deterministic but would only answer the questions we anticipated.

- **Rule-based categorization vs LLM categorization.** The rules tab in Google Sheets maps merchant-name substrings to categories; `categorize.py` is deterministic, free per ingest, and trivially auditable (the user can see exactly which rule fired). The cost is that a new merchant pattern is a manual rules-tab edit. For a personal-scale tool with ~hundreds of distinct merchants, this is the right trade — and unmatched rows are flagged `Uncategorized` so the user sees what's missing.

- **Frozen synthetic CSV fixture vs live Sheet for evals.** Synthetic data (`eval/fixtures/master.csv`, 23 hand-built rows with fake merchants) means evals are reproducible by anyone cloning the repo and produce the same numbers every run. The cost is that fixture totals are hand-derived and have to be updated when the eval set grows. The alternative — running evals against the live Sheet — would have answers drift every time the user ingests a statement, breaking the suite.

- **Per-question JSONL trace vs single aggregate eval report.** Each `cli.py eval` writes `eval/runs/<unix_ts>.jsonl` with one line per question (model, prompt_version hash, full tool-call args, tokens, latency, pass/fail). Two runs are line-by-line diffable as plain text — you can compare two prompts or two models without re-running. The cost is more disk + slightly more eval-runner code, and traces can leak personal data if `--fixture-dir` is pointed at a real snapshot (which is why `eval/runs/` is git-ignored).

## Limitations

What the project deliberately doesn't do (yet), and known gaps a reviewer should know about up front.

- **Single-source parser (intentional scope choice).** Only Axis Bank credit-card Excel statements parse end-to-end (`finance/parsers/axis_card.py`). The current scope intentionally targets one well-tested source rather than several half-finished ones. Other statement formats (other banks, debit cards) are future scope — each would be a new module under `finance/parsers/` plugging into the same `dedup → categorize → sheets` pipeline.

- **No rate-limiting or retry on OpenAI calls.** A flaky network or a 429 surfaces as an unhandled exception in `agent.run()`. For a personal-scale tool answering a handful of questions per session this is fine; for production use, an exponential-backoff wrapper around `_get_client().chat.completions.create` would be the right addition.

- **`flag_for_review` tool is wired up but not exercised by any eval (future scope).** The tool is declared in `agent.py`'s `TOOL_SCHEMAS` and dispatched in `_TOOL_FNS`, but no eval question yet triggers it, so we don't have a behavioral baseline for how the agent uses it under uncertainty. A future eval like *"flag this row as suspicious"* with an `expected_tool_args` constraint on `flag_for_review` would close the loop.

- **Categorization rules require manual `rules` tab edits.** A new merchant pattern means a human opens the Sheet and adds a row. No UI, no auto-suggest, no LLM fallback. Unmatched rows are flagged `Uncategorized` so nothing is silently mis-bucketed, but the human-in-the-loop is the bottleneck. Consistent with the rule-based-categorization tradeoff (above) — adding LLM auto-suggest is a future enhancement, not a redesign.

## Setup

```bash
# 1. Create and activate a virtualenv
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure secrets
cp .env.example .env
# edit .env, fill in OPENAI_API_KEY

# 4. Place Google OAuth desktop client JSON at ./credentials.json
#    (Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 Client ID, type "Desktop")
#    Make sure Google Sheets API and Google Drive API are enabled on the project.

# 5. Verify
python cli.py --help

# 6. Run the eval (no Google auth needed — reads the synthetic fixture)
python cli.py eval
```

See `docs/family-financial-tracker-system-design.md` for the full system-design document.
