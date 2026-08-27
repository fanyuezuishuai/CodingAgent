"""Local tool implementations and registry construction."""

from collections.abc import Callable
from pathlib import Path

from tracecoder.tools.filesystem import WorkspaceFileTools, WorkspacePolicy
from tracecoder.tools.registry import ToolDefinition, ToolRegistry
from tracecoder.tools.shell import RunCommandTool

__all__ = [
    "RunCommandTool",
    "ToolDefinition",
    "ToolRegistry",
    "WorkspaceFileTools",
    "WorkspacePolicy",
    "build_tool_registry",
]


def build_tool_registry(
    workspace: Path,
    approval: Callable[[list[str], Path], bool],
    *,
    default_timeout_seconds: int = 60,
    max_output_bytes: int = 20_000,
) -> ToolRegistry:
    """Create the six built-in tools for one canonical workspace."""

    policy = WorkspacePolicy(workspace)
    files = WorkspaceFileTools(policy)
    shell = RunCommandTool(
        policy.root,
        approval,
        default_timeout_seconds=default_timeout_seconds,
        max_output_bytes=max_output_bytes,
    )
    registry = ToolRegistry()

    object_schema = {"type": "object", "additionalProperties": False}
    registry.register(
        ToolDefinition(
            "list_files",
            "List files and directories inside the workspace.",
            {
                **object_schema,
                "properties": {
                    "path": {"type": "string"},
                    "recursive": {"type": "boolean"},
                    "max_entries": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
            },
            files.list_files,
        )
    )
    registry.register(
        ToolDefinition(
            "search_text",
            "Search for literal text inside workspace files.",
            {
                **object_schema,
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string"},
                    "pattern": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 500},
                },
                "required": ["query"],
            },
            files.search_text,
        )
    )
    registry.register(
        ToolDefinition(
            "read_file",
            "Read a bounded line range from a UTF-8 text file.",
            {
                **object_schema,
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "max_lines": {"type": "integer", "minimum": 1, "maximum": 2000},
                },
                "required": ["path"],
            },
            files.read_file,
        )
    )
    registry.register(
        ToolDefinition(
            "write_file",
            "Create or overwrite a UTF-8 text file in an existing workspace directory.",
            {
                **object_schema,
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "overwrite": {"type": "boolean"},
                },
                "required": ["path", "content"],
            },
            files.write_file,
        )
    )
    registry.register(
        ToolDefinition(
            "replace_text",
            "Replace text only when the exact expected match count is present.",
            {
                **object_schema,
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                    "expected_replacements": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
                "required": ["path", "old", "new"],
            },
            files.replace_text,
        )
    )
    registry.register(
        ToolDefinition(
            "run_command",
            "Run an approved argument vector locally with timeout and bounded output.",
            {
                **object_schema,
                "properties": {
                    "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "timeout_sec": {"type": "integer", "minimum": 1, "maximum": 600},
                    "purpose": {"type": "string", "enum": ["work", "verify"]},
                },
                "required": ["argv"],
            },
            shell,
        )
    )
    return registry

