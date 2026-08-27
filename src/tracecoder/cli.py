"""TraceCoder command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from tracecoder.agent import Agent
from tracecoder.config import ConfigError, Settings
from tracecoder.context import ContextManager
from tracecoder.domain import TerminationReason
from tracecoder.llm.openai_compatible import OpenAICompatibleClient
from tracecoder.tools import build_tool_registry
from tracecoder.trace import TraceFormatError, TraceRecorder, read_trace


def build_parser() -> argparse.ArgumentParser:
    """Build the complete CLI parser without loading credentials."""

    parser = argparse.ArgumentParser(
        prog="tracecoder",
        description="A small, local, traceable coding agent.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run one coding task")
    run_parser.add_argument("task", help="Natural-language programming task")
    run_parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Workspace directory (default: cwd)")
    run_parser.add_argument("--yes", action="store_true", help="Auto-approve commands with your full host permissions")
    run_parser.add_argument("--max-steps", type=_positive_cli_int, help="Override the configured model-step budget")
    run_parser.add_argument("--repeat-limit", type=_positive_cli_int, help="Override repeated-call termination threshold")

    trace_parser = subparsers.add_parser("trace", help="Pretty-print a JSONL run trace")
    trace_parser.add_argument("path", type=Path, help="Path to a trace JSONL file")
    return parser


def make_approval(auto_approve: bool) -> Callable[[list[str], Path], bool]:
    """Build an exact-argv command approval callback."""

    if auto_approve:
        return lambda _argv, _cwd: True

    def approve(argv: list[str], cwd: Path) -> bool:
        print("\nCommand approval required")
        print(f"cwd:  {cwd}")
        print(f"argv: {json.dumps(argv, ensure_ascii=False)}")
        try:
            answer = input("Run this command? [y/N] ").strip().casefold()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return answer in {"y", "yes"}

    return approve


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""

    args = build_parser().parse_args(argv)
    if args.command == "trace":
        return _show_trace(args.path)
    return _run_task(args)


def _run_task(args: argparse.Namespace) -> int:
    task = args.task.strip()
    if not task:
        print("error: task must not be empty", file=sys.stderr)
        return 2
    try:
        workspace = args.workspace.resolve(strict=True)
    except FileNotFoundError:
        print(f"error: workspace does not exist: {args.workspace}", file=sys.stderr)
        return 2
    if not workspace.is_dir():
        print(f"error: workspace is not a directory: {workspace}", file=sys.stderr)
        return 2
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    max_steps = args.max_steps or settings.max_steps
    repeat_limit = args.repeat_limit or settings.repeat_limit
    print(f"Provider:  {settings.base_url}")
    print(f"Model:     {settings.model}")
    print(f"Workspace: {workspace}")
    print("Data notice: prompts, requested source text, and command output are sent to this provider.")
    if args.yes:
        print("Command mode: auto-approved; commands receive your current host permissions.")
    else:
        print("Command mode: interactive approval of exact argv and cwd.")

    trace = TraceRecorder(workspace, secrets=[settings.api_key])
    registry = build_tool_registry(
        workspace,
        make_approval(args.yes),
        default_timeout_seconds=settings.command_timeout_seconds,
        max_output_bytes=settings.command_output_bytes,
    )
    model = OpenAICompatibleClient(settings.api_key, settings.base_url, settings.model)
    agent = Agent(
        model,
        registry,
        ContextManager(settings.context_max_chars),
        trace,
        max_steps=max_steps,
        repeat_limit=repeat_limit,
    )
    result = agent.run(task)

    print("\nModel summary")
    print(result.final_text or "(no model text)")
    print("\nRuntime evidence")
    print(f"termination: {result.termination_reason.value}")
    print(f"verification: {result.verification_status.value}")
    print(f"known changed files: {', '.join(result.changed_files) if result.changed_files else '(none)'}")
    print(f"shell side effects unknown: {str(result.shell_side_effects_unknown).lower()}")
    print(f"trace: {result.trace_path}")
    if result.termination_reason is TerminationReason.INTERRUPTED:
        return 130
    return 0 if result.successful else 1


def _show_trace(path: Path) -> int:
    try:
        events = read_trace(path)
    except TraceFormatError as exc:
        print(f"trace error: {exc}", file=sys.stderr)
        return 2
    for event in events:
        print(json.dumps(event, ensure_ascii=False, indent=2))
    return 0


def _positive_cli_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number

