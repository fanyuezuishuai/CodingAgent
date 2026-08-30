"""Regression guards for the coding-agent assignment constraints."""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = PROJECT_ROOT / "src" / "tracecoder"


def _load_runtime_modules() -> tuple[tuple[Path, str, ast.Module], ...]:
    modules: list[tuple[Path, str, ast.Module]] = []
    for path in sorted(RUNTIME_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        modules.append((path, source, ast.parse(source, filename=str(path))))
    return tuple(modules)


RUNTIME_MODULES = _load_runtime_modules()


def _distribution_name(requirement: str) -> str:
    name = re.split(r"[<>=!~;\s\[]", requirement, maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).casefold()


def _attribute_parts(node: ast.AST) -> tuple[str, ...]:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr.casefold())
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id.casefold())
    return tuple(reversed(parts))


def _provider_hosted_usages(tree: ast.AST) -> set[str]:
    prohibited_attributes = {
        ("assistants", "create"),
        ("files", "create"),
        ("threads", "runs"),
    }
    prohibited_tool_types = {"code_interpreter", "file_search"}
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            parts = _attribute_parts(node)
            for prohibited in prohibited_attributes:
                width = len(prohibited)
                if any(parts[index : index + width] == prohibited for index in range(len(parts))):
                    found.add(".".join(prohibited))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value.casefold()
            if value in prohibited_tool_types:
                found.add(value)
    return found


def test_runtime_dependencies_do_not_include_agent_frameworks() -> None:
    configuration = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = configuration["project"]
    requirements = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        requirements.extend(group)

    installed_names = {_distribution_name(requirement) for requirement in requirements}
    prohibited = {
        "autogen",
        "autogen-agentchat",
        "claude-agent-sdk",
        "crewai",
        "langchain",
        "llama-index",
        "openai-agents",
        "pyautogen",
    }

    assert installed_names.isdisjoint(prohibited)


def test_runtime_source_does_not_import_agent_frameworks() -> None:
    prohibited_roots = {
        "agents",
        "autogen",
        "claude_agent_sdk",
        "crewai",
        "langchain",
        "llama_index",
    }
    found: list[str] = []

    for path, _source, tree in RUNTIME_MODULES:
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".", maxsplit=1)[0].casefold() in prohibited_roots:
                    found.append(f"{path.relative_to(PROJECT_ROOT)} imports {name}")

    assert found == []


def test_runtime_source_does_not_call_provider_hosted_file_or_execution_tools() -> None:
    found = {
        f"{path.relative_to(PROJECT_ROOT)} uses {usage}"
        for path, _source, tree in RUNTIME_MODULES
        for usage in _provider_hosted_usages(tree)
    }

    assert found == set()


def test_hosted_provider_detection_is_independent_of_client_variable_name() -> None:
    tree = ast.parse(
        """
sdk.files.create()
provider.beta.assistants.create()
provider.beta.threads.runs.create()
tools = [{"type": "code_interpreter"}, {"type": "file_search"}]
"""
    )

    assert _provider_hosted_usages(tree) == {
        "assistants.create",
        "code_interpreter",
        "file_search",
        "files.create",
        "threads.runs",
    }


def test_submission_readme_stays_within_limit_and_contains_required_sections() -> None:
    submission = (PROJECT_ROOT / "README.txt").read_text(encoding="utf-8")

    assert len(submission) <= 1000
    assert "https://github.com/" in submission or "https://gitee.com/" in submission
    assert "tracecoder run" in submission
    assert "特色" in submission
