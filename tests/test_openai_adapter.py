"""OpenAI-compatible response boundary tests."""

from types import SimpleNamespace

import pytest

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

