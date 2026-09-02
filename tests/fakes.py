"""Test doubles shared by agent-loop tests."""

from collections.abc import Sequence

from tracecoder.domain import ModelReply, ToolCall


def plan_call(call_id: str, steps: list[str]) -> ToolCall:
    """Build the agent-owned plan control call used by scripted model replies."""

    return ToolCall(call_id, "update_plan", {"steps": steps})


class FakeModelClient:
    """Return a fixed sequence of model replies and capture requests."""

    def __init__(self, replies: Sequence[ModelReply]) -> None:
        self._replies = list(replies)
        self.requests: list[list[dict[str, object]]] = []
        self.tool_requests: list[list[dict[str, object]]] = []

    def complete(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> ModelReply:
        self.requests.append(messages)
        self.tool_requests.append(tools)
        if not self._replies:
            raise AssertionError("Fake model has no reply left")
        return self._replies.pop(0)
