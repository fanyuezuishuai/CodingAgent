"""Bounded autonomous model/tool execution loop."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence

from tracecoder.context import ContextManager
from tracecoder.domain import (
    Message,
    ModelReply,
    RunResult,
    TerminationReason,
    ToolCall,
    ToolResult,
    VerificationStatus,
)
from tracecoder.llm.base import ModelClient
from tracecoder.tools.registry import ToolRegistry
from tracecoder.trace import TraceRecorder

SYSTEM_PROMPT = """You are TraceCoder, a local coding agent.
Inspect the workspace with tools before changing it. Make focused edits, handle tool errors, and use
run_command with purpose='verify' after modifications. Do not claim verification that the runtime did not observe.
When the task is complete, return a concise summary with no tool calls.
"""

VERIFICATION_REMINDER = """Runtime reminder: workspace changes are not verified. Run an appropriate command with
purpose='verify', or explicitly explain in your next final response why verification cannot be performed.
"""


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
        self._conversation_messages: list[Message] = []

    @property
    def conversation_messages(self) -> tuple[Message, ...]:
        """Return reusable non-system messages from the latest run."""

        return tuple(dict(message) for message in _reusable_messages(self._conversation_messages))

    def run(self, task: str, *, history: Sequence[Message] = ()) -> RunResult:
        """Run one task until a deterministic terminal condition is reached."""

        if not task.strip():
            raise ValueError("task must not be empty")
        messages: list[Message] = [
            {"role": "system", "content": SYSTEM_PROMPT.strip()},
            *(dict(message) for message in history if message.get("role") != "system"),
            {"role": "user", "content": task.strip()},
        ]
        self._conversation_messages = messages
        changed_files: list[str] = []
        verification = VerificationStatus.NOT_REQUIRED
        shell_side_effects_unknown = False
        reminder_sent = False
        last_fingerprint: str | None = None
        repeat_count = 0
        failures: list[str] = []
        steps = 0
        self.trace.record(
            "run_started",
            {"task": task.strip(), "max_steps": self.max_steps, "history_messages": len(messages) - 2},
        )

        try:
            for steps in range(1, self.max_steps + 1):
                if self.cancelled():
                    return self._finish_interrupted(
                        verification,
                        changed_files,
                        steps - 1,
                        shell_side_effects_unknown,
                    )
                runtime_summary = _runtime_summary(changed_files, verification, shell_side_effects_unknown, failures)
                request_messages = self.context.prepare(messages, runtime_summary)
                model_started = time.monotonic()
                reply = self.model.complete(request_messages, self.registry.schemas_for_model())
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
                    if verification in {VerificationStatus.REQUIRED, VerificationStatus.FAILED} and not reminder_sent:
                        reminder_sent = True
                        messages.append({"role": "system", "content": VERIFICATION_REMINDER.strip()})
                        self.trace.record("verification_reminder", {"status": verification.value})
                        continue
                    return self._finish(
                        reply.content,
                        TerminationReason.COMPLETED,
                        verification,
                        changed_files,
                        steps,
                        shell_side_effects_unknown,
                    )

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

                    self.trace.record("tool_requested", {"step": steps, **_tool_call_payload(call)})
                    tool_started = time.monotonic()
                    result = self.registry.execute(call.name, call.arguments)
                    tool_elapsed = round(time.monotonic() - tool_started, 4)
                    redacted_result = self.trace.redact(result.to_dict())
                    self.trace.record(
                        "tool_result",
                        {
                            "step": steps,
                            "tool_call_id": call.id,
                            "tool": call.name,
                            "result": redacted_result,
                            "elapsed_seconds": tool_elapsed,
                        },
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(redacted_result, ensure_ascii=False, separators=(",", ":")),
                        }
                    )
                    verification, shell_side_effects_unknown = _update_evidence(
                        call,
                        result,
                        changed_files,
                        verification,
                        shell_side_effects_unknown,
                    )
                    if not result.ok and result.error_code:
                        failures.append(f"{call.name}:{result.error_code}")
                        failures = failures[-5:]

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
        safe_final_text = str(self.trace.redact(final_text))
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
        result = RunResult(
            final_text=safe_final_text,
            termination_reason=reason,
            verification_status=verification,
            changed_files=tuple(changed_files),
            trace_path=str(self.trace.path),
            steps=steps,
            shell_side_effects_unknown=shell_side_effects_unknown,
        )
        self.trace.record(
            "run_finished",
            {
                "reason": reason.value,
                "verification": verification.value,
                "changed_files": changed_files,
                "steps": steps,
                "shell_side_effects_unknown": shell_side_effects_unknown,
            },
        )
        return result


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
) -> str:
    return (
        f"known_changed_files={changed_files or 'none'}; "
        f"verification={verification.value}; "
        f"shell_side_effects_unknown={shell_side_effects_unknown}; "
        f"recent_failures={failures or 'none'}"
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

    process_started = result.metadata.get("shell_side_effects_unknown") is True
    if call.name == "run_command" and process_started:
        shell_side_effects_unknown = True
        purpose = call.arguments.get("purpose", "work")
        if purpose == "verify":
            verification = VerificationStatus.VERIFIED if result.ok else VerificationStatus.FAILED
        else:
            verification = VerificationStatus.REQUIRED
    return verification, shell_side_effects_unknown
