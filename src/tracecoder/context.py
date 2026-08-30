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
        if _serialized_size(copied) <= self.max_chars:
            return copied
        if len(copied) <= 2:
            raise ValueError("system prompt and current request exceed the context budget")

        latest_user_index = next(
            (index for index in range(len(copied) - 1, 0, -1) if copied[index].get("role") == "user"),
            1,
        )
        if latest_user_index > 1:
            return self._prepare_multiturn(copied, latest_user_index, runtime_summary)

        pinned = copied[:2]
        fact_message: Message = {
            "role": "system",
            "content": "[Compacted runtime facts]\n" + runtime_summary,
        }
        budget_base = pinned + [fact_message]
        _require_budget_for_mandatory_messages(budget_base, self.max_chars)
        return budget_base + _select_recent_bundles(
            budget_base,
            _bundle_history(copied[2:]),
            self.max_chars,
        )

    def _prepare_multiturn(
        self,
        messages: list[Message],
        latest_user_index: int,
        runtime_summary: str,
    ) -> list[Message]:
        """Keep the current request and recent complete bundles within the configured budget."""

        fact_message: Message = {
            "role": "system",
            "content": "[Compacted runtime facts]\n" + runtime_summary,
        }
        current_request = messages[latest_user_index]
        current_base = [messages[0], fact_message, current_request]
        _require_budget_for_mandatory_messages(current_base, self.max_chars)
        current_tail = _select_recent_bundles(
            current_base,
            _bundle_history(messages[latest_user_index + 1 :]),
            self.max_chars,
        )
        prior_turns = _bundle_conversation_turns(messages[1:latest_user_index])
        if any(
            message.get("reasoning_content") is not None
            for turn in prior_turns
            for message in turn
        ):
            compacted_history = [
                message
                for turn in prior_turns
                for message in _compact_reasoning_history(turn)
            ]
            candidate = (
                [messages[0], fact_message]
                + compacted_history
                + [current_request]
                + current_tail
            )
            if _serialized_size(candidate) > self.max_chars:
                raise ValueError(
                    "prior conversation exceeds the context budget while preserving reasoning_content"
                )
            return candidate

        selected: list[list[Message]] = []
        for bundle in reversed(prior_turns):
            prior_tail = [message for group in reversed(selected) for message in group]
            candidate = [messages[0], fact_message] + bundle + prior_tail + [current_request] + current_tail
            if _serialized_size(candidate) > self.max_chars:
                break
            selected.append(bundle)

        prior_tail = [message for bundle in reversed(selected) for message in bundle]
        return [messages[0], fact_message] + prior_tail + [current_request] + current_tail


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


def _bundle_conversation_turns(messages: list[Message]) -> list[list[Message]]:
    """Group prior user turns so compaction never keeps an answer without its question."""

    turns: list[list[Message]] = []
    current: list[Message] = []
    for message in messages:
        if message.get("role") == "user" and current:
            turns.append(current)
            current = []
        current.append(message)
    if current:
        turns.append(current)
    return turns


def _select_recent_bundles(
    base: list[Message],
    bundles: list[list[Message]],
    max_chars: int,
) -> list[Message]:
    """Select recent complete bundles, compacting the newest one when necessary."""

    if any(message.get("reasoning_content") is not None for bundle in bundles for message in bundle):
        reasoning_chain = [message for bundle in bundles for message in bundle]
        if _serialized_size(base + reasoning_chain) <= max_chars:
            return reasoning_chain
        compacted_chain = [
            message for bundle in bundles for message in _compact_bundle(bundle)
        ]
        if _serialized_size(base + compacted_chain) <= max_chars:
            return compacted_chain
        raise ValueError(
            "current assistant chain exceeds the context budget while preserving reasoning_content"
        )

    selected: list[list[Message]] = []
    for bundle in reversed(bundles):
        tail = [message for group in reversed(selected) for message in group]
        if _serialized_size(base + bundle + tail) <= max_chars:
            selected.append(bundle)
            continue
        if not selected:
            compacted = _compact_bundle(bundle)
            if _serialized_size(base + compacted) <= max_chars:
                selected.append(compacted)
            elif any(message.get("role") == "assistant" and message.get("tool_calls") for message in bundle):
                raise ValueError("latest tool-call bundle exceeds the context budget")
        break
    return [message for bundle in reversed(selected) for message in bundle]


def _require_budget_for_mandatory_messages(messages: list[Message], max_chars: int) -> None:
    if _serialized_size(messages) > max_chars:
        raise ValueError("system prompt, runtime facts, and current request exceed the context budget")


def _compact_bundle(bundle: list[Message]) -> list[Message]:
    """Shrink bulky content without modifying provider-issued assistant protocol state."""

    compacted: list[Message] = []
    for message in bundle:
        copied = dict(message)
        content = copied.get("content")
        is_reasoning_assistant = (
            copied.get("role") == "assistant" and copied.get("reasoning_content") is not None
        )
        if isinstance(content, str) and content and not is_reasoning_assistant:
            copied["content"] = "[Content truncated to fit the context budget]"
        compacted.append(copied)
    return compacted


def _compact_reasoning_history(messages: list[Message]) -> list[Message]:
    """Shrink prior tool results while retaining the complete provider conversation state."""

    compacted: list[Message] = []
    for message in messages:
        copied = dict(message)
        content = copied.get("content")
        if copied.get("role") == "tool" and isinstance(content, str) and content:
            copied["content"] = "[Content truncated to fit the context budget]"
        compacted.append(copied)
    return compacted


def _serialized_size(messages: list[Message]) -> int:
    return len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")))
