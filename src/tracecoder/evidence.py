"""Runtime-owned Proof Mode reports and local export helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from tracecoder.domain import JSONValue, ToolCall, ToolResult
from tracecoder.identifiers import validate_runtime_id
from tracecoder.transaction import TransactionError, WorkspaceTransaction


def command_evidence(
    call: ToolCall,
    result: ToolResult,
    redacted_result: dict[str, JSONValue],
) -> dict[str, JSONValue] | None:
    """Extract one executed command record from a redacted tool result."""

    if call.name != "run_command" or result.metadata.get("shell_side_effects_unknown") is not True:
        return None
    data = redacted_result.get("data")
    metadata = redacted_result.get("metadata")
    if not isinstance(data, dict) or not isinstance(metadata, dict):
        return None
    return {
        "argv": data.get("argv", []),
        "cwd": data.get("cwd", ""),
        "purpose": data.get("purpose", call.arguments.get("purpose", "work")),
        "ok": result.ok,
        "exit_code": data.get("exit_code"),
        "elapsed_seconds": data.get("elapsed_seconds"),
        "stdout": data.get("stdout", ""),
        "stderr": data.get("stderr", ""),
        "timed_out": metadata.get("timed_out", False),
        "stdout_truncated": metadata.get("stdout_truncated", False),
        "stderr_truncated": metadata.get("stderr_truncated", False),
    }


def build_proof(
    *,
    run_id: str,
    task: str,
    termination_reason: str,
    verification_status: str,
    changed_files: list[str],
    trace_path: str,
    steps: int,
    shell_side_effects_unknown: bool,
    commands: list[dict[str, JSONValue]],
    transaction: WorkspaceTransaction | None,
) -> dict[str, JSONValue]:
    """Build a provider-independent proof report from observed runtime state."""

    file_change_error: str | None = None
    try:
        file_changes = transaction.file_changes() if transaction is not None else []
    except (OSError, TransactionError) as exc:
        file_changes = []
        file_change_error = f"{type(exc).__name__}: {exc}"
    transaction_state = transaction.state if transaction is not None else "not_required"
    rollback_available = transaction.rollback_available if transaction is not None else False
    proof: dict[str, JSONValue] = {
        "schema_version": 1,
        "source": "tracecoder_runtime",
        "run_id": run_id,
        "task": task,
        "termination_reason": termination_reason,
        "verification_status": verification_status,
        "steps": steps,
        "changed_files": cast(JSONValue, list(changed_files)),
        "file_changes": cast(JSONValue, file_changes),
        "commands": cast(JSONValue, commands),
        "trace_path": trace_path,
        "shell_side_effects_unknown": shell_side_effects_unknown,
        "transaction": {
            "id": transaction.id if transaction is not None else None,
            "state": transaction_state,
            "rollback_available": rollback_available,
            "scope": "file_tools_only",
        },
    }
    if file_change_error is not None:
        proof["file_change_evidence_error"] = file_change_error
    return proof


def write_proof_artifacts(
    workspace: Path,
    run_id: str,
    proof: dict[str, JSONValue],
) -> tuple[Path, Path]:
    """Persist machine-readable and human-readable proof below `.tracecoder`."""

    validate_runtime_id(run_id, label="run_id")
    directory = workspace / ".tracecoder" / "proofs"
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{run_id}.json"
    markdown_path = directory / f"{run_id}.md"
    json_path.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_proof_markdown(proof), encoding="utf-8")
    return json_path, markdown_path


def render_proof_markdown(proof: dict[str, JSONValue]) -> str:
    """Render a compact evidence report without asking the model to summarize it."""

    lines = [
        "# TraceCoder Proof",
        "",
        f"- Run ID: `{proof.get('run_id', '')}`",
        f"- Termination: `{proof.get('termination_reason', '')}`",
        f"- Verification: `{proof.get('verification_status', '')}`",
        f"- Steps: `{proof.get('steps', '')}`",
        f"- Trace: `{proof.get('trace_path', '')}`",
        "",
        "## Task",
        "",
        str(proof.get("task", "")),
        "",
        "## File changes",
        "",
    ]
    file_changes = proof.get("file_changes")
    if isinstance(file_changes, list) and file_changes:
        for raw_change in file_changes:
            if not isinstance(raw_change, dict):
                continue
            lines.extend(
                [
                    f"### `{raw_change.get('path', '')}` ({raw_change.get('kind', 'changed')})",
                    "",
                ]
            )
            diff = raw_change.get("diff")
            if isinstance(diff, str):
                lines.extend(["````diff", diff.rstrip("\n"), "````", ""])
            else:
                lines.extend([f"Diff unavailable: `{raw_change.get('diff_unavailable_reason', 'unknown')}`", ""])
    else:
        lines.extend(["No file-tool changes were recorded.", ""])

    lines.extend(["## Commands", ""])
    commands = proof.get("commands")
    if isinstance(commands, list) and commands:
        for index, raw_command in enumerate(commands, start=1):
            if not isinstance(raw_command, dict):
                continue
            lines.extend(
                [
                    f"### Command {index}",
                    "",
                    f"- Purpose: `{raw_command.get('purpose', '')}`",
                    f"- Exit code: `{raw_command.get('exit_code', '')}`",
                    f"- Elapsed seconds: `{raw_command.get('elapsed_seconds', '')}`",
                    "",
                    "````json",
                    json.dumps(raw_command.get("argv", []), ensure_ascii=False),
                    "````",
                    "",
                ]
            )
            _append_command_stream(lines, "stdout", raw_command)
            _append_command_stream(lines, "stderr", raw_command)
    else:
        lines.extend(["No local command was executed.", ""])

    transaction = proof.get("transaction")
    if isinstance(transaction, dict):
        lines.extend(
            [
                "## Transaction",
                "",
                f"- State: `{transaction.get('state', 'not_required')}`",
                f"- Rollback available: `{str(transaction.get('rollback_available', False)).lower()}`",
                "- Scope: TraceCoder file tools only.",
                "",
            ]
        )
    if proof.get("shell_side_effects_unknown") is True:
        lines.extend(
            [
                "> Warning: at least one local command ran. Its arbitrary side effects are not covered by file-tool rollback.",
                "",
            ]
        )
    return "\n".join(lines)


def _append_command_stream(
    lines: list[str],
    stream: str,
    command: Mapping[str, object],
) -> None:
    value = command.get(stream)
    if not isinstance(value, str) or not value:
        return
    suffix = " (truncated)" if command.get(f"{stream}_truncated") is True else ""
    lines.extend([f"#### {stream}{suffix}", ""])
    lines.extend(f"    {line}" for line in value.rstrip("\n").split("\n"))
    lines.append("")
