"""Shared construction of the TraceCoder runtime for CLI and web entry points."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from tracecoder.agent import Agent
from tracecoder.config import Settings
from tracecoder.context import ContextManager
from tracecoder.llm.openai_compatible import OpenAICompatibleClient
from tracecoder.tools import build_tool_registry
from tracecoder.trace import TraceRecorder
from tracecoder.transaction import WorkspaceTransaction


def build_agent(
    workspace: Path,
    settings: Settings,
    approval: Callable[[list[str], Path], bool],
    *,
    observer: Callable[[dict[str, object]], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    session_id: str | None = None,
    max_steps: int | None = None,
    repeat_limit: int | None = None,
) -> Agent:
    """Build one isolated agent run from validated settings."""

    trace = TraceRecorder(
        workspace,
        secrets=settings.redaction_secrets,
        session_id=session_id,
        observer=observer,
    )
    transaction = WorkspaceTransaction(workspace, trace.session_id)
    registry = build_tool_registry(
        workspace,
        approval,
        default_timeout_seconds=settings.command_timeout_seconds,
        max_output_bytes=settings.command_output_bytes,
        transaction=transaction,
    )
    model = OpenAICompatibleClient(settings.api_key, settings.base_url, settings.model)
    return Agent(
        model,
        registry,
        ContextManager(settings.context_max_chars),
        trace,
        max_steps=settings.max_steps if max_steps is None else max_steps,
        repeat_limit=settings.repeat_limit if repeat_limit is None else repeat_limit,
        cancelled=cancelled,
        transaction=transaction,
    )
