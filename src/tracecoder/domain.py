"""Provider-neutral domain contracts used by the TraceCoder runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import TypeAlias

JSONValue: TypeAlias = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
Message: TypeAlias = dict[str, object]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A validated model request to invoke one local tool."""

    id: str
    name: str
    arguments: dict[str, JSONValue]


@dataclass(frozen=True, slots=True)
class ModelReply:
    """Normalized model output at the provider adapter boundary."""

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    reasoning_content: str | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Serializable result returned by every local tool."""

    ok: bool
    data: dict[str, JSONValue] = field(default_factory=dict)
    error_code: str | None = None
    message: str = ""
    metadata: dict[str, JSONValue] = field(default_factory=dict)

    @classmethod
    def success(
        cls,
        data: dict[str, JSONValue] | None = None,
        *,
        message: str = "",
        metadata: dict[str, JSONValue] | None = None,
    ) -> ToolResult:
        """Build a successful tool result."""

        return cls(True, data or {}, None, message, metadata or {})

    @classmethod
    def failure(
        cls,
        error_code: str,
        message: str,
        *,
        data: dict[str, JSONValue] | None = None,
        metadata: dict[str, JSONValue] | None = None,
    ) -> ToolResult:
        """Build a failed tool result with a stable machine-readable code."""

        return cls(False, data or {}, error_code, message, metadata or {})

    def to_dict(self) -> dict[str, JSONValue]:
        """Convert the result into JSON-serializable primitives."""

        return asdict(self)


class VerificationStatus(StrEnum):
    """Runtime evidence status after known workspace mutations."""

    NOT_REQUIRED = "not_required"
    REQUIRED = "required"
    COMMAND_PASSED = "verify_command_passed"
    COMMAND_FAILED = "verify_command_failed"


class AgentPhase(StrEnum):
    """Explicit phases of the bounded single-agent orchestration state machine."""

    PLAN = "plan"
    EXECUTE = "execute"
    REPLAN = "replan"
    VERIFY = "verify"
    COMPLETE = "complete"
    FAILED = "failed"


class TerminationReason(StrEnum):
    """Deterministic reasons for leaving the agent loop."""

    COMPLETED = "completed"
    MAX_STEPS = "max_steps"
    REPEATED_CALL = "repeated_call"
    PROVIDER_ERROR = "provider_error"
    INTERRUPTED = "interrupted"
    PLAN_FAILED = "plan_failed"


@dataclass(frozen=True, slots=True)
class RunResult:
    """Runtime-derived evidence returned at the end of one agent run."""

    final_text: str
    termination_reason: TerminationReason
    verification_status: VerificationStatus
    changed_files: tuple[str, ...]
    trace_path: str
    steps: int
    shell_side_effects_unknown: bool = False
    proof: dict[str, JSONValue] | None = None
    transaction_id: str | None = None
    transaction_state: str = "not_required"
    rollback_available: bool = False
    proof_json_path: str = ""
    proof_markdown_path: str = ""

    @property
    def successful(self) -> bool:
        """Whether the loop completed without pending or failed runtime checks."""

        return self.termination_reason is TerminationReason.COMPLETED and self.verification_status in {
            VerificationStatus.NOT_REQUIRED,
            VerificationStatus.COMMAND_PASSED,
        }
