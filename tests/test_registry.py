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


def test_registry_hides_and_blocks_tools_outside_run_allowlist() -> None:
    called = False

    def handler() -> object:
        nonlocal called
        called = True
        return object()

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="write_file",
            description="Write a file.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=handler,
        )
    )

    schemas = registry.schemas_for_model({"read_file"})
    result = registry.execute("write_file", {}, allowed_names={"read_file"})

    assert schemas == []
    assert result.error_code == "tool_not_allowed"
    assert not called
