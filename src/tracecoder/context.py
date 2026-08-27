"""Deterministic message-history compaction."""

from __future__ import annotations

import json

from tracecoder.domain import Message


class ContextManager:
    """Bound request size while preserving task and complete tool-call bundles."""

    def __init__(self, max_chars: int = 100_000) -> None:
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")
        self.max_chars = max_chars

    def prepare(self, messages: list[Message], runtime_summary: str) -> list[Message]:
        """Return the original history or a deterministic compacted copy."""

        copied = [dict(message) for message in messages]
        if _serialized_size(copied) <= self.max_chars or len(copied) <= 2:
            return copied

        pinned = copied[:2]
        fact_message: Message = {
            "role": "system",
            "content": "[Compacted runtime facts]\n" + runtime_summary,
        }
        budget_base = pinned + [fact_message]
        selected: list[list[Message]] = []
        for bundle in reversed(_bundle_history(copied[2:])):
            candidate = budget_base + [message for group in reversed(selected) for message in group] + bundle
            if selected and _serialized_size(candidate) > self.max_chars:
                break
            selected.append(bundle)
            if _serialized_size(candidate) > self.max_chars:
                break

        tail = [message for bundle in reversed(selected) for message in bundle]
        return budget_base + tail


def _bundle_history(messages: list[Message]) -> list[list[Message]]:
    bundles: list[list[Message]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        bundle = [message]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            index += 1
            while index < len(messages) and messages[index].get("role") == "tool":
                bundle.append(messages[index])
                index += 1
            bundles.append(bundle)
            continue
        bundles.append(bundle)
        index += 1
    return bundles


def _serialized_size(messages: list[Message]) -> int:
    return len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")))

