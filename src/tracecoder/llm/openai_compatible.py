"""OpenAI-compatible Chat Completions adapter."""

from __future__ import annotations

import json
from typing import Any

from tracecoder.domain import Message, ModelReply, ToolCall
from tracecoder.llm.base import ModelProtocolError


class OpenAICompatibleClient:
    """Translate provider SDK responses into provider-neutral domain objects."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        *,
        client: object | None = None,
    ) -> None:
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("Install the 'openai' package to run a live model") from exc
            client = OpenAI(api_key=api_key, base_url=base_url)
        self._client: Any = client
        self.model = model

    def complete(self, messages: list[Message], tools: list[dict[str, object]]) -> ModelReply:
        """Call Chat Completions and normalize text plus function calls."""

        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
            )
            message = response.choices[0].message
        except (AttributeError, IndexError, TypeError) as exc:
            raise ModelProtocolError("Provider response has no usable assistant message") from exc

        calls: list[ToolCall] = []
        for raw_call in message.tool_calls or []:
            try:
                arguments = json.loads(raw_call.function.arguments)
                if not isinstance(arguments, dict):
                    raise ModelProtocolError("Tool arguments must decode to a JSON object")
                if not raw_call.id or not raw_call.function.name:
                    raise ModelProtocolError("Tool call is missing id or function name")
                calls.append(ToolCall(str(raw_call.id), str(raw_call.function.name), arguments))
            except json.JSONDecodeError as exc:
                raise ModelProtocolError("Tool arguments contain invalid JSON") from exc
        return ModelReply(content=message.content or "", tool_calls=tuple(calls))

