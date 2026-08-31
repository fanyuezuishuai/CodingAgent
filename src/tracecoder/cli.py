"""TraceCoder command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from ipaddress import ip_address
from pathlib import Path

from tracecoder.config import ConfigError, Settings
from tracecoder.domain import JSONValue, TerminationReason
from tracecoder.evidence import write_proof_artifacts
from tracecoder.runtime import build_agent
from tracecoder.scenarios import apply_scenario
from tracecoder.trace import TraceFormatError, read_trace
from tracecoder.transaction import TransactionError, WorkspaceTransaction


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
    run_parser.add_argument(
        "--scenario",
        choices=["general", "repair", "generate"],
        default="general",
        help="Apply a bounded coursework workflow preset",
    )

    trace_parser = subparsers.add_parser("trace", help="Pretty-print a JSONL run trace")
    trace_parser.add_argument("path", type=Path, help="Path to a trace JSONL file")

    transaction_parser = subparsers.add_parser("transaction", help="Accept or roll back file-tool changes")
    transaction_parser.add_argument("action", choices=["accept", "rollback"])
    transaction_parser.add_argument("transaction_id", help="Run/transaction identifier")
    transaction_parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Workspace directory")

    web_parser = subparsers.add_parser("web", help="Start the local browser interface")
    web_parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Workspace directory (default: cwd)")
    web_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default: loopback only; non-loopback requires --trust-proxy-auth)",
    )
    web_parser.add_argument("--port", type=_tcp_port, default=8765, help="TCP port (default: 8765)")
    web_parser.add_argument(
        "--trust-proxy-auth",
        action="store_true",
        help="Acknowledge an authenticated reverse proxy protects a non-loopback bind (for example, a private forwarded port)",
    )
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
    if args.command == "transaction":
        return _run_transaction(args)
    if args.command == "web":
        return _run_web(args)
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
        settings = Settings.from_env(env_file=workspace / ".env")
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

    agent = build_agent(
        workspace,
        settings,
        make_approval(args.yes),
        max_steps=max_steps,
        repeat_limit=repeat_limit,
    )
    result = agent.run(apply_scenario(task, args.scenario))

    print("\nModel summary")
    print(result.final_text or "(no model text)")
    print("\nRuntime evidence")
    print(f"termination: {result.termination_reason.value}")
    print(f"verification: {result.verification_status.value}")
    print(f"known changed files: {', '.join(result.changed_files) if result.changed_files else '(none)'}")
    print(f"shell side effects unknown: {str(result.shell_side_effects_unknown).lower()}")
    print(f"trace: {result.trace_path}")
    if result.proof_json_path:
        print(f"proof (JSON): {result.proof_json_path}")
    if result.proof_markdown_path:
        print(f"proof (Markdown): {result.proof_markdown_path}")
    print(f"transaction: {result.transaction_state}")
    if result.rollback_available and result.transaction_id:
        print(
            "rollback: tracecoder transaction rollback "
            f"{result.transaction_id} --workspace {workspace}"
        )
        print(
            "accept:   tracecoder transaction accept "
            f"{result.transaction_id} --workspace {workspace}"
        )
    if result.termination_reason is TerminationReason.INTERRUPTED:
        return 130
    return 0 if result.successful else 1


def _run_web(args: argparse.Namespace) -> int:
    if not _is_loopback_host(args.host) and not args.trust_proxy_auth:
        print(
            "error: refusing non-loopback web binding without --trust-proxy-auth; "
            "use it only behind an authenticated reverse proxy (for example, a private forwarded port).",
            file=sys.stderr,
        )
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
        settings = Settings.from_env(env_file=workspace / ".env")
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    print(f"Provider:  {settings.base_url}")
    print(f"Model:     {settings.model}")
    print(f"Workspace: {workspace}")
    print(f"Web UI:    http://{args.host}:{args.port}")
    print("Data notice: prompts, requested source text, and command output are sent to this provider.")
    if not _is_loopback_host(args.host):
        print("Security acknowledgement: non-loopback binding relies on your authenticated reverse proxy.")
    start_web_server(workspace, settings, args.host, args.port)
    return 0


def _run_transaction(args: argparse.Namespace) -> int:
    try:
        workspace = args.workspace.resolve(strict=True)
    except FileNotFoundError:
        print(f"error: workspace does not exist: {args.workspace}", file=sys.stderr)
        return 2
    if not workspace.is_dir():
        print(f"error: workspace is not a directory: {workspace}", file=sys.stderr)
        return 2
    try:
        transaction = WorkspaceTransaction.load(workspace, args.transaction_id)
        outcome = transaction.rollback() if args.action == "rollback" else transaction.accept()
    except (TransactionError, ValueError) as exc:
        print(f"transaction error: {exc}", file=sys.stderr)
        return 2
    proof_path = workspace / ".tracecoder" / "proofs" / f"{args.transaction_id}.json"
    if proof_path.is_file():
        try:
            stored = json.loads(proof_path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                proof: dict[str, JSONValue] = stored
                transaction_proof = proof.get("transaction")
                if isinstance(transaction_proof, dict):
                    transaction_proof["state"] = str(outcome["state"])
                    transaction_proof["rollback_available"] = bool(outcome["rollback_available"])
                    write_proof_artifacts(workspace, args.transaction_id, proof)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"warning: transaction succeeded but proof export could not be updated: {exc}", file=sys.stderr)
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    return 0


def start_web_server(workspace: Path, settings: Settings, host: str, port: int) -> None:
    """Start the blocking ASGI server; kept separate for boundary tests."""

    import uvicorn

    from tracecoder.web import create_app

    uvicorn.run(create_app(workspace, settings), host=host, port=port, log_level="info")


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


def _tcp_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer between 1 and 65535") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("must be an integer between 1 and 65535")
    return port


def _is_loopback_host(host: str) -> bool:
    """Return whether a CLI host is localhost or an IP loopback address."""

    normalized = host.strip()
    if normalized.casefold() == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False
