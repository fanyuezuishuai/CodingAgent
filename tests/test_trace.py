"""JSONL trace and redaction tests."""

import json
from pathlib import Path
from threading import Thread

import pytest

from tracecoder.trace import TraceFormatError, TraceRecorder, read_trace


def test_trace_is_jsonl_and_redacts_secrets(tmp_path: Path) -> None:
    recorder = TraceRecorder(tmp_path, secrets=["sentinel-secret"], session_id="session-test")

    recorder.record("model_reply", {"content": "key=sentinel-secret", "nested": ["sentinel-secret"]})
    recorder.record("completed", {"reason": "done"})

    events = read_trace(recorder.path)
    assert [event["seq"] for event in events] == [1, 2]
    assert events[0]["payload"]["content"] == "key=[REDACTED]"
    assert "sentinel-secret" not in recorder.path.read_text(encoding="utf-8")
    for line in recorder.path.read_text(encoding="utf-8").splitlines():
        json.loads(line)


def test_read_trace_rejects_corrupt_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text('{"event_type": "ok"}\nnot-json\n', encoding="utf-8")

    with pytest.raises(TraceFormatError, match="line 2"):
        read_trace(path)


def test_trace_observer_receives_the_redacted_stored_event(tmp_path: Path) -> None:
    observed: list[dict[str, object]] = []
    recorder = TraceRecorder(
        tmp_path,
        secrets=["sentinel-secret"],
        session_id="observed",
        observer=observed.append,
    )

    stored = recorder.record("tool_result", {"output": "sentinel-secret"})

    assert observed == [stored]
    assert observed[0]["payload"] == {"output": "[REDACTED]"}


def test_trace_recorder_serializes_concurrent_records(tmp_path: Path) -> None:
    recorder = TraceRecorder(tmp_path, session_id="concurrent")
    threads = [Thread(target=recorder.record, args=("worker", {"worker": index})) for index in range(20)]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)

    events = read_trace(recorder.path)
    assert len(events) == 20
    assert sorted(event["seq"] for event in events) == list(range(1, 21))
