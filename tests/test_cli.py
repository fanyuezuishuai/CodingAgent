"""Command-line boundary tests."""

from pathlib import Path

from tracecoder.cli import build_parser, main, make_approval
from tracecoder.trace import TraceRecorder


def test_parser_exposes_run_and_trace_commands() -> None:
    parser = build_parser()

    run_args = parser.parse_args(["run", "fix it", "--workspace", ".", "--yes"])
    trace_args = parser.parse_args(["trace", "run.jsonl"])

    assert run_args.command == "run"
    assert run_args.task == "fix it"
    assert trace_args.command == "trace"


def test_invalid_workspace_fails_before_configuration(tmp_path: Path, capsys: object) -> None:
    exit_code = main(["run", "task", "--workspace", str(tmp_path / "missing")])

    assert exit_code == 2


def test_trace_command_prints_events(tmp_path: Path, capsys: object) -> None:
    recorder = TraceRecorder(tmp_path, session_id="cli-test")
    recorder.record("example", {"value": 1})

    exit_code = main(["trace", str(recorder.path)])

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert exit_code == 0
    assert "example" in captured.out


def test_auto_approval_accepts_exact_argv(tmp_path: Path) -> None:
    approval = make_approval(auto_approve=True)

    assert approval(["python", "-V"], tmp_path)

