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


def test_compaction_preserves_reasoning_and_tool_call_arguments_exactly() -> None:
    reasoning = "opaque-provider-state-" * 8
    arguments = '{"path":"current.py","line_start":1,"line_end":200}'
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "inspect the project"},
        {
            "role": "assistant",
            "content": "provider-visible assistant content",
            "reasoning_content": reasoning,
            "tool_calls": [
                {
                    "id": "current-call",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": arguments},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "current-call",
            "content": "large tool result " * 200,
        },
    ]

    prepared = ContextManager(max_chars=900).prepare(messages, "verification=not_required")

    assistant = prepared[-2]
    assert assistant["content"] == "provider-visible assistant content"
    assert assistant["reasoning_content"] == reasoning
    assert assistant["tool_calls"][0]["function"]["arguments"] == arguments  # type: ignore[index]
    assert "truncated" in str(prepared[-1]["content"]).casefold()


def test_latest_reasoning_tool_bundle_that_cannot_fit_fails_instead_of_being_dropped() -> None:
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "inspect the project"},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "private-state-" * 100,
            "tool_calls": [
                {
                    "id": "current-call",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"current.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "current-call", "content": "result"},
    ]

    with pytest.raises(ValueError, match="reasoning_content"):
        ContextManager(max_chars=350).prepare(messages, "verification=not_required")


def test_compaction_keeps_the_complete_current_reasoning_tool_chain() -> None:
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "inspect two files"},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "first-private-state-" * 5,
            "tool_calls": [
                {
                    "id": "first-call",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"first.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "first-call", "content": "first result " * 100},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "second-private-state-" * 5,
            "tool_calls": [
                {
                    "id": "second-call",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"second.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "second-call", "content": "second result " * 100},
    ]

    prepared = ContextManager(max_chars=1000).prepare(messages, "verification=not_required")

    reasoning_states = [
        message["reasoning_content"]
        for message in prepared
        if message.get("reasoning_content") is not None
    ]
    assert reasoning_states == ["first-private-state-" * 5, "second-private-state-" * 5]
    assert [
        message["tool_call_id"] for message in prepared if message.get("role") == "tool"
    ] == ["first-call", "second-call"]


def test_multiturn_compaction_keeps_complete_prior_reasoning_history() -> None:
    prior_question = "inspect the original file"
    prior_answer = "The original file was inspected."
    reasoning = "prior-turn-private-state-" * 6
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": prior_question},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": reasoning,
            "tool_calls": [
                {
                    "id": "prior-call",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"original.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "prior-call", "content": "large result " * 200},
        {
            "role": "assistant",
            "content": prior_answer,
            "reasoning_content": "prior-final-private-state",
        },
        {"role": "user", "content": "now inspect the replacement"},
    ]

    prepared = ContextManager(max_chars=1000).prepare(messages, "verification=not_required")

    assert any(message.get("content") == prior_question for message in prepared)
    assert any(message.get("content") == prior_answer for message in prepared)
    assert [
        message["reasoning_content"]
        for message in prepared
        if message.get("reasoning_content") is not None
    ] == [reasoning, "prior-final-private-state"]
    assert any(message.get("tool_call_id") == "prior-call" for message in prepared)
    assert any("truncated" in str(message.get("content", "")).casefold() for message in prepared)


def test_prior_reasoning_history_that_cannot_fit_fails_instead_of_being_dropped() -> None:
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "remember the first turn"},
        {
            "role": "assistant",
            "content": "first answer",
            "reasoning_content": "prior-private-state-" * 100,
        },
        {"role": "user", "content": "continue with the second turn"},
    ]

    with pytest.raises(ValueError, match="prior conversation.*reasoning_content"):
        ContextManager(max_chars=350).prepare(messages, "verification=not_required")
