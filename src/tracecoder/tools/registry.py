"""Tool definitions, argument validation, and safe dispatch."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from tracecoder.domain import JSONValue, ToolResult

ToolHandler = Callable[..., ToolResult]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """One callable tool plus its model-visible JSON Schema."""

    name: str
    description: str
    parameters: dict[str, object]
    handler: ToolHandler


class ToolRegistry:
    """Own tool schemas, validation, and exception-to-result conversion."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        """Register one uniquely named tool."""

        if definition.name in self._tools:
            raise ValueError(f"Tool already registered: {definition.name}")
        self._tools[definition.name] = definition

    def schemas_for_model(self) -> list[dict[str, object]]:
        """Return OpenAI-compatible function-tool definitions."""

        return [
            {
                "type": "function",
                "function": {
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": definition.parameters,
                },
            }
            for definition in self._tools.values()
        ]

    def execute(self, name: str, arguments: Mapping[str, JSONValue]) -> ToolResult:
        """Validate and execute a tool without leaking exceptions into the loop."""

        definition = self._tools.get(name)
        if definition is None:
            return ToolResult.failure("unknown_tool", f"Unknown tool: {name}")

        validation_error = _validate_object(arguments, definition.parameters)
        if validation_error:
            return ToolResult.failure("invalid_arguments", validation_error)

        try:
            return definition.handler(**dict(arguments))
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:  # Tool failures are data for the model, not loop crashes.
            return ToolResult.failure("execution_error", f"{type(exc).__name__}: {exc}")


def _validate_object(arguments: Mapping[str, JSONValue], schema: Mapping[str, object]) -> str | None:
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        return "Tool schema has invalid properties"
    required = schema.get("required", [])
    if not isinstance(required, list):
        return "Tool schema has invalid required list"

    for name in required:
        if name not in arguments:
            return f"Missing required argument: {name}"
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            return f"Unknown argument(s): {', '.join(unknown)}"

    for name, value in arguments.items():
        property_schema = properties.get(name)
        if isinstance(property_schema, Mapping):
            error = _validate_value(name, value, property_schema)
            if error:
                return error
    return None


def _validate_value(name: str, value: JSONValue, schema: Mapping[str, object]) -> str | None:
    expected = schema.get("type")
    valid = {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
    }.get(str(expected), True)
    if not valid:
        return f"Argument '{name}' must be {expected}"

    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return f"Argument '{name}' must be one of: {enum}"
    if isinstance(value, int) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, int) and value < minimum:
            return f"Argument '{name}' must be >= {minimum}"
        if isinstance(maximum, int) and value > maximum:
            return f"Argument '{name}' must be <= {maximum}"
    if isinstance(value, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            return f"Argument '{name}' requires at least {min_items} item(s)"
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                error = _validate_value(f"{name}[{index}]", item, item_schema)
                if error:
                    return error
    return None

