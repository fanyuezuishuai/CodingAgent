"""Local web application integration tests."""

import time
from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Any, Protocol, cast

import pytest
from fastapi.testclient import TestClient

from tests.fakes import FakeModelClient
from tracecoder.agent import Agent
from tracecoder.config import Settings
from tracecoder.context import ContextManager
from tracecoder.domain import ModelReply, RunResult, TerminationReason, ToolCall, VerificationStatus
from tracecoder.tools import build_tool_registry
from tracecoder.trace import TraceRecorder, read_trace
from tracecoder.web import RunManager, RunNotFoundError, _describe_command, create_app


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
    assert 'id="model"' in page.text
    assert 'id="history-list"' in page.text
    assert 'id="file-input"' in page.text
    assert 'id="task-input"' in page.text
    assert 'id="workspace"' not in page.text
    assert 'id="provider"' not in page.text
    assert config.json() == {
        "workspace": str(tmp_path.resolve()),
        "provider": "https://provider.example/v1",
        "model": "web-model",
    }
    assert "web-secret" not in page.text
    assert "web-secret" not in config.text


def test_web_ui_assets_keep_process_collapsed_and_composer_docked(tmp_path: Path) -> None:
    app = create_app(tmp_path, _settings())

    with TestClient(app) as client:
        page = client.get("/")
        script = client.get("/assets/app.js")
        markdown = client.get("/assets/markdown.js")
        styles = client.get("/assets/styles.css")

    assert '<details class="process-card">' in page.text
    assert page.text.index('id="model"') < page.text.index('id="new-chat-button"') < page.text.index("历史对话")
    assert 'class="new-chat-button"' in page.text
    assert 'id="approval-description"' in page.text
    assert "查看完整命令" in page.text
    assert "renderFinalAnswer(runId, data.result, data.error)" in script.text
    assert "renderProcessEvents(runId, data.events, data)" in script.text
    assert "deferredProcessEvents" in script.text
    assert 'request("/api/conversations")' in script.text
    assert "TraceCoderMarkdown.render" in script.text
    assert "/api/uploads?filename=" in script.text
    assert markdown.status_code == 200
    assert "escapeHtml" in markdown.text
    assert "javascript:" in markdown.text
    assert "grid-template-rows: auto minmax(0, 1fr) auto auto" in styles.text
    assert ".composer-dock" in styles.text
    assert ".empty-state[hidden]" in styles.text


def test_web_lists_run_history_newest_first(tmp_path: Path) -> None:
    class ImmediateAgent:
        def run(self, task: str) -> RunResult:
            return RunResult(
                final_text=f"Finished {task}.",
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

    app = create_app(tmp_path, _settings(), agent_factory=factory)
    with TestClient(app) as client:
        first = client.post("/api/runs", json={"task": "first task"}).json()
        _wait_for_status(client, first["id"], {"finished"})
        second = client.post("/api/runs", json={"task": "second task"}).json()
        _wait_for_status(client, second["id"], {"finished"})

        history = client.get("/api/runs")
        first_snapshot = client.get(f"/api/runs/{first['id']}")

    assert history.status_code == 200
    assert [run["id"] for run in history.json()["runs"]] == [second["id"], first["id"]]
    assert history.json()["runs"][0] == {
        "id": second["id"],
        "task": "second task",
        "status": "finished",
    }
    assert first_snapshot.json()["task"] == "first task"
    assert first_snapshot.json()["result"]["final_text"] == "Finished first task."


def test_web_conversation_supports_multiple_contextual_turns_until_new_chat(tmp_path: Path) -> None:
    private_state = "opaque-reasoning-state-for-web-history"
    replies = iter(
        [
            ModelReply(
                content="The project codename is Alpha.",
                reasoning_content=private_state,
            ),
            ModelReply(content="The codename from the previous turn is Alpha."),
            ModelReply(content="This is a separate conversation."),
        ]
    )
    models: list[FakeModelClient] = []

    def factory(
        approval: Callable[[list[str], Path], bool],
        observer: Callable[[dict[str, object]], None],
        cancelled: Callable[[], bool],
        session_id: str,
    ) -> RunnableAgent:
        model = FakeModelClient([next(replies)])
        models.append(model)
        return Agent(
            model,
            build_tool_registry(tmp_path, approval),
            ContextManager(),
            TraceRecorder(tmp_path, session_id=session_id, observer=observer),
            cancelled=cancelled,
        )

    app = create_app(tmp_path, _settings(), agent_factory=factory)
    with TestClient(app) as client:
        first = client.post("/api/runs", json={"task": "Remember the project codename Alpha."}).json()
        _wait_for_status(client, first["id"], {"finished"})

        second = client.post(
            "/api/runs",
            json={"task": "What was the codename?", "conversation_id": first["conversation_id"]},
        ).json()
        _wait_for_status(client, second["id"], {"finished"})

        conversation = client.get(f"/api/conversations/{first['conversation_id']}")
        history = client.get("/api/conversations")

        missing = client.post(
            "/api/runs",
            json={"task": "Do not start.", "conversation_id": "0" * 32},
        )

        separate = client.post("/api/runs", json={"task": "Start separately."}).json()
        _wait_for_status(client, separate["id"], {"finished"})
        updated_history = client.get("/api/conversations")

    assert second["conversation_id"] == first["conversation_id"]
    assert conversation.status_code == 200
    assert [turn["task"] for turn in conversation.json()["turns"]] == [
        "Remember the project codename Alpha.",
        "What was the codename?",
    ]
    assert history.json()["conversations"] == [
        {
            "id": first["conversation_id"],
            "title": "Remember the project codename Alpha.",
            "status": "finished",
            "turn_count": 2,
        }
    ]
    second_request = models[1].requests[0]
    assert [message["role"] for message in second_request] == ["system", "user", "assistant", "user"]
    assert second_request[1]["content"] == "Remember the project codename Alpha."
    assert second_request[2]["content"] == "The project codename is Alpha."
    assert second_request[2]["reasoning_content"] == private_state
    assert second_request[3]["content"] == "What was the codename?"
    assert private_state not in conversation.text
    assert missing.status_code == 404
    assert separate["conversation_id"] != first["conversation_id"]
    assert len(updated_history.json()["conversations"]) == 2


def test_web_uploads_files_into_workspace_without_overwriting(tmp_path: Path) -> None:
    app = create_app(tmp_path, _settings())

    with TestClient(app) as client:
        first = client.post(
            "/api/uploads",
            params={"filename": "example.py"},
            content=b"print('first')\n",
            headers={"content-type": "application/octet-stream"},
        )
        second = client.post(
            "/api/uploads",
            params={"filename": "example.py"},
            content=b"print('second')\n",
            headers={"content-type": "application/octet-stream"},
        )

    assert first.status_code == 201
    assert first.json() == {"name": "example.py", "path": "uploads/example.py", "size": 15}
    assert second.status_code == 201
    assert second.json()["path"] == "uploads/example-2.py"
    assert (tmp_path / "uploads" / "example.py").read_bytes() == b"print('first')\n"
    assert (tmp_path / "uploads" / "example-2.py").read_bytes() == b"print('second')\n"


def test_web_upload_rejects_unsafe_names_and_oversized_files(tmp_path: Path) -> None:
    app = create_app(tmp_path, _settings())
    upload_headers = {"content-type": "application/octet-stream"}

    with TestClient(app) as client:
        traversal = client.post(
            "/api/uploads",
            params={"filename": "../.env"},
            content=b"secret",
            headers=upload_headers,
        )
        windows_traversal = client.post(
            "/api/uploads",
            params={"filename": r"..\secret.txt"},
            content=b"secret",
            headers=upload_headers,
        )
        oversized = client.post(
            "/api/uploads",
            params={"filename": "large.txt"},
            content=b"x" * (10 * 1024 * 1024 + 1),
            headers=upload_headers,
        )
        form_upload = client.post(
            "/api/uploads",
            params={"filename": "cross-site.txt"},
            content=b"must not be written",
            headers={"content-type": "text/plain"},
        )

    assert traversal.status_code == 400
    assert windows_traversal.status_code == 400
    assert oversized.status_code == 413
    assert form_upload.status_code == 415
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "secret.txt").exists()
    assert not (tmp_path / "uploads" / "cross-site.txt").exists()


def test_web_passes_uploaded_attachments_to_agent_without_polluting_history_title(tmp_path: Path) -> None:
    received_tasks: list[str] = []

    class CapturingAgent:
        def run(self, task: str) -> RunResult:
            received_tasks.append(task)
            return RunResult(
                final_text="Attachment read.",
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
        return CapturingAgent()

    app = create_app(tmp_path, _settings(), agent_factory=factory)
    with TestClient(app) as client:
        uploaded = client.post(
            "/api/uploads",
            params={"filename": "notes.txt"},
            content=b"hello",
            headers={"content-type": "application/octet-stream"},
        ).json()
        started = client.post(
            "/api/runs",
            json={"task": "summarize the attachment", "attachments": [uploaded["path"]]},
        )
        payload = _wait_for_status(client, started.json()["id"], {"finished"})
        invalid = client.post(
            "/api/runs",
            json={"task": "read a secret", "attachments": ["../.env"]},
        )

    assert payload["task"] == "summarize the attachment"
    assert payload["attachments"] == ["uploads/notes.txt"]
    assert received_tasks == [
        "summarize the attachment\n\nUser-uploaded workspace files:\n- uploads/notes.txt"
    ]
    assert invalid.status_code == 400


def test_uploaded_file_is_readable_and_writable_by_real_agent(tmp_path: Path) -> None:
    models: list[FakeModelClient] = []

    def factory(
        approval: Callable[[list[str], Path], bool],
        observer: Callable[[dict[str, object]], None],
        cancelled: Callable[[], bool],
        session_id: str,
    ) -> RunnableAgent:
        model = FakeModelClient(
            [
                ModelReply(
                    tool_calls=(ToolCall("read-upload", "read_file", {"path": "uploads/notes.txt"}),)
                ),
                ModelReply(
                    tool_calls=(
                        ToolCall(
                            "write-upload",
                            "write_file",
                            {"path": "uploads/notes.txt", "content": "updated\n"},
                        ),
                    )
                ),
                ModelReply(content="The attachment is updated."),
                ModelReply(content="Updated the uploaded file; verification was not run."),
            ]
        )
        models.append(model)
        return Agent(
            model,
            build_tool_registry(tmp_path, approval),
            ContextManager(),
            TraceRecorder(tmp_path, session_id=session_id, observer=observer),
            cancelled=cancelled,
        )

    app = create_app(tmp_path, _settings(), agent_factory=factory)
    with TestClient(app) as client:
        uploaded = client.post(
            "/api/uploads",
            params={"filename": "notes.txt"},
            content=b"original\n",
            headers={"content-type": "application/octet-stream"},
        ).json()
        started = client.post(
            "/api/runs",
            json={"task": "read and update the attachment", "attachments": [uploaded["path"]]},
        )
        payload = _wait_for_status(client, started.json()["id"], {"finished"})

    assert (tmp_path / "uploads" / "notes.txt").read_text(encoding="utf-8") == "updated\n"
    assert payload["result"]["changed_files"] == ["uploads/notes.txt"]
    assert payload["result"]["final_text"] == "Updated the uploaded file; verification was not run."
    assert "original" in str(models[0].requests[1])


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
            decision_seen.append(
                self.approval(
                    ["g++", "-std=c++17", "-Wall", "-o", "fibonacci_test.exe", "fibonacci.cpp"],
                    tmp_path,
                )
            )
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
        assert approval["argv"] == [
            "g++",
            "-std=c++17",
            "-Wall",
            "-o",
            "fibonacci_test.exe",
            "fibonacci.cpp",
        ]
        assert approval["description"] == (
            f"申请在 {tmp_path} 编译 fibonacci.cpp，并生成 fibonacci_test.exe。"
        )

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


def test_run_manager_evicts_old_conversations(tmp_path: Path) -> None:
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


def test_run_manager_moves_continued_conversation_to_front_before_eviction(tmp_path: Path) -> None:
    class ImmediateAgent:
        def run(self, task: str) -> RunResult:
            return RunResult(
                final_text=f"Done: {task}",
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

    def wait(manager: RunManager, run_id: str) -> None:
        deadline = time.monotonic() + 1
        while manager.snapshot(run_id)["status"] != "finished" and time.monotonic() < deadline:
            time.sleep(0.005)

    manager = RunManager(factory, completed_run_limit=2)
    first = manager.start("first conversation")
    wait(manager, str(first["id"]))
    second = manager.start("second conversation")
    wait(manager, str(second["id"]))
    continued = manager.start("continue first", conversation_id=str(first["conversation_id"]))
    wait(manager, str(continued["id"]))

    assert [item["id"] for item in manager.conversation_history()] == [
        first["conversation_id"],
        second["conversation_id"],
    ]
    assert manager.conversation_history()[0]["turn_count"] == 2

    third = manager.start("third conversation")
    wait(manager, str(third["id"]))

    with pytest.raises(RunNotFoundError):
        manager.conversation_snapshot(str(second["conversation_id"]))
    assert manager.conversation_snapshot(str(first["conversation_id"]))["turn_count"] == 2


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["python", "-V"], "申请在 C:\\work 查看 Python 版本信息。"),
        (["python", "-m", "pytest", "-q"], "申请在 C:\\work 运行 Python 模块 pytest。"),
        (["python", "script.py"], "申请在 C:\\work 运行 Python 脚本 script.py。"),
        (["pytest", "-q"], "申请在 C:\\work 运行 Python 测试。"),
        (["git", "status"], "申请在 C:\\work 执行 Git status 操作。"),
        (["npm.cmd", "run", "test"], "申请在 C:\\work 执行前端任务 run test。"),
        (["mkdir", "build"], "申请在 C:\\work 新建 build。"),
        (["rustc", "main.rs"], "申请在 C:\\work 执行 rustc 命令。"),
    ],
)
def test_describe_command_covers_readable_command_families(argv: list[str], expected: str) -> None:
    assert _describe_command(argv, r"C:\work") == expected
