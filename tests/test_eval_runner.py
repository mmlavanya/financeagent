"""Tests for finance/eval_runner.py — pure scoring logic, agent mocked."""

from __future__ import annotations

from finance import eval_runner


def _stream(events):
    """Build a fake agent.run that yields the given events."""
    def run_fn(_question):
        yield from events
    return run_fn


def _spec(qid, question, expected_tools, **extra):
    spec = {"id": qid, "question": question, "expected_tools": expected_tools}
    spec.update(extra)
    return spec


class TestRequiredTools:
    def test_all_required_tools_present_passes(self):
        spec = _spec("q", "?", ["query_sheet", "aggregate"])
        run_fn = _stream([
            {"type": "tool_call", "name": "query_sheet"},
            {"type": "tool_call", "name": "aggregate"},
            {"type": "answer", "text": "₹100"},
        ])
        result = eval_runner.run_question(spec, run_fn=run_fn)
        assert result.passed
        assert result.actual_tools == ["query_sheet", "aggregate"]

    def test_extra_tools_allowed(self):
        spec = _spec("q", "?", ["query_sheet"])
        run_fn = _stream([
            {"type": "tool_call", "name": "get_rules"},
            {"type": "tool_call", "name": "query_sheet"},
            {"type": "tool_call", "name": "aggregate"},
            {"type": "answer", "text": "ok"},
        ])
        assert eval_runner.run_question(spec, run_fn=run_fn).passed

    def test_order_does_not_matter(self):
        spec = _spec("q", "?", ["query_sheet", "aggregate"])
        run_fn = _stream([
            {"type": "tool_call", "name": "aggregate"},   # reversed order
            {"type": "tool_call", "name": "query_sheet"},
            {"type": "answer", "text": "ok"},
        ])
        assert eval_runner.run_question(spec, run_fn=run_fn).passed

    def test_missing_required_tool_fails(self):
        spec = _spec("q", "?", ["query_sheet", "aggregate"])
        run_fn = _stream([
            {"type": "tool_call", "name": "query_sheet"},
            {"type": "answer", "text": "..."},
        ])
        result = eval_runner.run_question(spec, run_fn=run_fn)
        assert not result.passed
        assert any("missing required tool" in f for f in result.failures)
        assert any("aggregate" in f for f in result.failures)

    def test_smarter_solution_with_fewer_calls_passes(self):
        """The motivating case: agent uses group_by='month' to halve the calls."""
        spec = _spec("multi-step-comparison", "?",
                     ["query_sheet", "aggregate"], max_tool_calls=5)
        run_fn = _stream([
            {"type": "tool_call", "name": "query_sheet"},
            {"type": "tool_call", "name": "aggregate"},
            {"type": "answer", "text": "April vs May ..."},
        ])
        assert eval_runner.run_question(spec, run_fn=run_fn).passed


class TestMaxToolCalls:
    def test_within_cap_passes(self):
        spec = _spec("q", "?", ["query_sheet"], max_tool_calls=3)
        run_fn = _stream([
            {"type": "tool_call", "name": "query_sheet"},
            {"type": "tool_call", "name": "aggregate"},
            {"type": "answer", "text": "ok"},
        ])
        assert eval_runner.run_question(spec, run_fn=run_fn).passed

    def test_over_cap_fails(self):
        spec = _spec("q", "?", ["query_sheet"], max_tool_calls=2)
        run_fn = _stream([
            {"type": "tool_call", "name": "query_sheet"},
            {"type": "tool_call", "name": "aggregate"},
            {"type": "tool_call", "name": "aggregate"},
            {"type": "answer", "text": "ok"},
        ])
        result = eval_runner.run_question(spec, run_fn=run_fn)
        assert not result.passed
        assert any("too many tool calls" in f for f in result.failures)

    def test_default_cap_is_5(self):
        spec = _spec("q", "?", ["query_sheet"])  # no max_tool_calls => default
        run_fn = _stream(
            [{"type": "tool_call", "name": "query_sheet"}] * 6
            + [{"type": "answer", "text": "ok"}]
        )
        result = eval_runner.run_question(spec, run_fn=run_fn)
        assert not result.passed
        assert any("> cap 5" in f for f in result.failures)


class TestNoDataBranch:
    def test_acknowledges_no_data_passes(self):
        spec = _spec("no-data-branch", "yachts?", ["query_sheet"])
        run_fn = _stream([
            {"type": "tool_call", "name": "query_sheet"},
            {"type": "answer", "text": "No matching transactions."},
        ])
        assert eval_runner.run_question(spec, run_fn=run_fn).passed

    def test_zero_rupees_phrasing_also_passes(self):
        """The agent often says 'your spend on X was ₹0' — accept that too."""
        spec = _spec("no-data-branch", "yachts?", ["query_sheet"])
        run_fn = _stream([
            {"type": "tool_call", "name": "query_sheet"},
            {"type": "answer", "text": "Your spend on yachts was ₹0."},
        ])
        assert eval_runner.run_question(spec, run_fn=run_fn).passed

    def test_fabricated_answer_fails(self):
        spec = _spec("no-data-branch", "yachts?", ["query_sheet"])
        run_fn = _stream([
            {"type": "tool_call", "name": "query_sheet"},
            {"type": "answer", "text": "You spent ₹50,000 on yachts."},
        ])
        result = eval_runner.run_question(spec, run_fn=run_fn)
        assert not result.passed
        assert any("doesn't acknowledge" in f for f in result.failures)

    def test_no_data_check_only_applies_to_no_data_id(self):
        # Other question IDs are NOT scored on the no-data phrase.
        spec = _spec("single-step-merchant", "amazon?", ["query_sheet", "aggregate"])
        run_fn = _stream([
            {"type": "tool_call", "name": "query_sheet"},
            {"type": "tool_call", "name": "aggregate"},
            {"type": "answer", "text": "₹11,833 spent."},  # no no-data phrase, intentionally
        ])
        assert eval_runner.run_question(spec, run_fn=run_fn).passed


class TestErrorHandling:
    def test_agent_error_marks_failure(self):
        spec = _spec("q", "?", ["query_sheet"])
        run_fn = _stream([
            {"type": "error", "message": "agent looped"},
        ])
        result = eval_runner.run_question(spec, run_fn=run_fn)
        assert not result.passed
        assert result.error == "agent looped"


class TestToolArgs:
    """Per-question check: at-least-one call to TOOL had args matching CONSTRAINTS."""

    def _spec_with_args(self, expected_tool_args):
        return _spec(
            "q", "?", ["query_sheet"],
            expected_tool_args=expected_tool_args,
        )

    def test_matching_args_pass(self):
        spec = self._spec_with_args([{
            "tool": "query_sheet",
            "args_contain": {
                "filters.merchant_contains": "amazon",
                "filters.date_from": "2026-05-01",
            },
        }])
        run_fn = _stream([
            {"type": "tool_call", "name": "query_sheet",
             "args": {"filters": {"merchant_contains": "Amazon",
                                  "date_from": "2026-05-01",
                                  "date_to": "2026-05-31"}}},
            {"type": "answer", "text": "ok"},
        ])
        assert eval_runner.run_question(spec, run_fn=run_fn).passed

    def test_substring_case_insensitive(self):
        """Required 'amazon' must match actual 'AMAZON PAY INDIA'."""
        spec = self._spec_with_args([{
            "tool": "query_sheet",
            "args_contain": {"filters.merchant_contains": "amazon"},
        }])
        run_fn = _stream([
            {"type": "tool_call", "name": "query_sheet",
             "args": {"filters": {"merchant_contains": "AMAZON PAY INDIA"}}},
            {"type": "answer", "text": "ok"},
        ])
        assert eval_runner.run_question(spec, run_fn=run_fn).passed

    def test_wrong_filter_value_fails(self):
        """The motivating bug: agent passes category='dining' instead of 'Food & Dining'."""
        spec = self._spec_with_args([{
            "tool": "query_sheet",
            "args_contain": {"filters.category": "Food & Dining"},
        }])
        run_fn = _stream([
            {"type": "tool_call", "name": "query_sheet",
             "args": {"filters": {"category": "dining"}}},
            {"type": "answer", "text": "ok"},
        ])
        result = eval_runner.run_question(spec, run_fn=run_fn)
        assert not result.passed
        assert any("no 'query_sheet' call had args matching" in f for f in result.failures)

    def test_no_call_to_tool_fails(self):
        spec = self._spec_with_args([{
            "tool": "aggregate",
            "args_contain": {"op": "total_spent"},
        }])
        run_fn = _stream([
            {"type": "tool_call", "name": "query_sheet", "args": {}},
            {"type": "answer", "text": "ok"},
        ])
        result = eval_runner.run_question(spec, run_fn=run_fn)
        assert not result.passed
        assert any("no call to 'aggregate' found" in f for f in result.failures)

    def test_at_least_one_call_matches_passes(self):
        """Multi-call traces pass if ANY call to the tool satisfies the constraints."""
        spec = self._spec_with_args([{
            "tool": "query_sheet",
            "args_contain": {"filters.category": "Groceries"},
        }])
        run_fn = _stream([
            {"type": "tool_call", "name": "query_sheet",
             "args": {"filters": {"date_from": "2026-04-01"}}},  # no category
            {"type": "tool_call", "name": "query_sheet",
             "args": {"filters": {"category": "Groceries"}}},   # this one matches
            {"type": "answer", "text": "ok"},
        ])
        assert eval_runner.run_question(spec, run_fn=run_fn).passed

    def test_empty_constraints_pass_when_tool_called(self):
        """args_contain={} just asserts the tool was called at all."""
        spec = self._spec_with_args([{"tool": "query_sheet", "args_contain": {}}])
        run_fn = _stream([
            {"type": "tool_call", "name": "query_sheet", "args": {}},
            {"type": "answer", "text": "ok"},
        ])
        assert eval_runner.run_question(spec, run_fn=run_fn).passed

    def test_missing_field_is_opt_in(self):
        """No expected_tool_args => check is skipped; nothing fails."""
        spec = _spec("q", "?", ["query_sheet"])  # no expected_tool_args at all
        run_fn = _stream([
            {"type": "tool_call", "name": "query_sheet", "args": {}},
            {"type": "answer", "text": "ok"},
        ])
        assert eval_runner.run_question(spec, run_fn=run_fn).passed


class TestRunAll:
    def test_aggregates_results(self):
        spec_data = {
            "questions": [
                _spec("a", "?1", ["query_sheet"]),
                _spec("b", "?2", ["query_sheet"]),
            ]
        }
        run_fn = _stream([
            {"type": "tool_call", "name": "query_sheet"},
            {"type": "answer", "text": "ok"},
        ])
        results = eval_runner.run_all(spec_data, run_fn=run_fn)
        assert len(results) == 2
        assert all(r.passed for r in results)


class TestFormatSummary:
    def test_summary_lists_pass_and_fail(self):
        passing = eval_runner.QuestionResult(
            id="p", question="x", expected_tools=["q"],
            actual_tools=["q"], answer="42",
        )
        failing = eval_runner.QuestionResult(
            id="f", question="y", expected_tools=["q"],
            actual_tools=[], answer="",
            failures=["missing required tool ['q']"],
        )
        out = eval_runner.format_summary([passing, failing])
        assert "✓ p" in out
        assert "✗ f" in out
        assert "1/2 passed" in out
