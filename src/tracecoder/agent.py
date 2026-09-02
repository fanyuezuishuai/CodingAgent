"""Bounded autonomous model/tool execution loop."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import cast

from tracecoder.context import ContextManager
from tracecoder.domain import (
    AgentPhase,
    JSONValue,
    Message,
    ModelReply,
    RunResult,
    TerminationReason,
    ToolCall,
    ToolResult,
    VerificationStatus,
)
from tracecoder.evidence import build_proof, command_evidence, write_proof_artifacts
from tracecoder.llm.base import ModelClient
from tracecoder.tools.registry import ToolRegistry
from tracecoder.trace import TraceRecorder
from tracecoder.transaction import TransactionError, WorkspaceTransaction

SYSTEM_PROMPT = """You are TraceCoder, a local coding agent.
Inspect first. Before the first file change or command, establish one plan by putting update_plan first in that reply.
The runtime advances its active step after each successful action batch. Do not repeat update_plan unless an action
fails; then revise the remaining plan once. Make focused edits, verify changes, and claim only observed verification.
When the task is complete, return a concise summary with no tool calls.
"""

COMPLETION_REMINDER = """Runtime reminder: {requirements}. Continue with update_plan and the appropriate tools,
or explicitly explain in your next final response why the remaining work cannot be performed.
"""

UPDATE_PLAN_TOOL = "update_plan"
MAX_PLAN_STEPS = 8
MUTATION_TOOLS = frozenset({"create_directory", "replace_text", "write_file"})
UPDATE_PLAN_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": UPDATE_PLAN_TOOL,
        "description": "Manage the ordered runtime plan.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": MAX_PLAN_STEPS,
                },
            },
            "required": ["steps"],
        },
    },
}


@dataclass(slots=True)
class _PlanState:
    """Runtime-owned ordered plan and its bounded replan budget."""

    phase: AgentPhase = AgentPhase.PLAN
    version: int = 0
    steps: tuple[str, ...] = ()
    active_step: int | None = None
    completed_steps: set[int] = field(default_factory=set)
    replans_used: int = 0

    @property
    def established(self) -> bool:
        return bool(self.steps)

    @property
    def complete(self) -> bool:
        return self.established and len(self.completed_steps) == len(self.steps)

    @property
    def needs_replan(self) -> bool:
        return self.phase is AgentPhase.REPLAN

    @property
    def blocked(self) -> bool:
        return self.phase is AgentPhase.FAILED

    def update(self, arguments: dict[str, JSONValue]) -> tuple[ToolResult, str | None]:
        validation = _validated_plan_update(arguments)
        if isinstance(validation, ToolResult):
            return validation, None
        steps = validation

        if not self.established:
            self._replace(steps)
            return ToolResult.success({"kind": "created", "version": self.version}), "created"

        if self.needs_replan:
            if self.replans_used >= 1:
                return (
                    ToolResult.failure("replan_limit_exceeded", "The single allowed replan was already used."),
                    None,
                )
            self.replans_used += 1
            self._replace(steps)
            return ToolResult.success({"kind": "replanned", "version": self.version}), "replanned"

        return (
            ToolResult.failure(
                "plan_change_not_allowed",
                "The plan may be replaced only once, immediately after a failed action batch.",
            ),
            None,
        )

    def finish_batch(self, ok: bool) -> AgentPhase | None:
        if self.active_step is None:
            return None
        if ok:
            self.completed_steps.add(self.active_step)
            next_step = self.active_step + 1
            self.active_step = next_step if next_step <= len(self.steps) else None
            return None
        self.active_step = None
        return AgentPhase.FAILED if self.replans_used >= 1 else AgentPhase.REPLAN

    def snapshot(self) -> dict[str, JSONValue]:
        return {
            "phase": self.phase.value,
            "version": self.version,
            "steps": cast(list[JSONValue], list(self.steps)),
            "active_step": self.active_step,
            "completed_steps": cast(list[JSONValue], sorted(self.completed_steps)),
            "replans_used": self.replans_used,
            "needs_replan": self.needs_replan,
            "blocked": self.blocked,
        }

    def _replace(self, steps: tuple[str, ...]) -> None:
        self.version += 1
        self.steps = steps
        self.active_step = 1
        self.completed_steps.clear()


class Agent:
    """Coordinate normalized model replies and locally executed tools."""

    def __init__(
        self,
        model: ModelClient,
        registry: ToolRegistry,
        context: ContextManager,
        trace: TraceRecorder,
        *,
        max_steps: int = 20,
        repeat_limit: int = 3,
        cancelled: Callable[[], bool] | None = None,
        transaction: WorkspaceTransaction | None = None,
    ) -> None:
        if max_steps <= 0 or repeat_limit <= 0:
            raise ValueError("max_steps and repeat_limit must be positive")
        self.model = model
        self.registry = registry
        self.context = context
        self.trace = trace
        self.max_steps = max_steps
        self.repeat_limit = repeat_limit
        self.cancelled = cancelled or (lambda: False)
        self.transaction = transaction
        self._conversation_messages: list[Message] = []
        self._current_task = ""
        self._command_evidence: list[dict[str, JSONValue]] = []
        self._allowed_tools: frozenset[str] | None = None
        self._plan_state = _PlanState()

    @property
    def conversation_messages(self) -> tuple[Message, ...]:
        """Return reusable non-system messages from the latest run."""

        return tuple(dict(message) for message in _reusable_messages(self._conversation_messages))

    def set_tool_allowlist(self, names: Sequence[str] | None) -> None:
        """Restrict model-visible and executable tools for this isolated run."""

        self._allowed_tools = frozenset(names) if names is not None else None

    def _schemas_for_model(self) -> list[dict[str, object]]:
        schemas = self.registry.schemas_for_model(self._allowed_tools)
        registry_schemas: list[dict[str, object]] = []
        for schema in schemas:
            function = schema.get("function")
            if isinstance(function, dict) and function.get("name") == UPDATE_PLAN_TOOL:
                continue
            registry_schemas.append(schema)
        if self._tool_is_disallowed(UPDATE_PLAN_TOOL):
            return registry_schemas
        return [UPDATE_PLAN_SCHEMA, *registry_schemas]

    def _tool_is_disallowed(self, name: str) -> bool:
        return self._allowed_tools is not None and name not in self._allowed_tools

    def _set_phase(self, phase: AgentPhase) -> None:
        previous = self._plan_state.phase
        if previous is phase:
            return
        self._plan_state.phase = phase
        self.trace.record(
            "phase_changed",
            {
                "from": previous.value,
                "to": phase.value,
                "plan_version": self._plan_state.version,
            },
        )

    def run(self, task: str, *, history: Sequence[Message] = ()) -> RunResult:
        """Run one task until a deterministic terminal condition is reached."""

        if not task.strip():
            raise ValueError("task must not be empty")
        self._current_task = task.strip()
        self._command_evidence = []
        self._plan_state = _PlanState()
        messages: list[Message] = [
            {"role": "system", "content": SYSTEM_PROMPT.strip()},
            *(dict(message) for message in history if message.get("role") != "system"),
            {"role": "user", "content": task.strip()},
        ]
        self._conversation_messages = messages
        changed_files: list[str] = []
        verification = VerificationStatus.NOT_REQUIRED
        shell_side_effects_unknown = False
        plan_required_seen = False
        plan_reminder_sent = False
        verification_reminder_sent = False
        last_fingerprint: str | None = None
        repeat_count = 0
        failures: list[str] = []
        steps = 0
        self.trace.record(
            "run_started",
            {
                "task": task.strip(),
                "max_steps": self.max_steps,
                "history_messages": len(messages) - 2,
                "orchestration": self._plan_state.snapshot(),
            },
        )
        model_tools = self._schemas_for_model()

        try:
            for steps in range(1, self.max_steps + 1):
                if self.cancelled():
                    return self._finish_interrupted(
                        verification,
                        changed_files,
                        steps - 1,
                        shell_side_effects_unknown,
                    )
                runtime_summary = _runtime_summary(
                    changed_files,
                    verification,
                    shell_side_effects_unknown,
                    failures,
                    self._plan_state,
                )
                request_messages = self.context.prepare(messages, runtime_summary)
                model_started = time.monotonic()
                reply = self.model.complete(
                    request_messages,
                    model_tools,
                )
                model_elapsed = round(time.monotonic() - model_started, 4)
                self.trace.record(
                    "model_reply",
                    {
                        "step": steps,
                        "content": reply.content,
                        "tool_calls": [_tool_call_payload(call) for call in reply.tool_calls],
                        "elapsed_seconds": model_elapsed,
                    },
                )
                if self.cancelled():
                    return self._finish_interrupted(
                        verification,
                        changed_files,
                        steps,
                        shell_side_effects_unknown,
                    )
                messages.append(_assistant_message(reply))

                if not reply.tool_calls:
                    plan_incomplete = (
                        plan_required_seen or self._plan_state.established
                    ) and not self._plan_state.complete
                    verification_incomplete = verification in {
                        VerificationStatus.REQUIRED,
                        VerificationStatus.COMMAND_FAILED,
                    }
                    remind_plan = plan_incomplete and not plan_reminder_sent
                    remind_verification = verification_incomplete and not verification_reminder_sent
                    if remind_plan or remind_verification:
                        requirements = []
                        if remind_plan:
                            plan_reminder_sent = True
                            requirements.append("the explicit plan still has incomplete steps")
                        if remind_verification:
                            verification_reminder_sent = True
                            requirements.append("workspace changes are not verified")
                        messages.append(
                            {
                                "role": "system",
                                "content": COMPLETION_REMINDER.format(requirements="; ".join(requirements)).strip(),
                            }
                        )
                        self.trace.record(
                            "completion_reminder",
                            {
                                "plan_incomplete": plan_incomplete,
                                "verification": verification.value,
                            },
                        )
                        continue
                    if plan_incomplete:
                        return self._finish(
                            reply.content or "Stopped with an incomplete plan.",
                            TerminationReason.PLAN_FAILED,
                            verification,
                            changed_files,
                            steps,
                            shell_side_effects_unknown,
                        )
                    return self._finish(
                        reply.content,
                        TerminationReason.COMPLETED,
                        verification,
                        changed_files,
                        steps,
                        shell_side_effects_unknown,
                    )

                action_seen = False
                plan_control_seen = False
                plan_control_failed = False
                batch_step: int | None = None
                batch_results: list[bool] = []
                batch_aborted = False
                for call in reply.tool_calls:
                    if self.cancelled():
                        return self._finish_interrupted(
                            verification,
                            changed_files,
                            steps,
                            shell_side_effects_unknown,
                        )
                    fingerprint = _tool_fingerprint(call)
                    if fingerprint == last_fingerprint:
                        repeat_count += 1
                    else:
                        last_fingerprint = fingerprint
                        repeat_count = 1
                    if repeat_count >= self.repeat_limit:
                        return self._finish(
                            f"Stopped after {repeat_count} repeated calls to {call.name}.",
                            TerminationReason.REPEATED_CALL,
                            verification,
                            changed_files,
                            steps,
                            shell_side_effects_unknown,
                        )

                    is_plan_control = call.name == UPDATE_PLAN_TOOL
                    if not is_plan_control:
                        action_seen = True
                    tool_started = time.monotonic()
                    plan_update_kind: str | None = None
                    result: ToolResult | None = None
                    perform_plan_update = False
                    execute_registry = False
                    if is_plan_control:
                        if self._tool_is_disallowed(call.name):
                            result = ToolResult.failure(
                                "tool_not_allowed",
                                f"Tool is disabled for this run: {call.name}",
                            )
                        elif action_seen or plan_control_seen:
                            result = ToolResult.failure(
                                "plan_order_invalid",
                                "Call update_plan once and before all action tools in the same reply.",
                            )
                        else:
                            plan_control_seen = True
                            perform_plan_update = True
                    elif self._tool_is_disallowed(call.name):
                        execute_registry = True
                    else:
                        if _requires_plan(call):
                            plan_required_seen = True
                        if batch_aborted:
                            result = ToolResult.failure(
                                "action_batch_aborted",
                                "A preceding action in this batch failed, so this action was not executed.",
                            )
                        elif plan_control_failed:
                            result = ToolResult.failure(
                                "plan_update_failed",
                                "The preceding update_plan call failed, so this action was not executed.",
                            )
                        elif self._plan_state.needs_replan:
                            result = ToolResult.failure(
                                "replan_required",
                                "Revise the remaining plan before executing another action.",
                            )
                        elif _requires_plan(call) and not self._plan_state.established:
                            result = ToolResult.failure(
                                "plan_required",
                                "Create an explicit plan with update_plan before workspace-changing actions.",
                            )
                        elif self._plan_state.established and self._plan_state.active_step is None:
                            result = ToolResult.failure(
                                "plan_step_required",
                                "The runtime plan has no active step for this action.",
                            )
                        else:
                            if self._plan_state.established:
                                phase = AgentPhase.VERIFY if _is_verification_call(call) else AgentPhase.EXECUTE
                                self._set_phase(phase)
                            execute_registry = True
                    plan_link = _plan_link(self._plan_state)
                    self.trace.record(
                        "tool_requested",
                        {"step": steps, **_tool_call_payload(call), **plan_link},
                    )
                    if perform_plan_update:
                        result, plan_update_kind = self._plan_state.update(call.arguments)
                        if result.ok:
                            self._set_phase(AgentPhase.EXECUTE)
                            self.trace.record(
                                "plan_updated",
                                {
                                    "step": steps,
                                    "tool_call_id": call.id,
                                    "kind": plan_update_kind,
                                    "orchestration": self._plan_state.snapshot(),
                                },
                            )
                        else:
                            plan_control_failed = True
                    elif execute_registry:
                        result = self.registry.execute(
                            call.name,
                            call.arguments,
                            allowed_names=self._allowed_tools,
                        )
                    if result is None:
                        raise RuntimeError("Tool dispatch produced no result")
                    tool_elapsed = round(time.monotonic() - tool_started, 4)
                    redacted_result = self.trace.redact(result.to_dict())
                    if not is_plan_control and isinstance(redacted_result, dict):
                        observed_command = command_evidence(call, result, redacted_result)
                        if observed_command is not None:
                            self._command_evidence.append(observed_command)
                    self.trace.record(
                        "tool_result",
                        {
                            "step": steps,
                            "tool_call_id": call.id,
                            "tool": call.name,
                            "result": redacted_result,
                            "elapsed_seconds": tool_elapsed,
                            **plan_link,
                        },
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(redacted_result, ensure_ascii=False, separators=(",", ":")),
                        }
                    )
                    if not is_plan_control:
                        verification, shell_side_effects_unknown = _update_evidence(
                            call,
                            result,
                            changed_files,
                            verification,
                            shell_side_effects_unknown,
                        )
                        if self._plan_state.active_step is not None:
                            batch_step = self._plan_state.active_step
                            batch_results.append(result.ok)
                            if not result.ok:
                                batch_aborted = True
                    if not result.ok and result.error_code:
                        failures.append(f"{call.name}:{result.error_code}")
                        failures = failures[-5:]

                if batch_results and batch_step is not None:
                    batch_ok = all(batch_results)
                    next_phase = self._plan_state.finish_batch(batch_ok)
                    if next_phase is not None:
                        self._set_phase(next_phase)
                    self.trace.record(
                        "plan_step_completed" if batch_ok else "plan_step_failed",
                        {
                            "step": steps,
                            "plan_version": self._plan_state.version,
                            "plan_step": batch_step,
                            "orchestration": self._plan_state.snapshot(),
                        },
                    )
                    if next_phase is AgentPhase.FAILED:
                        return self._finish(
                            "Stopped because the plan failed after its single allowed replan.",
                            TerminationReason.PLAN_FAILED,
                            verification,
                            changed_files,
                            steps,
                            shell_side_effects_unknown,
                        )

            return self._finish(
                f"Stopped after reaching the {self.max_steps}-step limit.",
                TerminationReason.MAX_STEPS,
                verification,
                changed_files,
                steps,
                shell_side_effects_unknown,
            )
        except KeyboardInterrupt:
            return self._finish_interrupted(
                verification,
                changed_files,
                steps,
                shell_side_effects_unknown,
            )
        except Exception as exc:
            safe_error = str(self.trace.redact(f"{type(exc).__name__}: {exc}"))
            return self._finish(
                f"Provider or protocol error: {safe_error}",
                TerminationReason.PROVIDER_ERROR,
                verification,
                changed_files,
                steps,
                shell_side_effects_unknown,
            )

    def _finish_interrupted(
        self,
        verification: VerificationStatus,
        changed_files: list[str],
        steps: int,
        shell_side_effects_unknown: bool,
    ) -> RunResult:
        return self._finish(
            "Interrupted by the user.",
            TerminationReason.INTERRUPTED,
            verification,
            changed_files,
            steps,
            shell_side_effects_unknown,
        )

    def _finish(
        self,
        final_text: str,
        reason: TerminationReason,
        verification: VerificationStatus,
        changed_files: list[str],
        steps: int,
        shell_side_effects_unknown: bool,
    ) -> RunResult:
        if reason is TerminationReason.COMPLETED:
            self._set_phase(AgentPhase.COMPLETE)
        elif reason is TerminationReason.PLAN_FAILED:
            self._set_phase(AgentPhase.FAILED)
        safe_final_text = str(self.trace.redact(final_text))
        if self.transaction is not None and self.transaction.state == "pending":
            try:
                self.transaction.seal()
            except (OSError, TransactionError) as exc:
                self.trace.record(
                    "transaction_seal_failed",
                    {"error": str(self.trace.redact(f"{type(exc).__name__}: {exc}"))},
                )
        last_non_system = next(
            (message for message in reversed(self._conversation_messages) if message.get("role") != "system"),
            None,
        )
        if (
            last_non_system is None
            or last_non_system.get("role") != "assistant"
            or last_non_system.get("content") != safe_final_text
            or last_non_system.get("tool_calls")
        ):
            self._conversation_messages.append({"role": "assistant", "content": safe_final_text})
        proof = build_proof(
            run_id=self.trace.session_id,
            task=str(self.trace.redact(self._current_task)),
            termination_reason=reason.value,
            verification_status=verification.value,
            changed_files=changed_files,
            trace_path=str(self.trace.path),
            steps=steps,
            shell_side_effects_unknown=shell_side_effects_unknown,
            commands=self._command_evidence,
            transaction=self.transaction,
        )
        proof_json_path = ""
        proof_markdown_path = ""
        try:
            json_path, markdown_path = write_proof_artifacts(
                self.trace.workspace,
                self.trace.session_id,
                proof,
            )
            proof_json_path = str(json_path)
            proof_markdown_path = str(markdown_path)
        except OSError as exc:
            self.trace.record("proof_export_failed", {"error_type": type(exc).__name__})
        transaction_state = self.transaction.state if self.transaction is not None else "not_required"
        rollback_available = self.transaction.rollback_available if self.transaction is not None else False
        result = RunResult(
            final_text=safe_final_text,
            termination_reason=reason,
            verification_status=verification,
            changed_files=tuple(changed_files),
            trace_path=str(self.trace.path),
            steps=steps,
            shell_side_effects_unknown=shell_side_effects_unknown,
            proof=proof,
            transaction_id=self.transaction.id if self.transaction is not None else None,
            transaction_state=transaction_state,
            rollback_available=rollback_available,
            proof_json_path=proof_json_path,
            proof_markdown_path=proof_markdown_path,
        )
        self.trace.record(
            "run_finished",
            {
                "reason": reason.value,
                "verification": verification.value,
                "changed_files": changed_files,
                "steps": steps,
                "shell_side_effects_unknown": shell_side_effects_unknown,
                "transaction_state": transaction_state,
                "rollback_available": rollback_available,
                "proof_json_path": proof_json_path,
                "proof_markdown_path": proof_markdown_path,
                "orchestration": self._plan_state.snapshot(),
            },
        )
        return result


def _validated_plan_update(
    arguments: dict[str, JSONValue],
) -> tuple[str, ...] | ToolResult:
    unknown = sorted(set(arguments) - {"steps"})
    if unknown:
        return ToolResult.failure("invalid_plan", f"Unknown plan argument(s): {', '.join(unknown)}")
    raw_steps = arguments.get("steps")
    if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= MAX_PLAN_STEPS:
        return ToolResult.failure("invalid_plan", f"Plan must contain 1 to {MAX_PLAN_STEPS} steps.")
    if any(not isinstance(step, str) or not step.strip() for step in raw_steps):
        return ToolResult.failure("invalid_plan", "Every plan step must be a non-empty string.")
    return tuple(step.strip() for step in raw_steps if isinstance(step, str))


def _requires_plan(call: ToolCall) -> bool:
    return call.name in MUTATION_TOOLS or call.name == "run_command"


def _is_verification_call(call: ToolCall) -> bool:
    return call.name == "run_command" and call.arguments.get("purpose", "work") == "verify"


def _plan_link(plan: _PlanState) -> dict[str, JSONValue]:
    return {
        "phase": plan.phase.value,
        "plan_version": plan.version if plan.established else None,
        "plan_step": plan.active_step,
    }


def _assistant_message(reply: ModelReply) -> Message:
    message: Message = {"role": "assistant", "content": reply.content}
    if reply.reasoning_content is not None:
        message["reasoning_content"] = reply.reasoning_content
    if reply.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False, separators=(",", ":")),
                },
            }
            for call in reply.tool_calls
        ]
    return message


def _reusable_messages(messages: list[Message]) -> list[Message]:
    """Drop system reminders, orphan tools, and incomplete tool-call bundles."""

    reusable: list[Message] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        role = message.get("role")
        if role == "system" or role == "tool":
            index += 1
            continue
        tool_calls = message.get("tool_calls") if role == "assistant" else None
        if not isinstance(tool_calls, list) or not tool_calls:
            reusable.append(message)
            index += 1
            continue

        expected_ids = {
            str(call.get("id"))
            for call in tool_calls
            if isinstance(call, dict) and call.get("id") is not None
        }
        bundle = [message]
        response_ids: set[str] = set()
        index += 1
        while index < len(messages) and messages[index].get("role") == "tool":
            tool_message = messages[index]
            bundle.append(tool_message)
            if tool_message.get("tool_call_id") is not None:
                response_ids.add(str(tool_message["tool_call_id"]))
            index += 1
        if expected_ids and expected_ids <= response_ids:
            reusable.extend(bundle)
    return reusable


def _tool_call_payload(call: ToolCall) -> dict[str, object]:
    return {"tool_call_id": call.id, "tool": call.name, "arguments": call.arguments}


def _tool_fingerprint(call: ToolCall) -> str:
    arguments = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{call.name}:{arguments}"


def _runtime_summary(
    changed_files: list[str],
    verification: VerificationStatus,
    shell_side_effects_unknown: bool,
    failures: list[str],
    plan: _PlanState,
) -> str:
    return (
        f"known_changed_files={changed_files or 'none'}; "
        f"verification={verification.value}; "
        f"shell_side_effects_unknown={shell_side_effects_unknown}; "
        f"recent_failures={failures or 'none'}; "
        f"orchestration={json.dumps(plan.snapshot(), ensure_ascii=False, separators=(',', ':'))}"
    )


def _update_evidence(
    call: ToolCall,
    result: ToolResult,
    changed_files: list[str],
    verification: VerificationStatus,
    shell_side_effects_unknown: bool,
) -> tuple[VerificationStatus, bool]:
    changed_file = result.metadata.get("changed_file") if result.ok else None
    if isinstance(changed_file, str):
        if changed_file not in changed_files:
            changed_files.append(changed_file)
        verification = VerificationStatus.REQUIRED

    changed_directory = result.metadata.get("changed_directory") if result.ok else None
    if isinstance(changed_directory, str):
        verification = VerificationStatus.REQUIRED

    process_started = result.metadata.get("shell_side_effects_unknown") is True
    if call.name == "run_command" and process_started:
        shell_side_effects_unknown = True
        if _is_verification_call(call):
            verification = (
                VerificationStatus.COMMAND_PASSED
                if result.ok
                else VerificationStatus.COMMAND_FAILED
            )
        else:
            verification = VerificationStatus.REQUIRED
    return verification, shell_side_effects_unknown
