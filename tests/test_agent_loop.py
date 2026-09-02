"""Offline end-to-end tests for the autonomous loop."""

import json
import sys
from pathlib import Path

import pytest

from tests.fakes import FakeModelClient, plan_call
from tracecoder.agent import Agent
from tracecoder.context import ContextManager
from tracecoder.domain import JSONValue, ModelReply, TerminationReason, ToolCall, VerificationStatus
from tracecoder.tools import build_tool_registry
from tracecoder.trace import TraceRecorder, read_trace
from tracecoder.transaction import WorkspaceTransaction


def _agent(
    tmp_path: Path,
    replies: list[ModelReply],
    *,
    repeat_limit: int = 3,
    max_steps: int = 10,
    context_max_chars: int = 10_000,
) -> tuple[Agent, FakeModelClient, TraceRecorder]:
    model = FakeModelClient(replies)
    trace = TraceRecorder(tmp_path, secrets=["sentinel-secret"], session_id="agent-test")
    registry = build_tool_registry(tmp_path, lambda _argv, _cwd: True)
    agent = Agent(
        model,
        registry,
        ContextManager(max_chars=context_max_chars),
        trace,
        max_steps=max_steps,
        repeat_limit=repeat_limit,
    )
    return agent, model, trace


def test_fake_model_can_edit_verify_and_complete(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    steps = ["Update app.py", "Verify app.py"]
    replies = [
        ModelReply(tool_calls=(ToolCall("read", "read_file", {"path": "app.py"}),)),
        ModelReply(
            tool_calls=(
                plan_call("plan-edit", steps),
                ToolCall(
                    "write",
                    "replace_text",
                    {"path": "app.py", "old": "1", "new": "2"},
                ),
            )
        ),
        ModelReply(
            tool_calls=(
                ToolCall(
                    "verify",
                    "run_command",
                    {
                        "argv": [sys.executable, "-c", "from pathlib import Path; assert '2' in Path('app.py').read_text()"],
                        "purpose": "verify",
                    },
                ),
            )
        ),
        ModelReply(content="Updated app.py and verified the result."),
    ]
    agent, model, trace = _agent(tmp_path, replies)

    result = agent.run("Change the value to 2")

    assert result.termination_reason is TerminationReason.COMPLETED
    assert result.verification_status is VerificationStatus.COMMAND_PASSED
    assert result.changed_files == ("app.py",)
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert len(model.requests) == 4
    assert any(message.get("tool_call_id") == "verify" for message in model.requests[-1])
    events = read_trace(trace.path)
    assert events[-1]["event_type"] == "run_finished"
    assert events[-1]["payload"]["reason"] == "completed"
    phase_changes = [event["payload"]["to"] for event in events if event["event_type"] == "phase_changed"]
    assert phase_changes[-2:] == ["verify", "complete"]


def test_mutation_without_an_explicit_plan_is_blocked(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    replies = [
        ModelReply(
            tool_calls=(
                ToolCall(
                    "write",
                    "write_file",
                    {"path": "app.py", "content": "value = 2\n", "overwrite": True},
                ),
            )
        ),
        ModelReply(content="I did not modify the file."),
        ModelReply(content="I still cannot establish and complete a plan."),
    ]
    agent, model, _trace = _agent(tmp_path, replies)

    result = agent.run("Change the value to 2")

    assert target.read_text(encoding="utf-8") == "value = 1\n"
    assert result.changed_files == ()
    assert result.termination_reason is TerminationReason.PLAN_FAILED
    visible_names = {tool["function"]["name"] for tool in model.tool_requests[0]}
    assert "update_plan" in visible_names
    tool_messages = [message for message in model.requests[1] if message.get("role") == "tool"]
    assert json.loads(str(tool_messages[0]["content"]))["error_code"] == "plan_required"


def test_run_command_requires_a_plan_even_when_declared_as_verification(tmp_path: Path) -> None:
    replies = [
        ModelReply(
            tool_calls=(
                ToolCall(
                    "verify",
                    "run_command",
                    {"argv": [sys.executable, "-c", "print('not executed')"], "purpose": "verify"},
                ),
            )
        ),
        ModelReply(content="I did not run the command."),
        ModelReply(content="I still cannot establish and complete a plan."),
    ]
    agent, model, _trace = _agent(tmp_path, replies)

    result = agent.run("Run a verification command")

    assert result.verification_status is VerificationStatus.NOT_REQUIRED
    assert result.termination_reason is TerminationReason.PLAN_FAILED
    tool_messages = [message for message in model.requests[1] if message.get("role") == "tool"]
    assert json.loads(str(tool_messages[0]["content"]))["error_code"] == "plan_required"


@pytest.mark.parametrize(
    "arguments",
    [
        {"steps": []},
        {"steps": [f"Step {index}" for index in range(9)]},
        {"steps": ["  "]},
        {"steps": [1]},
        {"steps": ["Write app.py"], "extra": True},
    ],
    ids=["empty", "too-many", "blank", "non-string", "unknown-key"],
)
def test_invalid_plan_blocks_following_mutation(
    tmp_path: Path,
    arguments: dict[str, JSONValue],
) -> None:
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    replies = [
        ModelReply(
            tool_calls=(
                ToolCall("bad-plan", "update_plan", arguments),
                ToolCall("write", "write_file", {"path": "app.py", "content": "value = 2\n"}),
            )
        ),
        ModelReply(content="Done."),
        ModelReply(content="I cannot provide a valid plan."),
    ]
    agent, model, _trace = _agent(tmp_path, replies)

    result = agent.run("Change the value to 2")

    assert result.termination_reason is TerminationReason.PLAN_FAILED
    assert target.read_text(encoding="utf-8") == "value = 1\n"
    tool_messages = [message for message in model.requests[1] if message.get("role") == "tool"]
    payloads = [json.loads(str(message["content"])) for message in tool_messages]
    assert [payload["error_code"] for payload in payloads] == ["invalid_plan", "plan_update_failed"]


def test_one_replan_can_recover_and_tool_results_are_linked_to_plan_steps(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    initial_steps = ["Update app.py", "Verify app.py"]
    revised_steps = ["Correct app.py using observed content", "Verify the correction"]
    replies = [
        ModelReply(
            tool_calls=(
                plan_call("plan-1", initial_steps),
                ToolCall("bad-edit", "replace_text", {"path": "app.py", "old": "missing", "new": "2"}),
            )
        ),
        ModelReply(
            tool_calls=(
                plan_call("plan-2", revised_steps),
                ToolCall("good-edit", "replace_text", {"path": "app.py", "old": "1", "new": "2"}),
            )
        ),
        ModelReply(
            tool_calls=(
                ToolCall(
                    "verify",
                    "run_command",
                    {"argv": [sys.executable, "-c", "raise SystemExit(0)"], "purpose": "verify"},
                ),
            )
        ),
        ModelReply(content="Updated and verified app.py."),
    ]
    agent, _model, trace = _agent(tmp_path, replies)

    result = agent.run("Change the value to 2")

    assert result.successful
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    events = read_trace(trace.path)
    plan_updates = [event for event in events if event["event_type"] == "plan_updated"]
    assert [event["payload"]["kind"] for event in plan_updates] == ["created", "replanned"]
    tool_results = {
        event["payload"]["tool_call_id"]: event["payload"]
        for event in events
        if event["event_type"] == "tool_result"
    }
    assert tool_results["bad-edit"]["plan_version"] == 1
    assert tool_results["bad-edit"]["plan_step"] == 1
    assert tool_results["good-edit"]["plan_version"] == 2
    assert tool_results["good-edit"]["plan_step"] == 1
    assert tool_results["verify"]["plan_step"] == 2


def test_rejected_action_does_not_destroy_pending_replan_state(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    replies = [
        ModelReply(
            tool_calls=(
                plan_call("plan-1", ["Attempt edit"]),
                ToolCall("bad-edit", "replace_text", {"path": "app.py", "old": "missing", "new": "2"}),
            )
        ),
        ModelReply(
            tool_calls=(
                ToolCall("out-of-order", "write_file", {"path": "app.py", "content": "value = 3\n"}),
            )
        ),
        ModelReply(
            tool_calls=(
                plan_call("plan-2", ["Correct app.py", "Verify app.py"]),
                ToolCall("good-edit", "replace_text", {"path": "app.py", "old": "1", "new": "2"}),
            )
        ),
        ModelReply(
            tool_calls=(
                ToolCall(
                    "verify",
                    "run_command",
                    {"argv": [sys.executable, "-c", "raise SystemExit(0)"], "purpose": "verify"},
                ),
            )
        ),
        ModelReply(content="Updated and verified app.py."),
    ]
    agent, model, trace = _agent(tmp_path, replies)

    result = agent.run("Change the value to 2")

    assert result.successful
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    out_of_order_results = [
        message
        for message in model.requests[2]
        if message.get("role") == "tool" and message.get("tool_call_id") == "out-of-order"
    ]
    assert json.loads(str(out_of_order_results[0]["content"]))["error_code"] == "replan_required"
    phase_changes = [event["payload"]["to"] for event in read_trace(trace.path) if event["event_type"] == "phase_changed"]
    assert phase_changes.count("replan") == 1


def test_batch_failure_aborts_later_actions_and_allows_one_replan(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    replies = [
        ModelReply(
            tool_calls=(
                plan_call("plan-1", ["Apply the edit batch"]),
                ToolCall("first-write", "write_file", {"path": "app.py", "content": "value = 2\n"}),
                ToolCall("failed-edit", "replace_text", {"path": "app.py", "old": "missing", "new": "3"}),
                ToolCall("must-abort", "write_file", {"path": "app.py", "content": "value = 99\n"}),
            )
        ),
        ModelReply(
            tool_calls=(
                plan_call("plan-2", ["Correct app.py from observed state", "Verify app.py"]),
                ToolCall("recover", "replace_text", {"path": "app.py", "old": "2", "new": "4"}),
            )
        ),
        ModelReply(
            tool_calls=(
                ToolCall(
                    "verify",
                    "run_command",
                    {"argv": [sys.executable, "-c", "raise SystemExit(0)"], "purpose": "verify"},
                ),
            )
        ),
        ModelReply(content="Recovered and verified app.py."),
    ]
    agent, model, trace = _agent(tmp_path, replies)

    result = agent.run("Change the value safely")

    assert result.successful
    assert target.read_text(encoding="utf-8") == "value = 4\n"
    tool_messages = [message for message in model.requests[1] if message.get("role") == "tool"]
    aborted = next(message for message in tool_messages if message.get("tool_call_id") == "must-abort")
    assert json.loads(str(aborted["content"]))["error_code"] == "action_batch_aborted"
    failed_steps = [event for event in read_trace(trace.path) if event["event_type"] == "plan_step_failed"]
    assert len(failed_steps) == 1


def test_second_failure_after_the_only_replan_is_terminal(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    replies = [
        ModelReply(
            tool_calls=(
                plan_call("plan-1", ["Attempt edit"]),
                ToolCall("bad-1", "replace_text", {"path": "app.py", "old": "missing-1", "new": "2"}),
            )
        ),
        ModelReply(
            tool_calls=(
                plan_call("plan-2", ["Retry corrected edit"]),
                ToolCall("bad-2", "replace_text", {"path": "app.py", "old": "missing-2", "new": "2"}),
            )
        ),
    ]
    agent, model, trace = _agent(tmp_path, replies)

    result = agent.run("Change the value to 2")

    assert result.termination_reason is TerminationReason.PLAN_FAILED
    assert result.steps == 2
    assert len(model.requests) == 2
    assert target.read_text(encoding="utf-8") == "value = 1\n"
    assert read_trace(trace.path)[-1]["payload"]["reason"] == "plan_failed"


def test_incomplete_plan_cannot_finish_as_completed(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    steps = ["Update app.py", "Verify app.py"]
    replies = [
        ModelReply(
            tool_calls=(
                plan_call("plan", steps),
                ToolCall("write", "write_file", {"path": "app.py", "content": "value = 2\n"}),
            )
        ),
        ModelReply(content="Done."),
        ModelReply(content="I cannot complete the remaining plan step."),
    ]
    agent, model, _trace = _agent(tmp_path, replies)

    result = agent.run("Change the value to 2")

    assert result.termination_reason is TerminationReason.PLAN_FAILED
    assert len(model.requests) == 3
    assert any("plan" in str(message.get("content", "")).lower() for message in model.requests[-1])


def test_plan_and_verification_completion_reminders_are_independent(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("old", encoding="utf-8")
    replies = [
        ModelReply(tool_calls=(plan_call("plan", ["Update note.txt"]),)),
        ModelReply(content="Done before executing the plan."),
        ModelReply(
            tool_calls=(
                ToolCall("write", "write_file", {"path": "note.txt", "content": "new"}),
            )
        ),
        ModelReply(content="Done without verification."),
        ModelReply(content="I cannot run verification."),
    ]
    agent, model, _trace = _agent(tmp_path, replies)

    result = agent.run("Update the note")

    assert result.termination_reason is TerminationReason.COMPLETED
    assert result.verification_status is VerificationStatus.REQUIRED
    assert target.read_text(encoding="utf-8") == "new"
    assert len(model.requests) == 5
    assert any("plan" in str(message.get("content", "")).lower() for message in model.requests[2])
    assert any("verification" in str(message.get("content", "")).lower() for message in model.requests[4])


def test_verification_command_requires_an_active_plan_step(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("old", encoding="utf-8")
    command_output = tmp_path / "verification-command-ran.txt"
    replies = [
        ModelReply(
            tool_calls=(
                plan_call("plan", ["Update note.txt"]),
                ToolCall("write", "write_file", {"path": "note.txt", "content": "new"}),
            )
        ),
        ModelReply(
            tool_calls=(
                ToolCall(
                    "verify-outside-plan",
                    "run_command",
                    {
                        "argv": [
                            sys.executable,
                            "-c",
                            "from pathlib import Path; Path('verification-command-ran.txt').write_text('ran')",
                        ],
                        "purpose": "verify",
                    },
                ),
            )
        ),
        ModelReply(content="Done."),
        ModelReply(content="I cannot run a planned verification."),
    ]
    agent, model, _trace = _agent(tmp_path, replies)

    result = agent.run("Update the note")

    assert result.termination_reason is TerminationReason.COMPLETED
    assert result.verification_status is VerificationStatus.REQUIRED
    assert not command_output.exists()
    tool_messages = [
        message
        for message in model.requests[2]
        if message.get("role") == "tool" and message.get("tool_call_id") == "verify-outside-plan"
    ]
    assert json.loads(str(tool_messages[0]["content"]))["error_code"] == "plan_step_required"


def test_read_only_tool_policy_hides_and_blocks_mutating_tools(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    replies = [
        ModelReply(
            tool_calls=(
                ToolCall(
                    "write",
                    "write_file",
                    {"path": "app.py", "content": "value = 2\n", "overwrite": True},
                ),
            )
        ),
        ModelReply(content="I kept the planning run read-only."),
    ]
    agent, model, _trace = _agent(tmp_path, replies)
    agent.set_tool_allowlist(("list_files", "search_text", "read_file"))

    result = agent.run("Plan the change without modifying files")

    visible_names = {tool["function"]["name"] for tool in model.tool_requests[0]}
    assert visible_names == {"list_files", "search_text", "read_file"}
    assert target.read_text(encoding="utf-8") == "value = 1\n"
    tool_messages = [message for message in model.requests[1] if message.get("role") == "tool"]
    assert "tool_not_allowed" in str(tool_messages[0]["content"])
    assert result.changed_files == ()


def test_read_only_planning_response_is_not_replaced_by_runtime_plan_guard(tmp_path: Path) -> None:
    plan_text = "## 落地方案\n\n1. 完善成绩录入。\n2. 添加自动化测试。\n\n请批准后开始编码。"
    replies = [
        ModelReply(
            tool_calls=(
                plan_call("unexpected-runtime-plan", ["等待用户批准", "实现功能", "运行测试"]),
            )
        ),
        ModelReply(content=plan_text),
    ]
    agent, model, trace = _agent(tmp_path, replies)
    agent.set_tool_allowlist(("list_files", "search_text", "read_file"))

    result = agent.run("Read the uploaded code and provide an implementation plan for approval")

    assert result.termination_reason is TerminationReason.COMPLETED
    assert result.final_text == plan_text
    assert len(model.requests) == 2
    tool_messages = [message for message in model.requests[1] if message.get("role") == "tool"]
    assert "tool_not_allowed" in str(tool_messages[0]["content"])
    assert not any(event["event_type"] == "completion_reminder" for event in read_trace(trace.path))


def test_proof_mode_records_diff_command_evidence_and_local_exports(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    steps = ["Update app.py", "Verify app.py"]
    model = FakeModelClient(
        [
            ModelReply(
                tool_calls=(
                    plan_call("plan-edit", steps),
                    ToolCall("edit", "replace_text", {"path": "app.py", "old": "1", "new": "2"}),
                )
            ),
            ModelReply(
                tool_calls=(
                    ToolCall(
                        "verify",
                        "run_command",
                        {"argv": [sys.executable, "-c", "print('verified')"], "purpose": "verify"},
                    ),
                )
            ),
            ModelReply(content="Done."),
        ]
    )
    trace = TraceRecorder(tmp_path, session_id="proof-run")
    transaction = WorkspaceTransaction(tmp_path, trace.session_id)
    agent = Agent(
        model,
        build_tool_registry(tmp_path, lambda _argv, _cwd: True, transaction=transaction),
        ContextManager(),
        trace,
        transaction=transaction,
    )

    result = agent.run("Change app.py")

    assert result.proof is not None
    assert result.proof["source"] == "tracecoder_runtime"
    assert result.proof["file_changes"][0]["path"] == "app.py"
    assert "+value = 2" in result.proof["file_changes"][0]["diff"]
    assert result.proof["commands"][0]["purpose"] == "verify"
    assert result.proof["commands"][0]["exit_code"] == 0
    assert result.proof["verification_status"] == "verify_command_passed"
    assert result.proof["verification_basis"] == "runtime_observed_model_selected_command"
    assert result.proof["transaction"]["rollback_available"] is True
    assert Path(result.proof_json_path).is_file()
    assert Path(result.proof_markdown_path).is_file()
    markdown = Path(result.proof_markdown_path).read_text(encoding="utf-8")
    assert "# TraceCoder Proof" in markdown
    assert "#### stdout" in markdown
    assert "verify_command_passed" in markdown
    assert "model-selected" in markdown


def test_agent_carries_structured_conversation_messages_into_the_next_turn(tmp_path: Path) -> None:
    first_agent, _first_model, _first_trace = _agent(tmp_path, [ModelReply(content="The chosen name is Alpha.")])
    first_agent.run("Remember that the chosen name is Alpha.")

    second_agent, second_model, _second_trace = _agent(tmp_path, [ModelReply(content="You chose Alpha.")])
    second_agent.run("What name did I choose?", history=first_agent.conversation_messages)

    request = second_model.requests[0]
    assert [message["role"] for message in request] == ["system", "user", "assistant", "user"]
    assert request[1]["content"] == "Remember that the chosen name is Alpha."
    assert request[2]["content"] == "The chosen name is Alpha."
    assert request[3]["content"] == "What name did I choose?"


def test_agent_carries_complete_tool_bundles_into_the_next_turn(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("remember me", encoding="utf-8")
    first_agent, _first_model, _first_trace = _agent(
        tmp_path,
        [
            ModelReply(tool_calls=(ToolCall("read-note", "read_file", {"path": "note.txt"}),)),
            ModelReply(content="I read note.txt."),
        ],
    )
    first_agent.run("Read note.txt.")

    second_agent, second_model, _second_trace = _agent(tmp_path, [ModelReply(content="It said remember me.")])
    second_agent.run("What did the file say?", history=first_agent.conversation_messages)

    request = second_model.requests[0]
    assert [message["role"] for message in request] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assert request[3]["tool_call_id"] == "read-note"
    assert "remember me" in str(request[3]["content"])


def test_agent_replays_reasoning_content_after_tool_call_without_tracing_it(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("remember me", encoding="utf-8")
    private_state = "opaque-reasoning-state-after-tool-call"
    agent, model, trace = _agent(
        tmp_path,
        [
            ModelReply(
                reasoning_content=private_state,
                tool_calls=(ToolCall("read-note", "read_file", {"path": "note.txt"}),),
            ),
            ModelReply(content="I read note.txt."),
        ],
    )

    agent.run("Read note.txt.")

    assistant = next(
        message
        for message in model.requests[1]
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    assert assistant["reasoning_content"] == private_state
    assert private_state not in trace.path.read_text(encoding="utf-8")


def test_agent_replays_reasoning_content_across_user_turns(tmp_path: Path) -> None:
    private_state = "opaque-reasoning-state-from-prior-turn"
    first_agent, _first_model, _first_trace = _agent(
        tmp_path,
        [ModelReply(content="The chosen name is Alpha.", reasoning_content=private_state)],
    )
    first_agent.run("Remember that the chosen name is Alpha.")

    second_agent, second_model, _second_trace = _agent(
        tmp_path,
        [ModelReply(content="You chose Alpha.")],
    )
    second_agent.run("What name did I choose?", history=first_agent.conversation_messages)

    prior_reply = second_model.requests[0][2]
    assert prior_reply["role"] == "assistant"
    assert prior_reply["reasoning_content"] == private_state


def test_agent_replays_complete_reasoning_history_after_cross_turn_compaction(
    tmp_path: Path,
) -> None:
    (tmp_path / "large-note.txt").write_text("remember me " * 250, encoding="utf-8")
    tool_reasoning = "opaque-tool-reasoning"
    final_reasoning = "opaque-final-reasoning"
    first_agent, _first_model, _first_trace = _agent(
        tmp_path,
        [
            ModelReply(
                reasoning_content=tool_reasoning,
                tool_calls=(ToolCall("read-note", "read_file", {"path": "large-note.txt"}),),
            ),
            ModelReply(content="I read the large note.", reasoning_content=final_reasoning),
        ],
    )
    first_agent.run("Read the large note.")

    second_agent, second_model, _second_trace = _agent(
        tmp_path,
        [ModelReply(content="It said remember me.")],
        context_max_chars=1_500,
    )
    second_agent.run("What did it say?", history=first_agent.conversation_messages)

    request = second_model.requests[0]
    assert [
        message["reasoning_content"]
        for message in request
        if message.get("reasoning_content") is not None
    ] == [tool_reasoning, final_reasoning]
    assert any("truncated" in str(message.get("content", "")).casefold() for message in request)


def test_unverified_mutation_gets_one_reminder_then_finishes(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("old", encoding="utf-8")
    replies = [
        ModelReply(
            tool_calls=(
                plan_call("plan", ["Update note.txt"]),
                ToolCall("write", "write_file", {"path": "note.txt", "content": "new"}),
            )
        ),
        ModelReply(content="Done."),
        ModelReply(content="I cannot run verification."),
    ]
    agent, model, _trace = _agent(tmp_path, replies)

    result = agent.run("Update the note")

    assert result.termination_reason is TerminationReason.COMPLETED
    assert result.verification_status is VerificationStatus.REQUIRED
    assert len(model.requests) == 3
    assert any("verification" in str(message.get("content", "")).lower() for message in model.requests[-1])


def test_created_directory_requires_verification(tmp_path: Path) -> None:
    replies = [
        ModelReply(
            tool_calls=(
                plan_call("plan", ["Create the project directory"]),
                ToolCall("mkdir", "create_directory", {"path": "course_project"}),
            )
        ),
        ModelReply(content="Done."),
        ModelReply(content="I cannot run verification."),
    ]
    agent, model, _trace = _agent(tmp_path, replies)

    result = agent.run("Create a course project directory")

    assert result.verification_status is VerificationStatus.REQUIRED
    assert (tmp_path / "course_project").is_dir()
    assert len(model.requests) == 3
    assert any("verification" in str(message.get("content", "")).lower() for message in model.requests[-1])


def test_repeated_identical_calls_stop_deterministically(tmp_path: Path) -> None:
    repeated = [
        ModelReply(tool_calls=(ToolCall(f"call-{index}", "list_files", {"path": "."}),))
        for index in range(3)
    ]
    agent, _model, _trace = _agent(tmp_path, repeated, repeat_limit=3)

    result = agent.run("Loop forever")

    assert result.termination_reason is TerminationReason.REPEATED_CALL
    assert result.steps == 3


def test_max_steps_stops_non_finishing_model(tmp_path: Path) -> None:
    replies = [ModelReply(tool_calls=(ToolCall("call", "list_files", {}),))]
    agent, _model, _trace = _agent(tmp_path, replies, max_steps=1)

    result = agent.run("Keep working")

    assert result.termination_reason is TerminationReason.MAX_STEPS


def test_multiple_tool_results_remain_paired_with_ids(tmp_path: Path) -> None:
    (tmp_path / "one.txt").write_text("one", encoding="utf-8")
    replies = [
        ModelReply(
            tool_calls=(
                ToolCall("one", "read_file", {"path": "one.txt"}),
                ToolCall("missing", "read_file", {"path": "missing.txt"}),
            )
        ),
        ModelReply(content="Inspected both files."),
    ]
    agent, model, _trace = _agent(tmp_path, replies)

    agent.run("Inspect files")

    tool_messages = [message for message in model.requests[-1] if message.get("role") == "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == ["one", "missing"]
    assert json.loads(str(tool_messages[1]["content"]))["error_code"] == "path_not_found"


def test_secret_is_redacted_before_tool_result_reaches_model(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("sentinel-secret", encoding="utf-8")
    replies = [
        ModelReply(tool_calls=(ToolCall("read", "read_file", {"path": "secret.txt"}),)),
        ModelReply(content="Read it."),
    ]
    agent, model, trace = _agent(tmp_path, replies)

    agent.run("Read the file")

    assert "sentinel-secret" not in json.dumps(model.requests[-1])
    assert "sentinel-secret" not in trace.path.read_text(encoding="utf-8")


def test_later_mutation_invalidates_prior_verification(tmp_path: Path) -> None:
    (tmp_path / "value.txt").write_text("one", encoding="utf-8")
    steps = ["Write the first value", "Verify the first value", "Write the final value"]
    replies = [
        ModelReply(
            tool_calls=(
                plan_call("plan-write-1", steps),
                ToolCall("write-1", "write_file", {"path": "value.txt", "content": "two"}),
            )
        ),
        ModelReply(
            tool_calls=(
                ToolCall(
                    "verify",
                    "run_command",
                    {"argv": [sys.executable, "-c", "raise SystemExit(0)"], "purpose": "verify"},
                ),
            )
        ),
        ModelReply(
            tool_calls=(
                ToolCall("write-2", "write_file", {"path": "value.txt", "content": "three"}),
            )
        ),
        ModelReply(content="Done."),
        ModelReply(content="No further verification available."),
    ]
    agent, _model, _trace = _agent(tmp_path, replies)

    result = agent.run("Change the value twice")

    assert result.verification_status is VerificationStatus.REQUIRED


class RaisingModelClient:
    """Raise one configured failure from the model boundary."""

    def __init__(self, failure: BaseException) -> None:
        self.failure = failure

    def complete(self, messages: list[dict[str, object]], tools: list[dict[str, object]]) -> ModelReply:
        raise self.failure


def test_provider_error_becomes_terminal_result(tmp_path: Path) -> None:
    trace = TraceRecorder(tmp_path, session_id="provider-error")
    agent = Agent(
        RaisingModelClient(RuntimeError("provider unavailable")),
        build_tool_registry(tmp_path, lambda _argv, _cwd: True),
        ContextManager(),
        trace,
    )

    result = agent.run("Inspect the project")

    assert result.termination_reason is TerminationReason.PROVIDER_ERROR
    assert read_trace(trace.path)[-1]["payload"]["reason"] == "provider_error"


def test_keyboard_interrupt_becomes_interrupted_result(tmp_path: Path) -> None:
    trace = TraceRecorder(tmp_path, session_id="interrupted")
    agent = Agent(
        RaisingModelClient(KeyboardInterrupt()),
        build_tool_registry(tmp_path, lambda _argv, _cwd: True),
        ContextManager(),
        trace,
    )

    result = agent.run("Inspect the project")

    assert result.termination_reason is TerminationReason.INTERRUPTED


def test_cancel_request_stops_before_calling_the_model(tmp_path: Path) -> None:
    model = FakeModelClient([ModelReply(content="should not be returned")])
    trace = TraceRecorder(tmp_path, session_id="cancelled")
    agent = Agent(
        model,
        build_tool_registry(tmp_path, lambda _argv, _cwd: True),
        ContextManager(),
        trace,
        cancelled=lambda: True,
    )

    result = agent.run("Inspect the project")

    assert result.termination_reason is TerminationReason.INTERRUPTED
    assert model.requests == []
    assert read_trace(trace.path)[-1]["payload"]["reason"] == "interrupted"


def test_cancel_request_after_model_reply_skips_requested_tool(tmp_path: Path) -> None:
    model = FakeModelClient(
        [ModelReply(tool_calls=(ToolCall("should-not-run", "list_files", {"path": "."}),))]
    )
    trace = TraceRecorder(tmp_path, session_id="cancel-after-model")
    agent = Agent(
        model,
        build_tool_registry(tmp_path, lambda _argv, _cwd: True),
        ContextManager(),
        trace,
        cancelled=lambda: bool(model.requests),
    )

    result = agent.run("Inspect the project")

    assert result.termination_reason is TerminationReason.INTERRUPTED
    assert result.steps == 1
    assert len(model.requests) == 1
    events = read_trace(trace.path)
    assert [event["event_type"] for event in events] == ["run_started", "model_reply", "run_finished"]
    assert events[-1]["payload"]["reason"] == "interrupted"
    assert [message["role"] for message in agent.conversation_messages] == ["user", "assistant"]
    assert all(not message.get("tool_calls") for message in agent.conversation_messages)


def test_cancel_request_between_tool_calls_skips_later_tool_and_model(tmp_path: Path) -> None:
    cancelled = False

    def observe(event: dict[str, object]) -> None:
        nonlocal cancelled
        if event["event_type"] == "tool_result":
            cancelled = True

    model = FakeModelClient(
        [
            ModelReply(
                tool_calls=(
                    ToolCall("first", "list_files", {"path": "."}),
                    ToolCall("second", "list_files", {"path": "."}),
                )
            ),
            ModelReply(content="This reply must not be requested."),
        ]
    )
    trace = TraceRecorder(tmp_path, session_id="cancel-between-tools", observer=observe)
    agent = Agent(
        model,
        build_tool_registry(tmp_path, lambda _argv, _cwd: True),
        ContextManager(),
        trace,
        cancelled=lambda: cancelled,
    )

    result = agent.run("Inspect the project")

    assert result.termination_reason is TerminationReason.INTERRUPTED
    assert result.steps == 1
    assert len(model.requests) == 1
    events = read_trace(trace.path)
    assert [event["event_type"] for event in events] == [
        "run_started",
        "model_reply",
        "tool_requested",
        "tool_result",
        "run_finished",
    ]
    assert events[2]["payload"]["tool_call_id"] == "first"
    assert events[-1]["payload"]["reason"] == "interrupted"
    assert [message["role"] for message in agent.conversation_messages] == ["user", "assistant"]
    assert all(not message.get("tool_calls") for message in agent.conversation_messages)
