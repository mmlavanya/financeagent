"""Family Financial Tracker — command-line entrypoint.

Two subcommands:
  ingest <file>   parse a bank/UPI export, dedupe, categorize, write to Sheets
  qa <question>   ask a natural-language question over the master sheet
"""

import click
from dotenv import load_dotenv

load_dotenv()


@click.group()
def cli():
    """Family Financial Tracker."""


@cli.command()
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Run parse + skip-rules only. No LLM calls, no Sheets writes.",
)
def ingest(file: str, dry_run: bool) -> None:
    """Parse FILE (Excel/CSV), dedupe, categorize, and append to Google Sheets."""
    # Lazy imports so `cli.py --help` and unit tests don't pull gspread / openai.
    from finance.parsers import axis_card
    from finance.skip_rules import apply_skip_rules, load_skip_rules

    click.echo(f"Parsing {file}...")
    parsed = axis_card.parse(file)
    click.echo(f"  parsed: {len(parsed)} rows")

    rules = load_skip_rules()
    kept, skipped = apply_skip_rules(parsed, rules)
    if skipped:
        click.echo(f"  skipped by rules: {len(skipped)}")
        for r in skipped:
            click.echo(f"    - {r['date']}  {r['amount']:>+12.2f}  {r['merchant']}")

    if dry_run:
        click.echo(f"\n[dry-run] would dedup, categorize, and write {len(kept)} rows to master.")
        click.echo(f"[dry-run] would write {len(parsed)} rows (incl. skipped) to axis_card audit.")
        return

    from finance import dedup
    from finance import sheets
    from finance.categorize import categorize

    existing = sheets.existing_hashes("master")
    new_rows, dup_rows = dedup.filter_new(kept, existing)
    click.echo(f"  duplicates of master: {len(dup_rows)}")
    click.echo(f"  new rows to categorize: {len(new_rows)}")

    if new_rows:
        click.echo(f"  categorizing {len(new_rows)} rows by rules...")
        categorize(new_rows)
        flagged = sum(1 for r in new_rows if r.get("is_flagged") == "TRUE")
        click.echo(f"  flagged for review (uncategorized): {flagged}")

    # Master tab: deduped + categorized rows.
    rows_for_master = new_rows + dup_rows
    if rows_for_master:
        n = sheets.append_rows(rows_for_master, tab="master")
        click.echo(f"  wrote {n} rows to master")

    # Per-source audit: every parsed row, even ones the skip-rules dropped.
    # Hashes get computed (best-effort) so the audit tab can be cross-referenced.
    for r in parsed:
        r.setdefault("txn_hash", dedup.compute_hash(r))
        r.setdefault("is_duplicate", "")
        r.setdefault("category", "")
        r.setdefault("confidence", "")
        r.setdefault("is_flagged", "")
        r.setdefault("notes", "")
    n_audit = sheets.append_rows(parsed, tab="axis_card")
    click.echo(f"  wrote {n_audit} rows to axis_card (audit)")

    click.echo("\nDone.")


@cli.command()
@click.argument("question")
def qa(question: str) -> None:
    """Answer QUESTION using the tool-using LLM agent over the master sheet."""
    from finance.agent import cost_usd, run

    answer = None
    summary = None
    for event in run(question):
        kind = event["type"]
        if kind == "tool_call":
            args_repr = ", ".join(f"{k}={v!r}" for k, v in event["args"].items())
            click.echo(f"[tool] {event['name']}({args_repr})", nl=False)
        elif kind == "tool_result":
            click.echo(f" → {event['summary']}")
        elif kind == "answer":
            answer = event["text"]
        elif kind == "summary":
            summary = event
        elif kind == "error":
            click.echo(f"\n[error] {event['message']}", err=True)
            # fall through so the summary still prints if present

    click.echo(f"\nAnswer: {answer or '(no answer)'}")

    if summary:
        tokens = summary["tokens"]
        cost = cost_usd(summary["model"], tokens["prompt"], tokens["completion"])
        cost_str = f"~${cost:.4f}" if cost is not None else "$?"
        click.echo(
            click.style(
                f"[{tokens['total']:,} tokens · {cost_str} · "
                f"{summary['latency_s']}s · {summary['tool_calls']} tool calls · "
                f"{summary['model_calls']} model calls · {summary['model']}]",
                dim=True,
            )
        )


@cli.command()
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print what would change. No Sheets writes.",
)
def recategorize(dry_run: bool) -> None:
    """Re-apply rules to every row in master. Useful after editing the rules tab."""
    import finance.sheets as sheets
    from finance.categorize import categorize, reset_cache

    reset_cache()  # always read the latest rules

    click.echo("Reading master tab...")
    rows = sheets.read_tab("master")
    click.echo(f"  {len(rows)} rows")

    # Snapshot current categories so we can diff after running rules.
    before = [(r.get("txn_hash"), r.get("category")) for r in rows]
    categorize(rows)  # mutates in place

    changed = []
    still_uncat = 0
    for (h, old_cat), row in zip(before, rows):
        new_cat = row.get("category")
        if new_cat == "Uncategorized":
            still_uncat += 1
        if new_cat != old_cat:
            changed.append((h, old_cat, new_cat, row))

    click.echo(f"  changes: {len(changed)} rows")
    click.echo(f"  still Uncategorized: {still_uncat} rows")

    if dry_run:
        click.echo("\n[dry-run] sample changes (up to 10):")
        for h, old, new, _ in changed[:10]:
            click.echo(f"  {h}  {old!r} -> {new!r}")
        return

    if not changed:
        click.echo("Nothing to update.")
        return

    click.echo(f"  writing {len(changed)} updates to master...")
    for _, _, _, row in changed:
        sheets.update_category(
            txn_hash=row["txn_hash"],
            category=row["category"],
            confidence=row.get("confidence", ""),
            is_flagged=row.get("is_flagged", "FALSE"),
            tab="master",
        )
    click.echo("Done.")


@cli.command("repair-hashes")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="List corrupted hashes without writing.",
)
def repair_hashes(dry_run: bool) -> None:
    """Recompute and overwrite txn_hash cells that Sheets type-coerced.

    Sheets silently parses cells whose displayed text looks number-ish
    (e.g. 'inf3a2b1' -> infinity, '7e8a23bc' -> scientific notation).
    This command finds rows where the cell's stored hash differs from the
    deterministic hash of (date, merchant, amount), and overwrites them
    with the recomputed value as forced text.
    """
    from finance import dedup, sheets

    click.echo("Reading master tab...")
    rows = sheets.read_tab("master")
    click.echo(f"  {len(rows)} rows")

    corrupted: list[tuple[int, str, str, dict]] = []
    for i, row in enumerate(rows):
        # row index in Sheets is i + 2 (1 for 1-indexed, 1 more for header)
        sheets_row_idx = i + 2
        stored = row.get("txn_hash", "")

        try:
            amount_str = str(row.get("amount", "")).replace(",", "").strip()
            row_for_hash = {
                "date": row["date"],
                "merchant": row["merchant"],
                "amount": float(amount_str),
            }
        except (KeyError, ValueError):
            continue  # malformed row — skip rather than guess

        expected = dedup.compute_hash(row_for_hash)
        if str(stored) != expected:
            corrupted.append((sheets_row_idx, stored, expected, row))

    click.echo(f"  corrupted hashes: {len(corrupted)}")
    for sheets_row_idx, stored, expected, row in corrupted[:20]:
        click.echo(
            f"    row {sheets_row_idx}: stored={stored!r:<20} "
            f"expected={expected}  merchant={row.get('merchant')!r}"
        )

    if dry_run or not corrupted:
        if dry_run:
            click.echo("\n[dry-run] no writes performed.")
        else:
            click.echo("Nothing to repair.")
        return

    click.echo(f"\n  writing {len(corrupted)} repairs to master...")
    for sheets_row_idx, _, expected, _ in corrupted:
        sheets.update_hash(sheets_row_idx, expected, tab="master")
    click.echo("Done.")


@cli.command("eval")
@click.option(
    "--spec",
    default="eval/eval_qa.json",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to the eval question spec JSON.",
)
@click.option(
    "--fixture-dir",
    default="eval/fixtures",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Directory holding master.csv / rules.csv. Set FINANCE_FIXTURE_DIR "
         "for the agent run so reads are deterministic and don't hit Sheets.",
)
def eval_cmd(spec: str, fixture_dir: str) -> None:
    """Run every question in eval/eval_qa.json against the live agent and score.

    Reads from a frozen CSV fixture (eval/fixtures/) instead of the live
    master sheet — so eval answers don't drift as the user adds transactions.
    Scoring is rule-based: tool sequence must match, no-data questions must
    acknowledge missing data, mixed-scope questions must not leak off-topic
    content, and any expected_answer_contains substrings must appear.
    """
    import os

    from finance import eval_runner

    # Point sheets.read_tab at the fixture before the agent imports it.
    os.environ["FINANCE_FIXTURE_DIR"] = fixture_dir

    spec_data = eval_runner.load_spec(spec)
    n_active = len(spec_data.get("questions", []))
    n_skipped = len(spec_data.get("skipped_questions", []))
    skip_note = f" ({n_skipped} parked)" if n_skipped else ""
    click.echo(f"Running {n_active} eval question(s){skip_note} "
               f"against fixture {fixture_dir}...\n")
    results = eval_runner.run_all(spec_data)
    click.echo(eval_runner.format_summary(results))

    # Persist this run as a JSONL trace — one line per question. Each future
    # eval run produces its own file in eval/runs/, so two runs are trivially
    # diffable ("did v2 of the prompt cost less? did any question regress?").
    run_path = _write_jsonl_run(results)
    click.echo(f"\nTrace: {run_path}")

    failures = [r for r in results if not r.passed]
    if failures:
        raise SystemExit(1)


def _write_jsonl_run(results) -> str:
    """Append one JSONL line per question to eval/runs/<unix_ts>.jsonl.

    Each line carries enough to reconstruct what happened: question id,
    model + prompt_version, full tool calls with args, answer, tokens,
    latency, pass/fail. Two runs are diffable as plain text.
    """
    import json
    import os
    import time

    from finance import agent

    runs_dir = "eval/runs"
    os.makedirs(runs_dir, exist_ok=True)
    ts = int(time.time())
    path = os.path.join(runs_dir, f"{ts}.jsonl")
    iso_ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts))

    with open(path, "w", encoding="utf-8") as f:
        for r in results:
            line = {
                "timestamp": iso_ts,
                "id": r.id,
                "model": agent.MODEL,
                "prompt_version": agent.PROMPT_VERSION,
                "question": r.question,
                "answer": r.answer,
                "tool_calls": r.tool_calls,
                "tokens": r.summary.get("tokens") if r.summary else None,
                "latency_s": r.summary.get("latency_s") if r.summary else None,
                "model_calls": r.summary.get("model_calls") if r.summary else None,
                "passed": r.passed,
                "failures": r.failures,
                "error": r.error,
            }
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    return path


@cli.command("snapshot-master")
@click.option(
    "--out-dir",
    default="eval/fixtures-private",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Where to write master.csv and rules.csv. Default lives outside "
         "the committed synthetic fixture so personal data isn't accidentally "
         "tracked. To eval against this snapshot: "
         "`cli.py eval --fixture-dir eval/fixtures-private`.",
)
def snapshot_master(out_dir: str) -> None:
    """Dump the live master + rules tabs to CSVs.

    NOTE: this writes your real personal transaction history. By default it
    goes to eval/fixtures-private/ which is git-ignored. The committed
    synthetic fixture lives at eval/fixtures/ and is what `cli.py eval`
    reads — DO NOT overwrite that with your real data.
    """
    import csv
    import os

    from finance import sheets

    if os.path.abspath(out_dir) == os.path.abspath("eval/fixtures"):
        raise click.UsageError(
            "Refusing to overwrite eval/fixtures/ with personal data — that "
            "directory holds the committed synthetic fixture. Use a different "
            "--out-dir (the default eval/fixtures-private/ is git-ignored)."
        )

    # Read live, NOT from a fixture — temporarily clear the env var if set.
    saved = os.environ.pop("FINANCE_FIXTURE_DIR", None)
    try:
        os.makedirs(out_dir, exist_ok=True)
        for tab in ("master", "rules"):
            rows = sheets.read_tab(tab)
            path = os.path.join(out_dir, f"{tab}.csv")
            if not rows:
                click.echo(f"  {tab}: 0 rows — writing empty file with header only")
                # Best-effort header for an empty tab: just leave the file empty.
                open(path, "w").close()
                continue
            fieldnames = list(rows[0].keys())
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            click.echo(f"  {tab}: wrote {len(rows)} rows to {path}")
    finally:
        if saved is not None:
            os.environ["FINANCE_FIXTURE_DIR"] = saved
    click.echo("Done.")


if __name__ == "__main__":
    cli()
