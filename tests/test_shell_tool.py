"""Approved command execution tests."""

import os
import sys
from pathlib import Path

from tracecoder.tools.shell import RunCommandTool


def test_denied_command_never_starts(tmp_path: Path) -> None:
    tool = RunCommandTool(tmp_path, approval=lambda _argv, _cwd: False)

    result = tool([sys.executable, "-c", "raise SystemExit(99)"])

    assert result.error_code == "command_denied"


def test_command_uses_argument_vector_without_shell_interpretation(tmp_path: Path) -> None:
    tool = RunCommandTool(tmp_path, approval=lambda _argv, _cwd: True)

    result = tool([sys.executable, "-c", "import sys; print(sys.argv[1])", "&&"])

    assert result.ok
    assert result.data["stdout"].strip() == "&&"


def test_command_timeout_is_structured(tmp_path: Path) -> None:
    tool = RunCommandTool(tmp_path, approval=lambda _argv, _cwd: True, default_timeout_seconds=1)

    result = tool([sys.executable, "-c", "import time; time.sleep(5)"])

    assert result.error_code == "command_timeout"
    assert result.metadata["timed_out"] is True


def test_child_environment_does_not_receive_api_key(tmp_path: Path, monkeypatch: object) -> None:
    os.environ["TRACECODER_API_KEY"] = "sentinel-secret"
    try:
        tool = RunCommandTool(tmp_path, approval=lambda _argv, _cwd: True)
        result = tool(
            [
                sys.executable,
                "-c",
                "import os; print(os.environ.get('TRACECODER_API_KEY', 'missing'))",
            ]
        )
    finally:
        os.environ.pop("TRACECODER_API_KEY", None)

    assert result.ok
    assert result.data["stdout"].strip() == "missing"


def test_command_output_is_capped_while_process_runs(tmp_path: Path) -> None:
    tool = RunCommandTool(tmp_path, approval=lambda _argv, _cwd: True, max_output_bytes=128)

    result = tool([sys.executable, "-c", "print('x' * 4096)"])

    assert result.ok
    assert len(result.data["stdout"].encode()) <= 128
    assert result.metadata["stdout_truncated"] is True

