"""Approved local command execution with timeout and bounded capture."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import BinaryIO, cast

from tracecoder.domain import JSONValue, ToolResult

ApprovalCallback = Callable[[list[str], Path], bool]
_CHILD_ENV_ALLOWLIST = {
    "APPDATA",
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
    "PATH",
    "PATHEXT",
    "PYTHONPATH",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "VIRTUAL_ENV",
    "WINDIR",
}


class _BoundedCapture:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.buffer = bytearray()
        self.truncated = False

    def drain(self, stream: BinaryIO) -> None:
        try:
            while chunk := stream.read(8192):
                remaining = self.limit - len(self.buffer)
                if remaining > 0:
                    self.buffer.extend(chunk[:remaining])
                if len(chunk) > max(remaining, 0):
                    self.truncated = True
        except (OSError, ValueError):
            return


class RunCommandTool:
    """Execute the exact approved argv with no implicit shell interpretation."""

    def __init__(
        self,
        workspace: Path,
        approval: ApprovalCallback,
        *,
        default_timeout_seconds: int = 60,
        max_output_bytes: int = 20_000,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.workspace = workspace.resolve(strict=True)
        self.approval = approval
        self.default_timeout_seconds = default_timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.environ = os.environ if environ is None else environ

    def __call__(
        self,
        argv: list[str],
        timeout_sec: int | None = None,
        purpose: str = "work",
    ) -> ToolResult:
        """Run one command and return structured output and timing evidence."""

        if not argv or any(not isinstance(argument, str) or not argument for argument in argv):
            return ToolResult.failure("invalid_arguments", "argv must contain non-empty strings")
        if purpose not in {"work", "verify"}:
            return ToolResult.failure("invalid_arguments", "purpose must be 'work' or 'verify'")
        timeout = self.default_timeout_seconds if timeout_sec is None else timeout_sec
        if timeout <= 0 or timeout > 600:
            return ToolResult.failure("invalid_arguments", "timeout_sec must be between 1 and 600")
        try:
            approved = self.approval(list(argv), self.workspace)
        except (EOFError, KeyboardInterrupt):
            approved = False
        if not approved:
            return ToolResult.failure("command_denied", "Command was not approved")

        started = time.monotonic()
        try:
            process = subprocess.Popen(
                argv,
                cwd=self.workspace,
                env=_minimal_child_environment(self.environ),
                shell=False,
                start_new_session=os.name != "nt",
                creationflags=(
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    if os.name == "nt"
                    else 0
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            return ToolResult.failure("command_not_found", f"Command not found: {argv[0]}")
        except OSError as exc:
            return ToolResult.failure("execution_error", f"Could not start command: {exc}")

        assert process.stdout is not None
        assert process.stderr is not None
        stdout_capture = _BoundedCapture(self.max_output_bytes)
        stderr_capture = _BoundedCapture(self.max_output_bytes)
        stdout_thread = threading.Thread(target=stdout_capture.drain, args=(process.stdout,), daemon=True)
        stderr_thread = threading.Thread(target=stderr_capture.drain, args=(process.stderr,), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        timed_out = False
        process_tree_terminated = False
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process_tree_terminated = _terminate_process_tree(process, self.environ)
            exit_code = process.wait()
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        if stdout_thread.is_alive():
            process.stdout.close()
            stdout_thread.join(timeout=0.2)
        if stderr_thread.is_alive():
            process.stderr.close()
            stderr_thread.join(timeout=0.2)
        elapsed = round(time.monotonic() - started, 4)

        data: dict[str, JSONValue] = {
            "argv": cast(list[JSONValue], list(argv)),
            "cwd": str(self.workspace),
            "stdout": bytes(stdout_capture.buffer).decode("utf-8", errors="replace"),
            "stderr": bytes(stderr_capture.buffer).decode("utf-8", errors="replace"),
            "exit_code": exit_code,
            "elapsed_seconds": elapsed,
            "purpose": purpose,
        }
        metadata: dict[str, JSONValue] = {
            "timed_out": timed_out,
            "stdout_truncated": stdout_capture.truncated,
            "stderr_truncated": stderr_capture.truncated,
            "shell_side_effects_unknown": True,
            "process_tree_terminated": process_tree_terminated,
        }
        if timed_out:
            return ToolResult.failure(
                "command_timeout",
                f"Command exceeded {timeout} seconds; process-tree termination was attempted",
                data=data,
                metadata=metadata,
            )
        if exit_code != 0:
            return ToolResult.failure(
                "command_failed",
                f"Command exited with code {exit_code}",
                data=data,
                metadata=metadata,
            )
        return ToolResult.success(data, metadata=metadata)


def _minimal_child_environment(environ: Mapping[str, str]) -> dict[str, str]:
    return {name: value for name, value in environ.items() if name.upper() in _CHILD_ENV_ALLOWLIST}


def _terminate_process_tree(process: subprocess.Popen[bytes], environ: Mapping[str, str]) -> bool:
    """Best-effort termination for the approved command and descendants."""

    if os.name == "nt":
        system_root = environ.get("SYSTEMROOT") or environ.get("WINDIR") or r"C:\Windows"
        taskkill = Path(system_root) / "System32" / "taskkill.exe"
        try:
            completed = subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            if completed.returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        kill_process_group = getattr(os, "killpg", None)
        kill_signal = getattr(signal, "SIGKILL", 9)
        try:
            if not callable(kill_process_group):
                raise OSError("process-group termination is unavailable")
            kill_process_group(process.pid, kill_signal)
            return True
        except OSError:
            pass

    try:
        process.kill()
    except OSError:
        return process.poll() is not None
    return False
