"""Local web application integration tests."""

import time
from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Any, Protocol, cast

from fastapi.testclient import TestClient

from tests.fakes import FakeModelClient
from tracecoder.agent import Agent
from tracecoder.config import Settings
from tracecoder.context import ContextManager
from tracecoder.domain import ModelReply, RunResult, TerminationReason, ToolCall, VerificationStatus
from tracecoder.tools import build_tool_registry
from tracecoder.trace import TraceRecorder, read_trace
from tracecoder.web import RunManager, RunNotFoundError, create_app


class RunnableAgent(Protocol):
    def run(self, task: str) -> RunResult: ...


def _settings() -> Settings:
    return Settings.from_env(
        {
            "TRACECODER_API_KEY": "web-secret",
            "TRACECODER_BASE_URL": "https://provider.example/v1",
            "TRACECODER_MODEL": "web-model",
        }
    )


def _wait_for_status(client: TestClient, run_id: str, expected: set[str]) -> dict[str, Any]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{run_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in expected:
            return cast(dict[str, Any], payload)
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} did not reach {expected}")


def test_web_serves_ui_and_public_configuration(tmp_path: Path) -> None:
    app = create_app(tmp_path, _settings())

    with TestClient(app) as client:
        page = client.get("/")
        config = client.get("/api/config")

    assert page.status_code == 200
    assert "TraceCoder" in page.text
    assert config.json() == {
        "workspace": str(tmp_path.resolve()),
        "provider": "https://provider.example/v1",
        "model": "web-model",
    }
    assert "web-secret" not in page.text
    assert "web-secret" not in config.text


def test_web_run_streams_trace_events_and_result(tmp_path: Path) -> None:
    class FakeAgent:
        def __init__(self, observer: Callable[[dict[str, object]], None]) -> None:
            self.observer = observer

        def run(self, task: str) -> RunResult:
            self.observer(
                {
                    "event_type": "model_reply",
                    "payload": {"content": f"working on {task}", "tool_calls": []},
                }
            )
            return RunResult(
                final_text="Finished safely.",
                termination_reason=TerminationReason.COMPLETED,
                verification_status=VerificationStatus.NOT_REQUIRED,
                changed_files=(),
                trace_path=str(tmp_path / "trace.jsonl"),
                steps=1,
            )

    def factory(
        _approval: Callable[[list[str], Path], bool],
        observer: Callable[[dict[str, object]], None],
        _cancelled: Callable[[], bool],
        _session_id: str,
    ) -> RunnableAgent:
        return FakeAgent(observer)

    app = create_app(tmp_path, _settings(), agent_factory=factory)
    with TestClient(app) as client:
        started = client.post("/api/runs", json={"task": "inspect files"})
        assert started.status_code == 201
        run_id = started.json()["id"]
        payload = _wait_for_status(client, run_id, {"finished"})

    assert payload["result"]["final_text"] == "Finished safely."
    assert any(event["event_type"] == "model_reply" for event in payload["events"])


def test_web_integrates_real_agent_trace_and_run_manager(tmp_path: Path) -> None:
    def factory(
        approval: Callable[[list[str], Path], bool],
        observer: Callable[[dict[str, object]], None],
        cancelled: Callable[[], bool],
        session_id: str,
    ) -> RunnableAgent:
        return Agent(
            FakeModelClient([ModelReply(content="Repository inspected.")]),
            build_tool_registry(tmp_path, approval),
            ContextManager(),
            TraceRecorder(tmp_path, session_id=session_id, observer=observer),
            cancelled=cancelled,
        )

    app = create_app(tmp_path, _settings(), agent_factory=factory)
    with TestClient(app) as client:
        started = client.post("/api/runs", json={"task": "inspect the repository"})
        payload = _wait_for_status(client, started.json()["id"], {"finished"})

    event_types = [event["event_type"] for event in payload["events"]]
    assert event_types == ["run_started", "model_reply", "run_finished"]
    assert payload["result"]["successful"] is True


def test_web_command_approval_rejects_wrong_and_stale_ids_and_denial(tmp_path: Path) -> None:
    decision_seen: list[bool] = []

    class ApprovalAgent:
        def __init__(self, approval: Callable[[list[str], Path], bool]) -> None:
            self.approval = approval

        def run(self, _task: str) -> RunResult:
            decision_seen.append(self.approval(["python", "-V"], tmp_path))
            return RunResult(
                final_text="Approval handled.",
                termination_reason=TerminationReason.COMPLETED,
                verification_status=VerificationStatus.NOT_REQUIRED,
                changed_files=(),
                trace_path=str(tmp_path / "trace.jsonl"),
                steps=1,
            )

    def factory(
        approval: Callable[[list[str], Path], bool],
        _observer: Callable[[dict[str, object]], None],
        _cancelled: Callable[[], bool],
        _session_id: str,
    ) -> RunnableAgent:
        return ApprovalAgent(approval)

    app = create_app(tmp_path, _settings(), agent_factory=factory)
    with TestClient(app) as client:
        first = client.post("/api/runs", json={"task": "run a command"})
        run_id = first.json()["id"]
        waiting = _wait_for_status(client, run_id, {"waiting_approval"})
        approval = waiting["approval"]
        assert approval["argv"] == ["python", "-V"]

        wrong = client.post(
            f"/api/runs/{run_id}/approval",
            json={"approval_id": "not-the-pending-id", "approved": True},
        )
        assert wrong.status_code == 409
        assert decision_seen == []

        second = client.post("/api/runs", json={"task": "must not overlap"})
        assert second.status_code == 409

        denied = client.post(
            f"/api/runs/{run_id}/approval",
            json={"approval_id": approval["id"], "approved": False},
        )
        assert denied.status_code == 200
        payload = _wait_for_status(client, run_id, {"finished"})
        stale = client.post(
            f"/api/runs/{run_id}/approval",
            json={"approval_id": approval["id"], "approved": True},
        )
        assert stale.status_code == 409

    assert decision_seen == [False]
    assert payload["approval"] is None


def test_web_can_rediscover_the_active_pending_approval(tmp_path: Path) -> None:
    release = Event()

    class ApprovalAgent:
        def __init__(self, approval: Callable[[list[str], Path], bool]) -> None:
            self.approval = approval

        def run(self, _task: str) -> RunResult:
            self.approval(["python", "-V"], tmp_path)
            release.wait(timeout=1)
            return RunResult(
                final_text="Done.",
                termination_reason=TerminationReason.COMPLETED,
                verification_status=VerificationStatus.NOT_REQUIRED,
                changed_files=(),
                trace_path=str(tmp_path / "trace.jsonl"),
                steps=1,
            )

    def factory(
        approval: Callable[[list[str], Path], bool],
        _observer: Callable[[dict[str, object]], None],
        _cancelled: Callable[[], bool],
        _session_id: str,
    ) -> RunnableAgent:
        return ApprovalAgent(approval)

    app = create_app(tmp_path, _settings(), agent_factory=factory)
    with TestClient(app) as client:
        started = client.post("/api/runs", json={"task": "run a command"})
        run_id = started.json()["id"]
        waiting = _wait_for_status(client, run_id, {"waiting_approval"})
        active = client.get("/api/runs/active")

        assert active.status_code == 200
        assert active.json()["id"] == run_id
        assert active.json()["approval"] == waiting["approval"]

        approval = waiting["approval"]
        client.post(
            f"/api/runs/{run_id}/approval",
            json={"approval_id": approval["id"], "approved": False},
        )
        release.set()
        _wait_for_status(client, run_id, {"finished"})

        assert client.get("/api/runs/active").json() is None


def test_web_cancellation_releases_pending_approval_without_approval(tmp_path: Path) -> None:
    decision_seen: list[bool] = []
    approval_released = Event()

    class ApprovalAgent:
        def __init__(self, approval: Callable[[list[str], Path], bool]) -> None:
            self.approval = approval

        def run(self, _task: str) -> RunResult:
            decision_seen.append(self.approval(["python", "-V"], tmp_path))
            approval_released.set()
            return RunResult(
                final_text="Interrupted.",
                termination_reason=TerminationReason.INTERRUPTED,
                verification_status=VerificationStatus.NOT_REQUIRED,
                changed_files=(),
                trace_path=str(tmp_path / "trace.jsonl"),
                steps=0,
            )

    def factory(
        approval: Callable[[list[str], Path], bool],
        _observer: Callable[[dict[str, object]], None],
        _cancelled: Callable[[], bool],
        _session_id: str,
    ) -> RunnableAgent:
        return ApprovalAgent(approval)

    app = create_app(tmp_path, _settings(), agent_factory=factory)
    with TestClient(app) as client:
        started = client.post("/api/runs", json={"task": "run a command"})
        run_id = started.json()["id"]
        _wait_for_status(client, run_id, {"waiting_approval"})
        cancelled = client.post(f"/api/runs/{run_id}/cancel")

        assert cancelled.status_code == 200
        assert approval_released.wait(timeout=1)
        payload = _wait_for_status(client, run_id, {"interrupted"})

    assert decision_seen == [False]
    assert payload["approval"] is None


def test_web_persists_control_events_to_the_real_agent_trace(tmp_path: Path) -> None:
    def factory(
        approval: Callable[[list[str], Path], bool],
        observer: Callable[[dict[str, object]], None],
        cancelled: Callable[[], bool],
        session_id: str,
    ) -> RunnableAgent:
        return Agent(
            FakeModelClient(
                [
                    ModelReply(
                        tool_calls=(
                            ToolCall("run", "run_command", {"argv": ["python", "-V"]}),
                        )
                    )
                ]
            ),
            build_tool_registry(tmp_path, approval),
            ContextManager(),
            TraceRecorder(tmp_path, session_id=session_id, observer=observer),
            cancelled=cancelled,
        )

    app = create_app(tmp_path, _settings(), agent_factory=factory)
    with TestClient(app) as client:
        started = client.post("/api/runs", json={"task": "run a command"})
        run_id = started.json()["id"]
        _wait_for_status(client, run_id, {"waiting_approval"})
        client.post(f"/api/runs/{run_id}/cancel")
        payload = _wait_for_status(client, run_id, {"interrupted"})

    trace_path = Path(str(payload["result"]["trace_path"]))
    trace_types = [event["event_type"] for event in read_trace(trace_path)]
    web_types = [event["event_type"] for event in payload["events"]]
    for event_type in ("approval_required", "approval_resolved", "cancel_requested"):
        assert trace_types.count(event_type) == 1
        assert web_types.count(event_type) == 1


def test_web_backfills_control_event_when_cancel_precedes_trace_registration(tmp_path: Path) -> None:
    factory_entered = Event()
    release_factory = Event()
    created_trace: list[TraceRecorder] = []

    def factory(
        approval: Callable[[list[str], Path], bool],
        observer: Callable[[dict[str, object]], None],
        cancelled: Callable[[], bool],
        session_id: str,
    ) -> RunnableAgent:
        trace = TraceRecorder(tmp_path, session_id=session_id, observer=observer)
        created_trace.append(trace)
        factory_entered.set()
        release_factory.wait(timeout=1)
        return Agent(
            FakeModelClient([ModelReply(content="must not be requested")]),
            build_tool_registry(tmp_path, approval),
            ContextManager(),
            trace,
            cancelled=cancelled,
        )

    app = create_app(tmp_path, _settings(), agent_factory=factory)
    with TestClient(app) as client:
        started = client.post("/api/runs", json={"task": "cancel immediately"})
        run_id = started.json()["id"]
        assert factory_entered.wait(timeout=1)
        assert client.post(f"/api/runs/{run_id}/cancel").status_code == 200
        release_factory.set()
        _wait_for_status(client, run_id, {"interrupted"})

    event_types = [event["event_type"] for event in read_trace(created_trace[0].path)]
    assert event_types.count("cancel_requested") == 1


def test_web_cancel_releases_a_running_agent(tmp_path: Path) -> None:
    agent_started = Event()
    cancellation_observed = Event()
    finish_run = Event()

    class CancellableAgent:
        def __init__(self, cancelled: Callable[[], bool]) -> None:
            self.cancelled = cancelled

        def run(self, _task: str) -> RunResult:
            agent_started.set()
            deadline = time.monotonic() + 2
            while not self.cancelled() and time.monotonic() < deadline:
                time.sleep(0.005)
            cancellation_observed.set()
            finish_run.wait(timeout=1)
            return RunResult(
                final_text="Interrupted by the user.",
                termination_reason=TerminationReason.INTERRUPTED,
                verification_status=VerificationStatus.NOT_REQUIRED,
                changed_files=(),
                trace_path=str(tmp_path / "trace.jsonl"),
                steps=0,
            )

    def factory(
        _approval: Callable[[list[str], Path], bool],
        _observer: Callable[[dict[str, object]], None],
        cancelled: Callable[[], bool],
        _session_id: str,
    ) -> RunnableAgent:
        return CancellableAgent(cancelled)

    app = create_app(tmp_path, _settings(), agent_factory=factory)
    with TestClient(app) as client:
        started = client.post("/api/runs", json={"task": "keep running"})
        run_id = started.json()["id"]
        assert agent_started.wait(timeout=1)
        cancelled = client.post(f"/api/runs/{run_id}/cancel")
        assert cancelled.status_code == 200
        assert cancellation_observed.wait(timeout=1)
        cancelled_again = client.post(f"/api/runs/{run_id}/cancel")
        assert cancelled_again.status_code == 200
        cancel_events = [
            event for event in cancelled_again.json()["events"] if event["event_type"] == "cancel_requested"
        ]
        assert len(cancel_events) == 1
        finish_run.set()
        payload = _wait_for_status(client, run_id, {"interrupted"})

    assert payload["result"]["termination_reason"] == "interrupted"


def test_run_manager_evicts_old_completed_runs(tmp_path: Path) -> None:
    class ImmediateAgent:
        def run(self, _task: str) -> RunResult:
            return RunResult(
                final_text="Done.",
                termination_reason=TerminationReason.COMPLETED,
                verification_status=VerificationStatus.NOT_REQUIRED,
                changed_files=(),
                trace_path=str(tmp_path / "trace.jsonl"),
                steps=1,
            )

    def factory(
        _approval: Callable[[list[str], Path], bool],
        _observer: Callable[[dict[str, object]], None],
        _cancelled: Callable[[], bool],
        _session_id: str,
    ) -> RunnableAgent:
        return ImmediateAgent()

    manager = RunManager(factory, completed_run_limit=1)
    first_id = str(manager.start("first")["id"])
    deadline = time.monotonic() + 1
    while manager.snapshot(first_id)["status"] != "finished" and time.monotonic() < deadline:
        time.sleep(0.005)
    second_id = str(manager.start("second")["id"])
    deadline = time.monotonic() + 1
    while manager.snapshot(second_id)["status"] != "finished" and time.monotonic() < deadline:
        time.sleep(0.005)

    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            manager.snapshot(first_id)
        except RunNotFoundError:
            break
        time.sleep(0.005)
    else:
        raise AssertionError("old completed run was not evicted")
