"""Local-only web application and thread-safe agent run coordination."""

from __future__ import annotations

import threading
from _thread import LockType
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Literal, Protocol, TypeAlias, cast, runtime_checkable
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from tracecoder.config import Settings
from tracecoder.domain import JSONValue, Message, RunResult, TerminationReason
from tracecoder.evidence import render_proof_markdown, write_proof_artifacts
from tracecoder.runtime import build_agent
from tracecoder.scenarios import ScenarioName, apply_scenario
from tracecoder.trace import TraceRecorder
from tracecoder.transaction import TransactionError, WorkspaceTransaction

ApprovalCallback: TypeAlias = Callable[[list[str], Path], bool]
TraceObserver: TypeAlias = Callable[[dict[str, object]], None]
CancelCallback: TypeAlias = Callable[[], bool]
ToolPolicy: TypeAlias = Literal["full", "read_only"]
ProjectPhase: TypeAlias = Literal["planning", "awaiting_approval", "active"]


class RunnableAgent(Protocol):
    """Small runtime surface used by the web coordinator."""

    def run(self, task: str) -> RunResult:
        """Execute one task and return runtime evidence."""


@runtime_checkable
class ConversationalRunnableAgent(Protocol):
    """Optional richer runtime surface used for cross-turn model context."""

    @property
    def conversation_messages(self) -> tuple[Message, ...]:
        """Return reusable messages from the completed run."""

    def run(self, task: str, *, history: Sequence[Message] = ()) -> RunResult:
        """Execute one task with prior conversation messages."""


AgentFactory: TypeAlias = Callable[
    [ApprovalCallback, TraceObserver, CancelCallback, str],
    RunnableAgent,
]

_ACTIVE_STATUSES = {"running", "waiting_approval", "cancel_requested"}
_TERMINAL_STATUSES = {"finished", "interrupted", "failed"}
_STATIC_DIRECTORY = Path(__file__).with_name("web_static")
_DEFAULT_COMPLETED_RUN_LIMIT = 50
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
_INVALID_UPLOAD_NAME_CHARACTERS = frozenset('<>:"/\\|?*')
_READ_ONLY_TOOLS = ("list_files", "search_text", "read_file")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


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
    conversation_id: str
    conversation_title: str
    turn_index: int
    task: str
    attachments: tuple[str, ...]
    prior_messages: tuple[Message, ...]
    project_id: str | None = None
    project_name: str | None = None
    project_context: str | None = None
    scenario: ScenarioName = "general"
    tool_policy: ToolPolicy = "full"
    is_project_planning: bool = False
    status: str = "running"
    events: list[dict[str, object]] = field(default_factory=list)
    result: RunResult | None = None
    error: str | None = None
    approval: _PendingApproval | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    trace: TraceRecorder | None = None
    pending_trace_events: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    proof: dict[str, JSONValue] | None = None
    transaction_state: str = "not_required"
    rollback_available: bool = False
    lock: LockType = field(default_factory=threading.Lock, repr=False)


@dataclass(slots=True)
class _ManagedConversation:
    id: str
    title: str
    project_id: str | None = None
    run_ids: list[str] = field(default_factory=list)
    messages: tuple[Message, ...] = ()


@dataclass(slots=True)
class _ManagedProject:
    id: str
    name: str
    goal: str
    preferences: str
    target_path: str
    attachments: list[str]
    created_at: str
    updated_at: str
    phase: ProjectPhase = "planning"
    conversation_ids: list[str] = field(default_factory=list)
    messages: tuple[Message, ...] = ()


class RunManager:
    """Serialize workspace mutations while exposing observable web run state."""

    def __init__(
        self,
        agent_factory: AgentFactory,
        *,
        completed_run_limit: int = _DEFAULT_COMPLETED_RUN_LIMIT,
        workspace: Path | None = None,
    ) -> None:
        if completed_run_limit <= 0:
            raise ValueError("completed_run_limit must be positive")
        self._agent_factory = agent_factory
        self._workspace = workspace.resolve(strict=True) if workspace is not None else None
        self._runs: dict[str, _ManagedRun] = {}
        self._conversations: dict[str, _ManagedConversation] = {}
        self._conversation_order: deque[str] = deque()
        self._projects: dict[str, _ManagedProject] = {}
        self._project_order: deque[str] = deque()
        self._completed_run_limit = completed_run_limit
        self._active_id: str | None = None
        self._latest_transaction_run_id: str | None = None
        self._lock = threading.Lock()

    def create_project(
        self,
        *,
        name: str,
        goal: str,
        preferences: str = "",
        target_path: str = "",
        attachments: tuple[str, ...] = (),
    ) -> dict[str, object]:
        """Create one in-memory project container for related conversations."""

        timestamp = datetime.now(UTC).isoformat()
        project = _ManagedProject(
            id=uuid4().hex,
            name=name.strip(),
            goal=goal.strip(),
            preferences=preferences.strip(),
            target_path=target_path.strip(),
            attachments=list(dict.fromkeys(attachments)),
            created_at=timestamp,
            updated_at=timestamp,
        )
        if not project.name or not project.goal:
            raise ValueError("project name and goal must not be blank")
        with self._lock:
            self._projects[project.id] = project
            self._project_order.append(project.id)
            return self._project_snapshot_locked(project, include_conversations=True)

    def project_history(self) -> list[dict[str, object]]:
        """Return project summaries in most-recently-used order."""

        with self._lock:
            return [
                self._project_summary_locked(self._projects[project_id])
                for project_id in reversed(self._project_order)
            ]

    def project_snapshot(self, project_id: str) -> dict[str, object]:
        """Return project metadata and all of its current conversations."""

        with self._lock:
            try:
                project = self._projects[project_id]
            except KeyError as exc:
                raise RunNotFoundError(project_id) from exc
            return self._project_snapshot_locked(project, include_conversations=True)

    def start(
        self,
        task: str,
        *,
        attachments: tuple[str, ...] = (),
        conversation_id: str | None = None,
        project_id: str | None = None,
        scenario: ScenarioName = "general",
        tool_policy: ToolPolicy = "full",
    ) -> dict[str, object]:
        """Start one background run, rejecting overlapping workspace mutations."""

        normalized_task = task.strip()
        if not normalized_task:
            raise ValueError("task must not be empty")
        with self._lock:
            if self._active_id is not None:
                active = self._runs[self._active_id]
                if active.status in _ACTIVE_STATUSES:
                    raise RunConflictError("another agent run is already active")
            if conversation_id is None:
                project = self._project_for_start_locked(project_id)
                conversation = None
            else:
                try:
                    conversation = self._conversations[conversation_id]
                except KeyError as exc:
                    raise RunNotFoundError(conversation_id) from exc
                if project_id is not None and project_id != conversation.project_id:
                    raise RunConflictError("conversation does not belong to the requested project")
                project = self._project_for_start_locked(conversation.project_id)

            if project is not None and scenario != "general":
                raise RunConflictError("project conversations do not use repair/generate presets")
            is_project_planning = project is not None and project.phase == "planning"
            if is_project_planning and tool_policy != "read_only":
                raise RunConflictError("project planning must complete before full coding tools are enabled")
            self._accept_previous_transaction_locked()
            if project is not None and project.phase == "awaiting_approval" and tool_policy == "full":
                project.phase = "active"
                self._touch_project_locked(project)

            if conversation is None:
                conversation = _ManagedConversation(
                    uuid4().hex,
                    _conversation_title(normalized_task),
                    project_id=project.id if project is not None else None,
                )
                self._conversations[conversation.id] = conversation
                if project is not None:
                    project.conversation_ids.append(conversation.id)

            combined_attachments = tuple(
                dict.fromkeys([*(project.attachments if project is not None else []), *attachments])
            )
            if project is not None:
                project.attachments = list(combined_attachments)
                self._touch_project_locked(project)

            if conversation.id in self._conversation_order:
                self._conversation_order.remove(conversation.id)
            self._conversation_order.append(conversation.id)
            while len(self._conversation_order) > self._completed_run_limit:
                expired_conversation_id = self._conversation_order.popleft()
                expired = self._conversations.pop(expired_conversation_id)
                if expired.project_id is not None and expired.project_id in self._projects:
                    project_conversations = self._projects[expired.project_id].conversation_ids
                    if expired_conversation_id in project_conversations:
                        project_conversations.remove(expired_conversation_id)
                for expired_run_id in expired.run_ids:
                    self._runs.pop(expired_run_id, None)

            run_id = uuid4().hex
            managed = _ManagedRun(
                id=run_id,
                conversation_id=conversation.id,
                conversation_title=conversation.title,
                turn_index=len(conversation.run_ids) + 1,
                task=normalized_task,
                attachments=combined_attachments,
                prior_messages=project.messages if project is not None else conversation.messages,
                project_id=project.id if project is not None else None,
                project_name=project.name if project is not None else None,
                project_context=_project_context(project) if project is not None else None,
                scenario=scenario,
                tool_policy=tool_policy,
                is_project_planning=is_project_planning,
            )
            self._runs[run_id] = managed
            conversation.run_ids.append(run_id)
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

    def history(self) -> list[dict[str, object]]:
        """Return lightweight run summaries in newest-first order."""

        with self._lock:
            run_ids = list(islice(reversed(self._runs), self._completed_run_limit))
        summaries: list[dict[str, object]] = []
        for run_id in run_ids:
            try:
                managed = self._get(run_id)
            except RunNotFoundError:
                continue
            with managed.lock:
                summaries.append(
                    {
                        "id": managed.id,
                        "task": managed.task,
                        "status": managed.status,
                    }
                )
        return summaries

    def conversation_history(self) -> list[dict[str, object]]:
        """Return recent conversation summaries in most-recently-used order."""

        with self._lock:
            return [
                self._conversation_summary_locked(self._conversations[conversation_id])
                for conversation_id in reversed(self._conversation_order)
                if self._conversations[conversation_id].run_ids
            ]

    def conversation_snapshot(self, conversation_id: str) -> dict[str, object]:
        """Return all visible turns for one conversation."""

        with self._lock:
            try:
                conversation = self._conversations[conversation_id]
            except KeyError as exc:
                raise RunNotFoundError(conversation_id) from exc
            title = conversation.title
            project_id = conversation.project_id
            project_name = self._projects[project_id].name if project_id is not None else None
            managed_runs = [self._runs[run_id] for run_id in conversation.run_ids]
        turns: list[dict[str, object]] = []
        for managed in managed_runs:
            with managed.lock:
                turns.append(self._snapshot_locked(managed))
        return {
            "id": conversation_id,
            "title": title,
            "project_id": project_id,
            "project_name": project_name,
            "status": turns[-1]["status"] if turns else "empty",
            "turn_count": len(turns),
            "turns": turns,
        }

    def _project_for_start_locked(self, project_id: str | None) -> _ManagedProject | None:
        if project_id is None:
            return None
        try:
            return self._projects[project_id]
        except KeyError as exc:
            raise RunNotFoundError(project_id) from exc

    def _touch_project_locked(self, project: _ManagedProject) -> None:
        project.updated_at = datetime.now(UTC).isoformat()
        if project.id in self._project_order:
            self._project_order.remove(project.id)
        self._project_order.append(project.id)

    def _conversation_summary_locked(self, conversation: _ManagedConversation) -> dict[str, object]:
        latest_run = self._runs[conversation.run_ids[-1]]
        with latest_run.lock:
            status = latest_run.status
        project_name = (
            self._projects[conversation.project_id].name
            if conversation.project_id is not None and conversation.project_id in self._projects
            else None
        )
        return {
            "id": conversation.id,
            "title": conversation.title,
            "status": status,
            "turn_count": len(conversation.run_ids),
            "project_id": conversation.project_id,
            "project_name": project_name,
        }

    def _project_snapshot_locked(
        self,
        project: _ManagedProject,
        *,
        include_conversations: bool,
    ) -> dict[str, object]:
        snapshot: dict[str, object] = {
            "id": project.id,
            "name": project.name,
            "goal": project.goal,
            "preferences": project.preferences,
            "target_path": project.target_path,
            "attachments": list(project.attachments),
            "created_at": project.created_at,
            "updated_at": project.updated_at,
            "phase": project.phase,
            "conversation_count": len(project.conversation_ids),
        }
        if include_conversations:
            snapshot["conversations"] = [
                self._conversation_summary_locked(self._conversations[conversation_id])
                for conversation_id in reversed(project.conversation_ids)
                if conversation_id in self._conversations
            ]
        return snapshot

    @staticmethod
    def _project_summary_locked(project: _ManagedProject) -> dict[str, object]:
        goal = project.goal if len(project.goal) <= 240 else f"{project.goal[:237]}..."
        return {
            "id": project.id,
            "name": project.name,
            "goal": goal,
            "updated_at": project.updated_at,
            "phase": project.phase,
            "conversation_count": len(project.conversation_ids),
        }

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

    def proof(self, run_id: str) -> dict[str, JSONValue]:
        """Return the runtime-owned proof report for one completed run."""

        managed = self._get(run_id)
        with managed.lock:
            if managed.proof is None:
                raise RunConflictError("proof is unavailable for this run")
            return deepcopy(managed.proof)

    def resolve_transaction(self, run_id: str, action: str) -> dict[str, object]:
        """Accept or roll back the latest completed file-tool transaction."""

        if self._workspace is None:
            raise RunConflictError("transaction control is unavailable")
        managed = self._get(run_id)
        with self._lock:
            if self._active_id is not None:
                active = self._runs[self._active_id]
                if active.status in _ACTIVE_STATUSES:
                    raise RunConflictError("cannot change a transaction while an agent run is active")
            with managed.lock:
                if managed.status not in _TERMINAL_STATUSES or managed.result is None:
                    raise RunConflictError("run has not finished")
                transaction_id = managed.result.transaction_id
                if transaction_id is None:
                    raise RunConflictError("run has no file-tool transaction")
                if (
                    action == "rollback"
                    and managed.transaction_state == "pending"
                    and self._latest_transaction_run_id != run_id
                ):
                    raise RunConflictError("only the latest pending transaction can be rolled back")
                try:
                    transaction = WorkspaceTransaction.load(self._workspace, transaction_id)
                    outcome = transaction.rollback() if action == "rollback" else transaction.accept()
                except TransactionError as exc:
                    raise RunConflictError(str(exc)) from exc
                managed.transaction_state = str(outcome["state"])
                managed.rollback_available = bool(outcome["rollback_available"])
                self._update_transaction_proof_locked(managed)
                self._append_event_locked(
                    managed,
                    f"transaction_{managed.transaction_state}",
                    {"transaction_id": transaction_id, "action": action},
                )
                trace = managed.trace
                if self._latest_transaction_run_id == run_id:
                    self._latest_transaction_run_id = None
                snapshot = self._snapshot_locked(managed)
        if trace is not None:
            trace.record(
                f"transaction_{managed.transaction_state}",
                {"transaction_id": transaction_id, "action": action},
                notify_observer=False,
            )
        return snapshot

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

        agent_task = _agent_task_with_attachments(
            managed.task,
            list(managed.attachments),
            managed.scenario,
            project_context=managed.project_context,
        )
        conversation_messages: tuple[Message, ...] | None = None
        result: RunResult | None = None
        terminal_status = "failed"
        run_error: str | None = None
        error_type: str | None = None
        try:
            agent = self._agent_factory(approval, observer, managed.cancel_event.is_set, managed.id)
            if managed.tool_policy == "read_only":
                configure_tools = getattr(agent, "set_tool_allowlist", None)
                if not callable(configure_tools):
                    raise RuntimeError("agent does not support read-only tool policy")
                configure_tools(_READ_ONLY_TOOLS)
            trace = getattr(agent, "trace", None)
            if isinstance(trace, TraceRecorder):
                with managed.lock:
                    managed.trace = trace
                    for event_type, payload in managed.pending_trace_events:
                        trace.record(event_type, payload, notify_observer=False)
                    managed.pending_trace_events.clear()
            if isinstance(agent, ConversationalRunnableAgent):
                result = agent.run(agent_task, history=managed.prior_messages)
                conversation_messages = agent.conversation_messages
            else:
                result = agent.run(agent_task)
                conversation_messages = _fallback_conversation_messages(managed, result.final_text)
            terminal_status = (
                "interrupted" if result.termination_reason is TerminationReason.INTERRUPTED else "finished"
            )
        except Exception as exc:
            error_type = type(exc).__name__
            run_error = f"{error_type}: local runner failed"
            result = None
        finally:
            if conversation_messages is None:
                conversation_messages = _fallback_conversation_messages(
                    managed,
                    "Local runner failed before responding.",
                )
            with self._lock:
                with managed.lock:
                    managed.result = result
                    managed.status = terminal_status
                    managed.error = run_error
                    managed.approval = None
                    managed.prior_messages = ()
                    if result is not None:
                        managed.proof = deepcopy(result.proof) if result.proof is not None else None
                        managed.transaction_state = result.transaction_state
                        managed.rollback_available = result.rollback_available
                        if result.rollback_available:
                            self._latest_transaction_run_id = managed.id
                    if error_type is not None:
                        self._append_event_locked(managed, "web_runner_error", {"error_type": error_type})
                conversation = self._conversations.get(managed.conversation_id)
                if conversation is not None and conversation.run_ids[-1] == managed.id:
                    reusable_messages = tuple(dict(message) for message in conversation_messages)
                    conversation.messages = reusable_messages
                    if managed.project_id is not None and managed.project_id in self._projects:
                        project = self._projects[managed.project_id]
                        project.messages = reusable_messages
                        if (
                            managed.is_project_planning
                            and result is not None
                            and result.termination_reason is TerminationReason.COMPLETED
                        ):
                            project.phase = "awaiting_approval"
                        self._touch_project_locked(project)
                if self._active_id == managed.id:
                    self._active_id = None

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

    def _accept_previous_transaction_locked(self) -> None:
        """Auto-accept the previous run before a newer run can build on it."""

        run_id = self._latest_transaction_run_id
        if run_id is None or self._workspace is None:
            return
        managed = self._runs.get(run_id)
        if managed is None:
            self._latest_transaction_run_id = None
            return
        with managed.lock:
            if managed.result is None or managed.result.transaction_id is None:
                self._latest_transaction_run_id = None
                return
            try:
                transaction = WorkspaceTransaction.load(self._workspace, managed.result.transaction_id)
                outcome = transaction.accept()
            except TransactionError as exc:
                raise RunConflictError(f"could not accept previous transaction: {exc}") from exc
            managed.transaction_state = str(outcome["state"])
            managed.rollback_available = False
            self._update_transaction_proof_locked(managed)
            self._append_event_locked(
                managed,
                "transaction_accepted",
                {"transaction_id": managed.result.transaction_id, "action": "auto_accept_before_next_run"},
            )
            if managed.trace is not None:
                managed.trace.record(
                    "transaction_accepted",
                    {"transaction_id": managed.result.transaction_id, "action": "auto_accept_before_next_run"},
                    notify_observer=False,
                )
        self._latest_transaction_run_id = None

    def _update_transaction_proof_locked(self, managed: _ManagedRun) -> None:
        if managed.proof is None:
            return
        transaction = managed.proof.get("transaction")
        if isinstance(transaction, dict):
            transaction["state"] = managed.transaction_state
            transaction["rollback_available"] = managed.rollback_available
        if self._workspace is not None:
            try:
                write_proof_artifacts(self._workspace, managed.id, managed.proof)
            except OSError:
                self._append_event_locked(managed, "proof_export_failed", {"error_type": "OSError"})

    @staticmethod
    def _snapshot_locked(managed: _ManagedRun, *, after: int = 0) -> dict[str, object]:
        pending = managed.approval
        approval: dict[str, object] | None = None
        if pending is not None and not pending.resolved.is_set():
            approval = {
                "id": pending.id,
                "argv": list(pending.argv),
                "cwd": pending.cwd,
                "description": _describe_command(pending.argv, pending.cwd),
            }
        result: dict[str, object] | None = None
        if managed.result is not None:
            proof = deepcopy(managed.proof) if managed.proof is not None else None
            result = {
                "final_text": managed.result.final_text,
                "termination_reason": managed.result.termination_reason.value,
                "verification_status": managed.result.verification_status.value,
                "changed_files": list(managed.result.changed_files),
                "trace_path": managed.result.trace_path,
                "steps": managed.result.steps,
                "shell_side_effects_unknown": managed.result.shell_side_effects_unknown,
                "successful": managed.result.successful,
                "proof": proof,
                "transaction_id": managed.result.transaction_id,
                "transaction_state": managed.transaction_state,
                "rollback_available": managed.rollback_available,
                "proof_json_path": managed.result.proof_json_path,
                "proof_markdown_path": managed.result.proof_markdown_path,
            }
        return {
            "id": managed.id,
            "conversation_id": managed.conversation_id,
            "conversation_title": managed.conversation_title,
            "project_id": managed.project_id,
            "project_name": managed.project_name,
            "turn_index": managed.turn_index,
            "task": managed.task,
            "attachments": list(managed.attachments),
            "scenario": managed.scenario,
            "tool_policy": managed.tool_policy,
            "status": managed.status,
            "events": [event.copy() for event in managed.events[after:]],
            "next_event_id": len(managed.events),
            "approval": approval,
            "result": result,
            "error": managed.error,
        }


def _normalize_attachment_paths(value: list[str]) -> list[str]:
    if any(not attachment.strip() for attachment in value):
        raise ValueError("attachment paths must not be blank")
    return list(dict.fromkeys(attachment.strip() for attachment in value))


class StartRunRequest(BaseModel):
    """Validated task submitted from the local UI."""

    model_config = ConfigDict(str_strip_whitespace=True)
    task: str = Field(min_length=1, max_length=20_000)
    attachments: list[str] = Field(default_factory=list, max_length=20)
    scenario: ScenarioName = "general"
    conversation_id: str | None = Field(default=None, min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    project_id: str | None = Field(default=None, min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    tool_policy: ToolPolicy = "full"

    @field_validator("task")
    @classmethod
    def reject_blank_task(cls, value: str) -> str:
        if not value:
            raise ValueError("task must not be blank")
        return value

    @field_validator("attachments")
    @classmethod
    def reject_blank_attachments(cls, value: list[str]) -> list[str]:
        return _normalize_attachment_paths(value)


class CreateProjectRequest(BaseModel):
    """Structured project metadata submitted from the local UI."""

    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    goal: str = Field(min_length=1, max_length=8_000)
    preferences: str = Field(default="", max_length=4_000)
    target_path: str = Field(default="", max_length=500)
    attachments: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("attachments")
    @classmethod
    def reject_blank_attachments(cls, value: list[str]) -> list[str]:
        return _normalize_attachment_paths(value)


class ApprovalRequest(BaseModel):
    """A decision tied to one exact pending command."""

    approval_id: str = Field(min_length=1, max_length=64)
    approved: bool


class TransactionRequest(BaseModel):
    """One explicit decision for a completed file-tool transaction."""

    action: Literal["accept", "rollback"]


def _validate_upload_name(filename: str) -> str:
    """Validate one portable basename supplied by a browser file picker."""

    if (
        not filename
        or len(filename) > 255
        or filename in {".", ".."}
        or filename[-1] in {" ", "."}
        or any(character in _INVALID_UPLOAD_NAME_CHARACTERS or ord(character) < 32 for character in filename)
    ):
        raise ValueError("filename must be a safe basename")
    device_name = filename.split(".", maxsplit=1)[0].upper()
    if device_name in _WINDOWS_RESERVED_NAMES:
        raise ValueError("filename is reserved by Windows")
    return filename


def _save_upload(workspace: Path, filename: str, content: bytes | bytearray) -> dict[str, object]:
    """Write one upload inside the workspace without following a directory symlink."""

    safe_name = _validate_upload_name(filename)
    upload_directory = workspace / "uploads"
    try:
        upload_directory.mkdir(exist_ok=True)
    except FileExistsError as exc:
        raise RunConflictError("workspace uploads path must be a regular directory") from exc
    if not upload_directory.is_dir() or upload_directory.is_symlink():
        raise RunConflictError("workspace uploads path must be a regular directory")
    if upload_directory.resolve(strict=True).parent != workspace:
        raise RunConflictError("workspace uploads path is unsafe")

    requested = Path(safe_name)
    for sequence in range(1, 10_001):
        candidate_name = safe_name if sequence == 1 else f"{requested.stem}-{sequence}{requested.suffix}"
        candidate = upload_directory / candidate_name
        try:
            with candidate.open("xb") as handle:
                handle.write(content)
        except FileExistsError:
            continue
        except OSError:
            candidate.unlink(missing_ok=True)
            raise
        return {
            "name": candidate_name,
            "path": candidate.relative_to(workspace).as_posix(),
            "size": len(content),
        }
    raise RunConflictError("too many files share this upload name")


def _resolve_attachments(workspace: Path, attachments: list[str]) -> list[str]:
    """Resolve only regular files beneath the dedicated workspace upload directory."""

    if not attachments:
        return []
    upload_directory = workspace / "uploads"
    if not upload_directory.is_dir() or upload_directory.is_symlink():
        raise ValueError("uploaded attachment directory is unavailable")
    upload_root = upload_directory.resolve(strict=True)
    resolved: list[str] = []
    for attachment in attachments:
        candidate = (workspace / attachment).resolve(strict=True)
        if not candidate.is_file() or candidate.is_symlink() or not candidate.is_relative_to(upload_root):
            raise ValueError("attachment must reference an uploaded workspace file")
        resolved.append(candidate.relative_to(workspace).as_posix())
    return resolved


def _agent_task_with_attachments(
    task: str,
    attachments: list[str],
    scenario: ScenarioName = "general",
    *,
    project_context: str | None = None,
) -> str:
    enriched_task = apply_scenario(task, scenario)
    if project_context is not None:
        enriched_task = f"{project_context}\n\nCurrent project request:\n{enriched_task}"
    if not attachments:
        return enriched_task
    attachment_list = "\n".join(f"- {attachment}" for attachment in attachments)
    return f"{enriched_task}\n\nUser-uploaded workspace files:\n{attachment_list}"


def _fallback_conversation_messages(managed: _ManagedRun, final_text: str) -> tuple[Message, ...]:
    """Build basic cross-turn context for a custom Agent without history support."""

    task = _agent_task_with_attachments(
        managed.task,
        list(managed.attachments),
        managed.scenario,
        project_context=managed.project_context,
    )
    return (
        *(dict(message) for message in managed.prior_messages),
        {"role": "user", "content": task},
        {"role": "assistant", "content": final_text},
    )


def _project_context(project: _ManagedProject) -> str:
    """Build stable project metadata without assuming repair or generation."""

    lines = [
        f'Project: "{project.name}"',
        f"Project goal: {project.goal}",
    ]
    if project.preferences:
        lines.append(f"Technical preferences: {project.preferences}")
    if project.target_path:
        lines.append(f"Preferred target path: {project.target_path}")
    lines.append(
        "Use the current workspace and uploaded files as authoritative. The project may contain existing code "
        "or may start empty; inspect what exists and decide whether to modify or create code from the current request."
    )
    return "\n".join(lines)


def _conversation_title(task: str) -> str:
    first_line = task.splitlines()[0].strip()
    return first_line if len(first_line) <= 120 else first_line[:119] + "…"


def _executable_name(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", maxsplit=1)[-1].casefold()


def _describe_command(argv: list[str], cwd: str) -> str:
    """Create a deterministic Chinese summary while preserving exact argv separately."""

    if not argv:
        return f"申请在 {cwd} 执行一条空命令。"
    executable = _executable_name(argv[0])
    arguments = argv[1:]

    if executable in {"g++", "gcc", "clang", "clang++", "cl", "cl.exe"}:
        sources = [
            argument
            for argument in arguments
            if Path(argument).suffix.casefold() in {".c", ".cc", ".cpp", ".cxx"}
        ]
        output = next(
            (arguments[index + 1] for index, argument in enumerate(arguments[:-1]) if argument in {"-o", "/Fe"}),
            None,
        )
        source_text = "、".join(sources) if sources else "指定源文件"
        if output:
            return f"申请在 {cwd} 编译 {source_text}，并生成 {output}。"
        return f"申请在 {cwd} 编译 {source_text}。"

    if executable in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}:
        if any(argument in {"-V", "--version"} for argument in arguments):
            return f"申请在 {cwd} 查看 Python 版本信息。"
        if len(arguments) >= 2 and arguments[0] == "-m":
            module = arguments[1]
            return f"申请在 {cwd} 运行 Python 模块 {module}。"
        if arguments and arguments[0] == "-c":
            return f"申请在 {cwd} 执行一段 Python 代码。"
        script = next((argument for argument in arguments if argument.casefold().endswith(".py")), None)
        if script:
            return f"申请在 {cwd} 运行 Python 脚本 {script}。"

    if executable in {"pytest", "pytest.exe"}:
        return f"申请在 {cwd} 运行 Python 测试。"
    if executable in {"git", "git.exe"}:
        operation = arguments[0] if arguments else "命令"
        return f"申请在 {cwd} 执行 Git {operation} 操作。"
    if executable in {"npm", "npm.cmd", "pnpm", "pnpm.cmd", "yarn", "yarn.cmd"}:
        operation = " ".join(arguments[:2]) or "命令"
        return f"申请在 {cwd} 执行前端任务 {operation}。"
    if executable in {"mkdir", "mkdir.exe", "md", "new-item"}:
        target = arguments[-1] if arguments else "指定路径"
        return f"申请在 {cwd} 新建 {target}。"

    return f"申请在 {cwd} 执行 {argv[0]} 命令。"


async def _read_upload_body(request: Request) -> bytearray:
    """Read an octet-stream upload while enforcing the limit during streaming."""

    media_type = request.headers.get("content-type", "").partition(";")[0].strip().casefold()
    if media_type != "application/octet-stream":
        raise HTTPException(status_code=415, detail="upload must use application/octet-stream")

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
        if declared_size < 0:
            raise HTTPException(status_code=400, detail="invalid Content-Length")
        if declared_size > _MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="file exceeds the 10 MiB upload limit")

    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > _MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="file exceeds the 10 MiB upload limit")
    return content


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

    manager = RunManager(agent_factory, workspace=canonical_workspace)
    app = FastAPI(title="TraceCoder Local Web", docs_url=None, redoc_url=None)
    app.state.run_manager = manager

    @app.middleware("http")
    async def prevent_static_cache(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "no-store"
        return response

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
            attachments = _resolve_attachments(canonical_workspace, request.attachments)
            return manager.start(
                request.task,
                attachments=tuple(attachments),
                conversation_id=request.conversation_id,
                project_id=request.project_id,
                scenario=request.scenario,
                tool_policy=request.tool_policy,
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="conversation or project not found") from exc
        except RunConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/runs")
    def run_history() -> dict[str, object]:
        return {"runs": manager.history()}

    @app.get("/api/conversations")
    def conversation_history() -> dict[str, object]:
        return {"conversations": manager.conversation_history()}

    @app.post("/api/projects", status_code=201)
    def create_project(request: CreateProjectRequest) -> dict[str, object]:
        try:
            attachments = _resolve_attachments(canonical_workspace, request.attachments)
            return manager.create_project(
                name=request.name,
                goal=request.goal,
                preferences=request.preferences,
                target_path=request.target_path,
                attachments=tuple(attachments),
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/projects")
    def project_history() -> dict[str, object]:
        return {"projects": manager.project_history()}

    @app.get("/api/projects/{project_id}")
    def project_state(project_id: str) -> dict[str, object]:
        try:
            return manager.project_snapshot(project_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="project not found") from exc

    @app.get("/api/conversations/{conversation_id}")
    def conversation_state(conversation_id: str) -> dict[str, object]:
        try:
            return manager.conversation_snapshot(conversation_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="conversation not found") from exc

    @app.post("/api/uploads", status_code=201)
    async def upload_file(
        request: Request,
        filename: str = Query(min_length=1, max_length=255),
    ) -> dict[str, object]:
        content = await _read_upload_body(request)
        try:
            return _save_upload(canonical_workspace, filename, content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RunConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail="could not save the uploaded file") from exc

    @app.get("/api/runs/active")
    def active_run() -> dict[str, object] | None:
        return manager.active_snapshot()

    @app.get("/api/runs/{run_id}")
    def run_state(run_id: str, after: int = Query(default=0, ge=0)) -> dict[str, object]:
        try:
            return manager.snapshot(run_id, after=after)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.get("/api/runs/{run_id}/proof")
    def run_proof(run_id: str) -> dict[str, object]:
        try:
            return cast(dict[str, object], manager.proof(run_id))
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        except RunConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/proof.md")
    def run_proof_markdown(run_id: str) -> Response:
        try:
            content = render_proof_markdown(manager.proof(run_id))
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        except RunConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Response(
            content=content,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="tracecoder-proof-{run_id}.md"'},
        )

    @app.post("/api/runs/{run_id}/transaction")
    def resolve_run_transaction(run_id: str, request: TransactionRequest) -> dict[str, object]:
        try:
            return manager.resolve_transaction(run_id, request.action)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        except RunConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
