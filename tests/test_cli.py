"""Command-line boundary tests."""

from pathlib import Path

import pytest

from tracecoder.cli import build_parser, main, make_approval
from tracecoder.config import Settings
from tracecoder.evidence import write_proof_artifacts
from tracecoder.trace import TraceRecorder
from tracecoder.transaction import WorkspaceTransaction


def test_parser_exposes_run_and_trace_commands() -> None:
    parser = build_parser()

    run_args = parser.parse_args(["run", "fix it", "--workspace", ".", "--yes"])
    trace_args = parser.parse_args(["trace", "run.jsonl"])
    web_args = parser.parse_args(["web", "--workspace", ".", "--port", "9000", "--trust-proxy-auth"])
    transaction_args = parser.parse_args(["transaction", "rollback", "run-id", "--workspace", "."])

    assert run_args.command == "run"
    assert run_args.task == "fix it"
    assert trace_args.command == "trace"
    assert web_args.command == "web"
    assert web_args.port == 9000
    assert web_args.trust_proxy_auth is True
    assert run_args.scenario == "general"
    assert transaction_args.action == "rollback"
    assert transaction_args.transaction_id == "run-id"


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


def test_transaction_command_rolls_back_without_loading_model_configuration(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "course.py"
    target.write_text("before\n", encoding="utf-8")
    transaction = WorkspaceTransaction(tmp_path, "cli-run")
    transaction.prepare_file(target)
    target.write_text("after\n", encoding="utf-8")
    proof = {
        "run_id": "cli-run",
        "transaction": {
            "id": "cli-run",
            "state": "pending",
            "rollback_available": True,
            "scope": "file_tools_only",
        },
    }
    proof_path, _markdown_path = write_proof_artifacts(tmp_path, "cli-run", proof)

    exit_code = main(["transaction", "rollback", "cli-run", "--workspace", str(tmp_path)])

    assert exit_code == 0
    assert target.read_text(encoding="utf-8") == "before\n"
    assert "rolled_back" in capsys.readouterr().out
    assert '"state": "rolled_back"' in proof_path.read_text(encoding="utf-8")


def test_web_command_loads_workspace_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text(
        "TRACECODER_API_KEY=dotenv-secret\n"
        "TRACECODER_BASE_URL=https://api.deepseek.com\n"
        "TRACECODER_MODEL=deepseek-chat\n",
        encoding="utf-8",
    )
    observed: list[tuple[Path, Settings, str, int]] = []

    def fake_server(workspace: Path, settings: Settings, host: str, port: int) -> None:
        observed.append((workspace, settings, host, port))

    monkeypatch.setattr("tracecoder.cli.start_web_server", fake_server)

    exit_code = main(
        ["web", "--workspace", str(tmp_path), "--host", "0.0.0.0", "--trust-proxy-auth"]
    )

    assert exit_code == 0
    assert observed[0][0] == tmp_path.resolve()
    assert observed[0][1].model == "deepseek-chat"
    assert observed[0][2:] == ("0.0.0.0", 8765)


def test_web_rejects_non_loopback_without_trusted_proxy_acknowledgement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    started = False

    def fake_server(_workspace: Path, _settings: Settings, _host: str, _port: int) -> None:
        nonlocal started
        started = True

    monkeypatch.setattr("tracecoder.cli.start_web_server", fake_server)

    exit_code = main(["web", "--workspace", str(tmp_path), "--host", "0.0.0.0"])

    assert exit_code == 2
    assert started is False
    assert "--trust-proxy-auth" in capsys.readouterr().err
