"""Tests for finance/agent.py — OpenAI mocked.

Verifies the loop's safety properties: identical-call guard, max-call ceiling,
no-data branch produces a clean answer, tool dispatch wires through correctly.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from finance import agent


def _llm_text(text: str) -> MagicMock:
    """A ChatCompletion that returns a plain text answer (no tool_calls)."""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = text
    resp.choices[0].message.tool_calls = None
    return resp


def _llm_tool(name: str, args: dict, call_id: str = "call_1") -> MagicMock:
    """A ChatCompletion that requests one tool call."""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    msg = resp.choices[0].message
    msg.content = None
    tc = MagicMock()
    tc.id = call_id
    tc.function.name = name
    tc.function.arguments = json.dumps(args)
    msg.tool_calls = [tc]
    return resp


def _drain(gen):
    return list(gen)


class TestHappyPath:
    def test_text_answer_short_circuits(self):
        client = MagicMock()
        client.chat.completions.create.return_value = _llm_text("Hello.")
        with patch.object(agent, "_get_client", return_value=client):
            events = _drain(agent.run("hi"))
        assert len(events) == 1
        assert events[0] == {"type": "answer", "text": "Hello."}
        assert client.chat.completions.create.call_count == 1

    def test_one_tool_call_then_answer(self):
        client = MagicMock()
        client.chat.completions.create.side_effect = [
            _llm_tool("query_sheet", {"filters": {"category": "Groceries"}}),
            _llm_text("You spent ₹2,000 on groceries."),
        ]
        with patch.object(agent, "_get_client", return_value=client), \
             patch("finance.tools.sheets.read_tab", return_value=[]):
            events = _drain(agent.run("groceries?"))

        types = [e["type"] for e in events]
        assert types == ["tool_call", "tool_result", "answer"]
        assert events[0]["name"] == "query_sheet"
        assert events[2]["text"] == "You spent ₹2,000 on groceries."


class TestIdenticalCallGuard:
    def test_same_tool_same_args_twice_aborts(self):
        # Both responses request the SAME tool with SAME args.
        same_call = _llm_tool("query_sheet", {"filters": {"category": "X"}}, call_id="a")
        same_again = _llm_tool("query_sheet", {"filters": {"category": "X"}}, call_id="b")
        client = MagicMock()
        client.chat.completions.create.side_effect = [same_call, same_again]

        with patch.object(agent, "_get_client", return_value=client), \
             patch("finance.tools.sheets.read_tab", return_value=[]):
            events = _drain(agent.run("?"))

        types = [e["type"] for e in events]
        # First: tool_call + tool_result (succeeds). Second: error before execution.
        assert types == ["tool_call", "tool_result", "error"]
        assert "looped" in events[-1]["message"].lower()


class TestMaxCallCeiling:
    def test_too_many_tool_calls_aborts(self):
        # Always return a (different) tool call so the loop never gets a text answer.
        # We vary the args to avoid the identical-call guard.
        responses = [
            _llm_tool("query_sheet", {"filters": {"category": f"X{i}"}}, call_id=f"c{i}")
            for i in range(agent.MAX_TOOL_CALLS + 2)
        ]
        client = MagicMock()
        client.chat.completions.create.side_effect = responses

        with patch.object(agent, "_get_client", return_value=client), \
             patch("finance.tools.sheets.read_tab", return_value=[]):
            events = _drain(agent.run("?"))

        # Should have made exactly MAX_TOOL_CALLS tool calls, then errored.
        tool_calls = [e for e in events if e["type"] == "tool_call"]
        assert len(tool_calls) == agent.MAX_TOOL_CALLS + 1  # final extra call still counted before ceiling
        errors = [e for e in events if e["type"] == "error"]
        assert errors
        assert "exceeded" in errors[-1]["message"].lower()


class TestNoDataBranch:
    def test_empty_query_result_does_not_crash(self):
        client = MagicMock()
        client.chat.completions.create.side_effect = [
            _llm_tool("query_sheet", {"filters": {"merchant_contains": "yacht"}}),
            _llm_text("No matching transactions."),
        ]
        with patch.object(agent, "_get_client", return_value=client), \
             patch("finance.tools.sheets.read_tab", return_value=[]):
            events = _drain(agent.run("yachts?"))

        # tool_result should report 0 rows
        result_event = next(e for e in events if e["type"] == "tool_result")
        assert "0 rows" in result_event["summary"]
        # The agent's text answer is preserved
        answer = next(e for e in events if e["type"] == "answer")
        assert "No matching" in answer["text"]


class TestToolDispatch:
    def test_unknown_tool_name_yields_error_in_result_but_loop_continues(self):
        client = MagicMock()
        client.chat.completions.create.side_effect = [
            _llm_tool("nonexistent_tool", {}),
            _llm_text("done"),
        ]
        with patch.object(agent, "_get_client", return_value=client):
            events = _drain(agent.run("?"))

        result = next(e for e in events if e["type"] == "tool_result")
        assert "ERROR" in result["summary"]
        # The agent then produced an answer, so it didn't crash.
        assert any(e["type"] == "answer" for e in events)

    def test_aggregate_actually_called(self):
        rows = [{"amount": "-100"}, {"amount": "-200"}]
        client = MagicMock()
        client.chat.completions.create.side_effect = [
            _llm_tool("aggregate", {"rows": rows, "op": "sum"}),
            _llm_text("₹300 total."),
        ]
        with patch.object(agent, "_get_client", return_value=client):
            events = _drain(agent.run("?"))

        result = next(e for e in events if e["type"] == "tool_result")
        assert "sum=-300.0" in result["summary"]
