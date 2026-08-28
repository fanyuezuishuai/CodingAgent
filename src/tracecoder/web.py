"""Local-only web application and thread-safe agent run coordination."""

from __future__ import annotations

import threading
from _thread import LockType
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TypeAlias
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from tracecoder.config import Settings
from tracecoder.domain import RunResult, TerminationReason
from tracecoder.runtime import build_agent
from tracecoder.trace import TraceRecorder

ApprovalCallback: TypeAlias = Callable[[list[str], Path], bool]
TraceObserver: TypeAlias = Callable[[dict[str, object]], None]
CancelCallback: TypeAlias = Callable[[], bool]


class RunnableAgent(Protocol):
    """Small runtime surface used by the web coordinator."""

    def run(self, task: str) -> RunResult:
        """Execute one task and return runtime evidence."""


AgentFactory: TypeAlias = Callable[
    [ApprovalCallback, TraceObserver, CancelCallback, str],
    RunnableAgent,
]

_ACTIVE_STATUSES = {"running", "waiting_approval", "cancel_requested"}
_TERMINAL_STATUSES = {"finished", "interrupted", "failed"}
_STATIC_DIRECTORY = Path(__file__).with_name("web_static")
_DEFAULT_COMPLETED_RUN_LIMIT = 50


class RunConflictError(RuntimeError):
    """Raised when an operation conflicts with the current run state."""


class RunNotFoundError(KeyError):
    """Raised when a requested run does not exist."""


@dataclass(slots=True)
class _PendingApproval:
    id: str
    argv: list[str]
    cwd: str
    resolved: threading.Event = field(default_factory=threading.Event)
    decision: bool | None = None


@dataclass(slots=True)
class _ManagedRun:
    id: str
    task: str
    status: str = "running"
    events: list[dict[str, object]] = field(default_factory=list)
    result: RunResult | None = None
    error: str | None = None
    approval: _PendingApproval | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    trace: TraceRecorder | None = None
    pending_trace_events: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    lock: LockType = field(default_factory=threading.Lock, repr=False)


class RunManager:
    """Serialize workspace mutations while exposing observable web run state."""

    def __init__(
        self,
        agent_factory: AgentFactory,
        *,
        completed_run_limit: int = _DEFAULT_COMPLETED_RUN_LIMIT,
    ) -> None:
        if completed_run_limit <= 0:
            raise ValueError("completed_run_limit must be positive")
        self._agent_factory = agent_factory
        self._runs: dict[str, _ManagedRun] = {}
        self._completed_run_ids: deque[str] = deque()
        self._completed_run_limit = completed_run_limit
        self._active_id: str | None = None
        self._lock = threading.Lock()

    def start(self, task: str) -> dict[str, object]:
        """Start one background run, rejecting overlapping workspace mutations."""

        normalized_task = task.strip()
        if not normalized_task:
            raise ValueError("task must not be empty")
        with self._lock:
            if self._active_id is not None:
                active = self._runs[self._active_id]
                if active.status in _ACTIVE_STATUSES:
                    raise RunConflictError("another agent run is already active")
            run_id = uuid4().hex
            managed = _ManagedRun(run_id, normalized_task)
            self._runs[run_id] = managed
            self._active_id = run_id
        thread = threading.Thread(target=self._execute, args=(managed,), daemon=True, name=f"tracecoder-{run_id[:8]}")
        thread.start()
        return self.snapshot(run_id)

    def snapshot(self, run_id: str, *, after: int = 0) -> dict[str, object]:
        """Return one immutable JSON-ready view of current run state."""

        managed = self._get(run_id)
        with managed.lock:
            return self._snapshot_locked(managed, after=after)

    def active_snapshot(self) -> dict[str, object] | None:
        """Return the active run, if any, for reconnecting browser clients."""

        with self._lock:
            active_id = self._active_id
        if active_id is None:
            return None
        return self.snapshot(active_id)

    def approve(self, run_id: str, approval_id: str, approved: bool) -> dict[str, object]:
        """Resolve exactly the currently pending command approval."""

        managed = self._get(run_id)
        with managed.lock:
            pending = managed.approval
            if pending is None or pending.id != approval_id or pending.resolved.is_set():
                raise RunConflictError("approval is no longer pending")
            pending.decision = approved
            pending.resolved.set()
            return self._snapshot_locked(managed)

    def cancel(self, run_id: str) -> dict[str, object]:
        """Request cooperative cancellation and release any pending approval."""

        managed = self._get(run_id)
        should_record = False
        with managed.lock:
            if managed.status in _TERMINAL_STATUSES:
                return self._snapshot_locked(managed)
            if managed.cancel_event.is_set():
                return self._snapshot_locked(managed)
            managed.cancel_event.set()
            managed.status = "cancel_requested"
            if managed.approval is not None and not managed.approval.resolved.is_set():
                managed.approval.decision = False
                managed.approval.resolved.set()
            should_record = True
        if should_record:
            self._record_control_event(managed, "cancel_requested", {})
        return self.snapshot(run_id)

    def _execute(self, managed: _ManagedRun) -> None:
        def observer(event: dict[str, object]) -> None:
            event_type = str(event.get("event_type", "runtime_event"))
            payload = event.get("payload", {})
            self._append_event(managed, event_type, payload if isinstance(payload, dict) else {})

        def approval(argv: list[str], cwd: Path) -> bool:
            if managed.cancel_event.is_set():
                return False
            pending = _PendingApproval(uuid4().hex, list(argv), str(cwd))
            with managed.lock:
                managed.approval = pending
                managed.status = "waiting_approval"
            self._record_control_event(
                managed,
                "approval_required",
                {"approval_id": pending.id, "argv": pending.argv, "cwd": pending.cwd},
            )
            while not pending.resolved.wait(timeout=0.1):
                if managed.cancel_event.is_set():
                    pending.decision = False
                    pending.resolved.set()
            decision = pending.decision is True and not managed.cancel_event.is_set()
            with managed.lock:
                if managed.approval is pending:
                    managed.approval = None
                if managed.status != "cancel_requested":
                    managed.status = "running"
            self._record_control_event(
                managed,
                "approval_resolved",
                {"approval_id": pending.id, "approved": decision},
            )
            return decision

        try:
            agent = self._agent_factory(approval, observer, managed.cancel_event.is_set, managed.id)
            trace = getattr(agent, "trace", None)
            if isinstance(trace, TraceRecorder):
                with managed.lock:
                    managed.trace = trace
                    for event_type, payload in managed.pending_trace_events:
                        trace.record(event_type, payload, notify_observer=False)
                    managed.pending_trace_events.clear()
            result = agent.run(managed.task)
            with managed.lock:
                managed.result = result
                managed.status = (
                    "interrupted" if result.termination_reason is TerminationReason.INTERRUPTED else "finished"
                )
                managed.approval = None
        except Exception as exc:
            with managed.lock:
                managed.status = "failed"
                managed.error = f"{type(exc).__name__}: local runner failed"
                managed.approval = None
                self._append_event_locked(managed, "web_runner_error", {"error_type": type(exc).__name__})
        finally:
            with self._lock:
                if self._active_id == managed.id:
                    self._active_id = None
                self._completed_run_ids.append(managed.id)
                while len(self._completed_run_ids) > self._completed_run_limit:
                    expired_id = self._completed_run_ids.popleft()
                    self._runs.pop(expired_id, None)

    def _get(self, run_id: str) -> _ManagedRun:
        with self._lock:
            try:
                return self._runs[run_id]
            except KeyError as exc:
                raise RunNotFoundError(run_id) from exc

    def _append_event(self, managed: _ManagedRun, event_type: str, payload: dict[str, object]) -> None:
        with managed.lock:
            self._append_event_locked(managed, event_type, payload)

    def _record_control_event(
        self,
        managed: _ManagedRun,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        """Persist control-plane events when an agent trace is available."""

        with managed.lock:
            trace = managed.trace
            if trace is None:
                self._append_event_locked(managed, event_type, payload)
                managed.pending_trace_events.append((event_type, dict(payload)))
                return
        trace.record(event_type, payload)

    @staticmethod
    def _append_event_locked(managed: _ManagedRun, event_type: str, payload: dict[str, object]) -> None:
        managed.events.append(
            {
                "web_event_id": len(managed.events) + 1,
                "event_type": event_type,
                "payload": payload,
            }
        )

    @staticmethod
    def _snapshot_locked(managed: _ManagedRun, *, after: int = 0) -> dict[str, object]:
        pending = managed.approval
        approval: dict[str, object] | None = None
        if pending is not None and not pending.resolved.is_set():
            approval = {"id": pending.id, "argv": list(pending.argv), "cwd": pending.cwd}
        result: dict[str, object] | None = None
        if managed.result is not None:
            result = {
                "final_text": managed.result.final_text,
                "termination_reason": managed.result.termination_reason.value,
                "verification_status": managed.result.verification_status.value,
                "changed_files": list(managed.result.changed_files),
                "trace_path": managed.result.trace_path,
                "steps": managed.result.steps,
                "shell_side_effects_unknown": managed.result.shell_side_effects_unknown,
                "successful": managed.result.successful,
            }
        return {
            "id": managed.id,
            "task": managed.task,
            "status": managed.status,
            "events": [event.copy() for event in managed.events[after:]],
            "next_event_id": len(managed.events),
            "approval": approval,
            "result": result,
            "error": managed.error,
        }


class StartRunRequest(BaseModel):
    """Validated task submitted from the local UI."""

    model_config = ConfigDict(str_strip_whitespace=True)
    task: str = Field(min_length=1, max_length=20_000)

    @field_validator("task")
    @classmethod
    def reject_blank_task(cls, value: str) -> str:
        if not value:
            raise ValueError("task must not be blank")
        return value


class ApprovalRequest(BaseModel):
    """A decision tied to one exact pending command."""

    approval_id: str = Field(min_length=1, max_length=64)
    approved: bool


def create_app(
    workspace: Path,
    settings: Settings,
    *,
    agent_factory: AgentFactory | None = None,
) -> FastAPI:
    """Create the local web API and static UI for one canonical workspace."""

    canonical_workspace = workspace.resolve(strict=True)
    if not canonical_workspace.is_dir():
        raise ValueError("workspace must be a directory")

    if agent_factory is None:
        def configured_factory(
            approval: ApprovalCallback,
            observer: TraceObserver,
            cancelled: CancelCallback,
            session_id: str,
        ) -> RunnableAgent:
            return build_agent(
                canonical_workspace,
                settings,
                approval,
                observer=observer,
                cancelled=cancelled,
                session_id=session_id,
            )

        agent_factory = configured_factory

    manager = RunManager(agent_factory)
    app = FastAPI(title="TraceCoder Local Web", docs_url=None, redoc_url=None)
    app.state.run_manager = manager
    app.mount("/assets", StaticFiles(directory=_STATIC_DIRECTORY), name="assets")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(_STATIC_DIRECTORY / "index.html")

    @app.get("/api/config")
    def public_config() -> dict[str, object]:
        return {
            "workspace": str(canonical_workspace),
            "provider": settings.base_url,
            "model": settings.model,
        }

    @app.post("/api/runs", status_code=201)
    def start_run(request: StartRunRequest) -> dict[str, object]:
        try:
            return manager.start(request.task)
        except RunConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/runs/active")
    def active_run() -> dict[str, object] | None:
        return manager.active_snapshot()

    @app.get("/api/runs/{run_id}")
    def run_state(run_id: str, after: int = Query(default=0, ge=0)) -> dict[str, object]:
        try:
            return manager.snapshot(run_id, after=after)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.post("/api/runs/{run_id}/approval")
    def resolve_approval(run_id: str, request: ApprovalRequest) -> dict[str, object]:
        try:
            return manager.approve(run_id, request.approval_id, request.approved)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        except RunConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: str) -> dict[str, object]:
        try:
            return manager.cancel(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    return app
