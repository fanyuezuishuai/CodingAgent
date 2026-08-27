"""Deterministic context compaction tests."""

from tracecoder.context import ContextManager


def test_small_history_is_unchanged() -> None:
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
    ]

    prepared = ContextManager(max_chars=1000).prepare(messages, "no changes")

    assert prepared == messages


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

