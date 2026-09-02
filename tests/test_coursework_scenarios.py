"""Offline end-to-end demonstrations for the two coursework scenarios."""

import sys
from pathlib import Path

import pytest

from tests.fakes import FakeModelClient, plan_call
from tracecoder.agent import Agent
from tracecoder.context import ContextManager
from tracecoder.domain import ModelReply, ToolCall, VerificationStatus
from tracecoder.scenarios import apply_scenario
from tracecoder.tools import build_tool_registry
from tracecoder.trace import TraceRecorder
from tracecoder.transaction import WorkspaceTransaction


def test_unknown_coursework_scenario_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown scenario"):
        apply_scenario("task", "unsupported")  # type: ignore[arg-type]


def _transactional_agent(
    workspace: Path,
    session_id: str,
    replies: list[ModelReply],
) -> tuple[Agent, WorkspaceTransaction]:
    transaction = WorkspaceTransaction(workspace, session_id)
    trace = TraceRecorder(workspace, session_id=session_id)
    return (
        Agent(
            FakeModelClient(replies),
            build_tool_registry(workspace, lambda _argv, _cwd: True, transaction=transaction),
            ContextManager(),
            trace,
            transaction=transaction,
        ),
        transaction,
    )


def test_coursework_repair_scenario_fixes_verifies_proves_and_rolls_back(tmp_path: Path) -> None:
    project = tmp_path / "course_project"
    project.mkdir()
    calculator = project / "calculator.py"
    calculator.write_text("def add(left, right):\n    return left - right\n", encoding="utf-8")
    (project / "test_calculator.py").write_text(
        "import unittest\n"
        "from course_project.calculator import add\n\n"
        "class CalculatorTest(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )
    steps = ["Fix calculator.py", "Run the coursework test"]
    replies = [
        ModelReply(tool_calls=(ToolCall("inspect", "read_file", {"path": "course_project/calculator.py"}),)),
        ModelReply(
            tool_calls=(
                plan_call("plan-fix", steps),
                ToolCall(
                    "fix",
                    "replace_text",
                    {
                        "path": "course_project/calculator.py",
                        "old": "return left - right",
                        "new": "return left + right",
                    },
                ),
            )
        ),
        ModelReply(
            tool_calls=(
                ToolCall(
                    "verify",
                    "run_command",
                    {
                        "argv": [sys.executable, "-m", "unittest", "course_project.test_calculator"],
                        "purpose": "verify",
                    },
                ),
            )
        ),
        ModelReply(content="Fixed addition and passed the coursework test."),
    ]
    agent, transaction = _transactional_agent(tmp_path, "repair-demo", replies)

    result = agent.run(apply_scenario("Fix the addition function", "repair"))

    assert result.verification_status is VerificationStatus.COMMAND_PASSED
    assert result.successful
    assert result.proof is not None
    assert result.proof["file_changes"][0]["path"] == "course_project/calculator.py"
    assert result.proof["commands"][0]["exit_code"] == 0
    assert "left + right" in calculator.read_text(encoding="utf-8")
    transaction.rollback()
    assert "left - right" in calculator.read_text(encoding="utf-8")


def test_small_project_generation_creates_runnable_tested_project_and_rolls_back(tmp_path: Path) -> None:
    files = {
        "course_project/__init__.py": "\"\"\"Tiny generated course project.\"\"\"\n",
        "course_project/calculator.py": "def add(left: int, right: int) -> int:\n    return left + right\n",
        "course_project/main.py": (
            "from course_project.calculator import add\n\n"
            "if __name__ == '__main__':\n"
            "    print(add(2, 3))\n"
        ),
        "course_project/README.md": "# Calculator\n\nRun `python -m course_project.main`.\n",
        "course_project/tests/__init__.py": "",
        "course_project/tests/test_calculator.py": (
            "import unittest\n"
            "from course_project.calculator import add\n\n"
            "class CalculatorTest(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(add(2, 3), 5)\n"
        ),
    }
    steps = ["Create the project root", "Create the test directory", "Write project files", "Run tests"]
    replies = [
        ModelReply(
            tool_calls=(
                plan_call("plan-root", steps),
                ToolCall("root", "create_directory", {"path": "course_project"}),
            )
        ),
        ModelReply(
            tool_calls=(
                ToolCall("tests", "create_directory", {"path": "course_project/tests"}),
            )
        ),
        ModelReply(
            tool_calls=(
                *(
                    ToolCall(f"write-{index}", "write_file", {"path": path, "content": content})
                    for index, (path, content) in enumerate(files.items(), start=1)
                ),
            )
        ),
        ModelReply(
            tool_calls=(
                ToolCall(
                    "verify",
                    "run_command",
                    {
                        "argv": [
                            sys.executable,
                            "-B",
                            "-m",
                            "unittest",
                            "discover",
                            "-s",
                            "course_project/tests",
                        ],
                        "purpose": "verify",
                    },
                ),
            )
        ),
        ModelReply(content="Generated a small runnable project with tests."),
    ]
    agent, transaction = _transactional_agent(tmp_path, "generate-demo", replies)

    result = agent.run(
        apply_scenario("Topic: calculator; target directory: course_project", "generate")
    )

    assert result.successful
    assert result.verification_status is VerificationStatus.COMMAND_PASSED
    assert len(result.changed_files) == 6
    assert all((tmp_path / path).is_file() for path in files)
    assert result.proof is not None
    assert len(result.proof["file_changes"]) == 6
    assert result.proof["commands"][0]["exit_code"] == 0
    transaction.rollback()
    assert not (tmp_path / "course_project").exists()
