"""Test doubles shared by agent-loop tests."""

from collections.abc import Sequence

from tracecoder.domain import ModelReply


class FakeModelClient:
    """Return a fixed sequence of model replies and capture requests."""

    def __init__(self, replies: Sequence[ModelReply]) -> None:
        self._replies = list(replies)
        self.requests: list[list[dict[str, object]]] = []

    def complete(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> ModelReply:
        self.requests.append(messages)
        if not self._replies:
            raise AssertionError("Fake model has no reply left")
        return self._replies.pop(0)

