"""Deterministic context compaction tests."""

import json

import pytest

from tracecoder.context import ContextManager


def test_small_history_is_unchanged() -> None:
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
    ]

    prepared = ContextManager(max_chars=1000).prepare(messages, "no changes")

    assert prepared == messages


def test_mandatory_messages_that_exceed_budget_fail_instead_of_leaking_past_limit() -> None:
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system" * 20},
        {"role": "user", "content": "current request" * 20},
    ]

    with pytest.raises(ValueError, match="exceed the context budget"):
        ContextManager(max_chars=100).prepare(messages, "no changes")


def test_compaction_keeps_task_runtime_facts_and_complete_tool_bundle() -> None:
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "fix the project"},
        {"role": "assistant", "content": "old" * 1000},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
    ]

    prepared = ContextManager(max_chars=900).prepare(messages, "changed=a.py; verification=required")

    assert prepared[0:2] == messages[0:2]
    assert "changed=a.py" in str(prepared[2]["content"])
    assert prepared[-2]["role"] == "assistant"
    assert prepared[-1]["tool_call_id"] == "call-1"


def test_multiturn_compaction_keeps_latest_user_and_current_tool_bundle() -> None:
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer" * 500},
        {"role": "user", "content": "current question"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "current-call",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"current.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "current-call", "content": "current result" * 100},
    ]

    prepared = ContextManager(max_chars=600).prepare(messages, "verification=not_required")

    assert any(message.get("content") == "current question" for message in prepared)
    assert prepared[-2]["role"] == "assistant"
    assert prepared[-1]["tool_call_id"] == "current-call"
    assert "truncated" in str(prepared[-1]["content"]).casefold()
    assert len(json.dumps(prepared, ensure_ascii=False, separators=(",", ":"))) <= 600
