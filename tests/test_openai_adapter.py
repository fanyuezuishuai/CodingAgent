"""OpenAI-compatible response boundary tests."""

import json
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI

from tracecoder.llm.base import ModelProtocolError
from tracecoder.llm.openai_compatible import OpenAICompatibleClient


class FakeCompletions:
    def __init__(self, response: object) -> None:
        self.response = response
        self.kwargs: dict[str, object] = {}

    def create(self, **kwargs: object) -> object:
        self.kwargs = kwargs
        return self.response


def _client_for(message: object) -> tuple[OpenAICompatibleClient, FakeCompletions]:
    completions = FakeCompletions(SimpleNamespace(choices=[SimpleNamespace(message=message)]))
    sdk_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return OpenAICompatibleClient("key", "https://provider.example/v1", "model", client=sdk_client), completions


def test_adapter_normalizes_multiple_tool_calls() -> None:
    message = SimpleNamespace(
        content=None,
        tool_calls=[
            SimpleNamespace(
                id="call-1",
                function=SimpleNamespace(name="read_file", arguments='{"path":"a.py"}'),
            ),
            SimpleNamespace(
                id="call-2",
                function=SimpleNamespace(name="list_files", arguments="{}"),
            ),
        ],
    )
    adapter, completions = _client_for(message)

    reply = adapter.complete([{"role": "user", "content": "task"}], [])

    assert [call.id for call in reply.tool_calls] == ["call-1", "call-2"]
    assert reply.tool_calls[0].arguments == {"path": "a.py"}
    assert completions.kwargs["model"] == "model"


def test_adapter_preserves_optional_reasoning_content() -> None:
    message = SimpleNamespace(
        content=None,
        reasoning_content="opaque-provider-state",
        tool_calls=[],
    )
    adapter, _ = _client_for(message)

    reply = adapter.complete([], [])

    assert reply.reasoning_content == "opaque-provider-state"


def test_adapter_supports_providers_without_reasoning_content() -> None:
    message = SimpleNamespace(content="ordinary reply", tool_calls=[])
    adapter, _ = _client_for(message)

    reply = adapter.complete([], [])

    assert reply.reasoning_content is None


def test_openai_sdk_serializes_reasoning_content_in_followup_request() -> None:
    captured_body: dict[str, object] = {}

    def handle_request(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "completion-1",
                "object": "chat.completion",
                "created": 0,
                "model": "deepseek-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "done"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle_request)) as http_client:
        sdk_client = OpenAI(
            api_key="test-key",
            base_url="https://provider.example/v1",
            http_client=http_client,
        )
        adapter = OpenAICompatibleClient(
            "test-key",
            "https://provider.example/v1",
            "deepseek-model",
            client=sdk_client,
        )
        adapter.complete(
            [
                {"role": "user", "content": "inspect a.py"},
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "opaque-provider-state",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path":"a.py"}',
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call-1", "content": "file contents"},
            ],
            [],
        )

    messages = captured_body["messages"]
    assert isinstance(messages, list)
    assert messages[1]["reasoning_content"] == "opaque-provider-state"


def test_adapter_rejects_non_string_reasoning_content() -> None:
    message = SimpleNamespace(content="", reasoning_content={"unexpected": True}, tool_calls=[])
    adapter, _ = _client_for(message)

    with pytest.raises(ModelProtocolError, match="reasoning_content"):
        adapter.complete([], [])


def test_adapter_rejects_non_object_arguments() -> None:
    message = SimpleNamespace(
        content="",
        tool_calls=[
            SimpleNamespace(id="bad", function=SimpleNamespace(name="read_file", arguments="[]"))
        ],
    )
    adapter, _ = _client_for(message)

    with pytest.raises(ModelProtocolError, match="JSON object"):
        adapter.complete([], [])
