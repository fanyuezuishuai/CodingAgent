"""Tool registry validation tests."""

from tracecoder.tools.registry import ToolDefinition, ToolRegistry


def test_registry_rejects_unknown_tool() -> None:
    registry = ToolRegistry()

    result = registry.execute("missing", {})

    assert result.error_code == "unknown_tool"


def test_registry_rejects_bad_arguments_before_handler() -> None:
    called = False

    def handler(name: str) -> object:
        nonlocal called
        called = True
        return name

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="hello",
            description="Say hello.",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
            handler=handler,
        )
    )

    result = registry.execute("hello", {"name": 123})

    assert result.error_code == "invalid_arguments"
    assert not called

