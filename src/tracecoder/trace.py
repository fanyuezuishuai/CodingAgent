"""Append-only JSONL execution traces with recursive secret redaction."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


class TraceFormatError(ValueError):
    """Raised when a stored trace is not valid JSONL event data."""


class TraceRecorder:
    """Write ordered runtime events below the reserved trace directory."""

    def __init__(
        self,
        workspace: Path,
        *,
        secrets: list[str] | tuple[str, ...] = (),
        session_id: str | None = None,
        observer: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.session_id = session_id or uuid4().hex
        self._secrets = tuple(secret for secret in secrets if secret)
        trace_directory = workspace.resolve(strict=True) / ".tracecoder" / "traces"
        trace_directory.mkdir(parents=True, exist_ok=True)
        self.path = trace_directory / f"{self.session_id}.jsonl"
        self._sequence = 0
        self._observer = observer
        self._lock = threading.RLock()

    def redact(self, value: Any) -> Any:
        """Recursively redact configured secret substrings from a JSON-like value."""

        if isinstance(value, str):
            redacted = value
            for secret in self._secrets:
                redacted = redacted.replace(secret, "[REDACTED]")
            return redacted
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, tuple):
            return [self.redact(item) for item in value]
        if isinstance(value, dict):
            return {str(key): self.redact(item) for key, item in value.items()}
        return value

    def record(
        self,
        event_type: str,
        payload: dict[str, object] | None = None,
        *,
        notify_observer: bool = True,
    ) -> dict[str, object]:
        """Append one redacted event and return the stored representation."""

        with self._lock:
            self._sequence += 1
            event: dict[str, object] = {
                "session_id": self.session_id,
                "seq": self._sequence,
                "timestamp": datetime.now(UTC).isoformat(),
                "event_type": event_type,
                "payload": self.redact(payload or {}),
            }
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            if notify_observer and self._observer is not None:
                # The re-entrant lock keeps observer delivery in trace order without
                # deadlocking an observer that records a follow-up event.
                with suppress(Exception):
                    self._observer(event)
        return event


def read_trace(path: Path) -> list[dict[str, Any]]:
    """Read and validate a JSONL trace for CLI presentation."""

    if not path.is_file():
        raise TraceFormatError(f"Trace file does not exist: {path}")
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TraceFormatError(f"Invalid JSON at line {line_number}") from exc
            if not isinstance(event, dict) or "event_type" not in event:
                raise TraceFormatError(f"Invalid trace event at line {line_number}")
            events.append(event)
    return events
