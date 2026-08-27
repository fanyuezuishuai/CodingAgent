"""Provider-neutral model interface."""

from typing import Protocol

from tracecoder.domain import Message, ModelReply


class ModelProtocolError(RuntimeError):
    """Raised when a provider returns an unusable response shape."""


class ModelClient(Protocol):
    """Structural interface implemented by real and fake model clients."""

    def complete(self, messages: list[Message], tools: list[dict[str, object]]) -> ModelReply:
        """Return the next normalized model reply."""

        ...

