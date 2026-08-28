const state = {
  runId: null,
  lastEventId: 0,
  polling: null,
  approvalId: null,
  submittingApprovalId: null,
};

const elements = {
  form: document.querySelector("#task-form"),
  input: document.querySelector("#task-input"),
  run: document.querySelector("#run-button"),
  cancel: document.querySelector("#cancel-button"),
  timeline: document.querySelector("#timeline"),
  empty: document.querySelector("#empty-state"),
  approval: document.querySelector("#approval-card"),
  approvalCwd: document.querySelector("#approval-cwd"),
  approvalCommand: document.querySelector("#approval-command"),
  approve: document.querySelector("#approve-button"),
  deny: document.querySelector("#deny-button"),
  evidence: document.querySelector("#evidence"),
  evidenceDetails: document.querySelector("#evidence-details"),
  runtimeLabel: document.querySelector("#runtime-label"),
};

async function request(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `请求失败 (${response.status})`);
  return data;
}

function setText(selector, value) {
  document.querySelector(selector).textContent = value;
}

async function loadConfig() {
  try {
    const config = await request("/api/config");
    setText("#workspace", config.workspace);
    setText("#model", config.model);
    setText("#provider", config.provider);
    if (!state.runId) elements.runtimeLabel.textContent = "本地运行时已连接";
  } catch (error) {
    if (!state.runId) elements.runtimeLabel.textContent = error.message;
  }
}

function formatPayload(event) {
  const payload = event.payload || {};
  if (event.event_type === "model_reply") {
    return payload.content || (payload.tool_calls?.length ? "模型请求调用工具" : "模型未返回文本");
  }
  if (event.event_type === "tool_requested") {
    return `${payload.tool || "tool"}\n${JSON.stringify(payload.arguments || {}, null, 2)}`;
  }
  if (event.event_type === "tool_result") {
    return `${payload.tool || "tool"}\n${JSON.stringify(payload.result || {}, null, 2)}`;
  }
  if (event.event_type === "approval_required") return "等待你确认命令是否可以执行。";
  if (event.event_type === "approval_resolved") return payload.approved ? "命令已批准。" : "命令已拒绝。";
  if (event.event_type === "run_finished") return JSON.stringify(payload, null, 2);
  return Object.keys(payload).length ? JSON.stringify(payload, null, 2) : event.event_type;
}

function eventLabel(type) {
  return ({
    run_started: "开始",
    model_reply: "模型回复",
    tool_requested: "工具请求",
    tool_result: "工具结果",
    verification_reminder: "验证提醒",
    approval_required: "命令审批",
    approval_resolved: "审批结果",
    run_finished: "运行结束",
    cancel_requested: "停止请求",
  })[type] || type.replaceAll("_", " ");
}

function appendEvent(event) {
  elements.empty.hidden = true;
  const node = document.querySelector("#event-template").content.firstElementChild.cloneNode(true);
  node.classList.add(event.event_type);
  node.querySelector(".event-kind").textContent = eventLabel(event.event_type);
  node.querySelector(".event-index").textContent = `#${event.web_event_id}`;
  const content = node.querySelector(".event-content");
  content.textContent = formatPayload(event);
  if (event.event_type === "tool_result" && event.payload?.result?.ok === false) node.classList.add("error");
  elements.timeline.appendChild(node);
  elements.timeline.scrollTop = elements.timeline.scrollHeight;
}

function updateApproval(approval) {
  state.approvalId = approval?.id || null;
  elements.approval.hidden = !approval;
  if (approval) {
    elements.approvalCwd.textContent = `cwd: ${approval.cwd}`;
    elements.approvalCommand.textContent = JSON.stringify(approval.argv);
  }
  const submitting = state.approvalId && state.submittingApprovalId === state.approvalId;
  elements.approve.disabled = !state.approvalId || submitting;
  elements.deny.disabled = !state.approvalId || submitting;
}

function renderEvidence(result, error) {
  if (!result && !error) return;
  elements.evidence.hidden = false;
  elements.evidenceDetails.replaceChildren();
  const rows = result ? [
    ["终止原因", result.termination_reason],
    ["验证状态", result.verification_status],
    ["运行步数", String(result.steps)],
    ["改动文件", result.changed_files.length ? result.changed_files.join(", ") : "无"],
    ["Trace", result.trace_path],
  ] : [["运行错误", error]];
  for (const [label, value] of rows) {
    const row = document.createElement("div");
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = label;
    dd.textContent = value;
    row.append(dt, dd);
    elements.evidenceDetails.appendChild(row);
  }
}

function setRunning(running) {
  elements.run.disabled = running;
  elements.input.disabled = running;
  elements.cancel.disabled = !running;
  elements.run.textContent = running ? "运行中…" : "运行任务 ↗";
}

async function poll() {
  if (!state.runId) return true;
  try {
    const data = await request(`/api/runs/${state.runId}?after=${state.lastEventId}`);
    for (const event of data.events) appendEvent(event);
    state.lastEventId = data.next_event_id;
    updateApproval(data.approval);
    const terminal = ["finished", "interrupted", "failed"].includes(data.status);
    elements.runtimeLabel.textContent = terminal ? `运行已${data.status === "finished" ? "结束" : "停止"}` : eventLabel(data.status);
    if (terminal) {
      state.polling = null;
      setRunning(false);
      updateApproval(null);
      renderEvidence(data.result, data.error);
    }
    return terminal;
  } catch (error) {
    elements.runtimeLabel.textContent = error.message;
    return false;
  }
}

async function pollingLoop() {
  const terminal = await poll();
  if (!terminal && state.runId) state.polling = setTimeout(pollingLoop, 650);
}

async function restoreActiveRun() {
  try {
    const snapshot = await request("/api/runs/active");
    if (!snapshot || state.runId) return;

    state.runId = snapshot.id;
    state.lastEventId = 0;
    elements.evidence.hidden = true;
    elements.timeline.querySelectorAll(".event-card").forEach((node) => node.remove());
    elements.empty.hidden = !snapshot.events.length;
    for (const event of snapshot.events) appendEvent(event);
    state.lastEventId = snapshot.next_event_id;
    updateApproval(snapshot.approval);

    const terminal = ["finished", "interrupted", "failed"].includes(snapshot.status);
    elements.runtimeLabel.textContent = eventLabel(snapshot.status);
    setRunning(!terminal);
    if (terminal) {
      updateApproval(null);
      renderEvidence(snapshot.result, snapshot.error);
      return;
    }
    await pollingLoop();
  } catch (error) {
    if (!state.runId) elements.runtimeLabel.textContent = error.message;
  }
}

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const task = elements.input.value.trim();
  if (!task) return;
  try {
    setRunning(true);
    elements.evidence.hidden = true;
    const started = await request("/api/runs", { method: "POST", body: JSON.stringify({ task }) });
    state.runId = started.id;
    state.lastEventId = 0;
    elements.timeline.querySelectorAll(".event-card").forEach((node) => node.remove());
    await pollingLoop();
  } catch (error) {
    setRunning(false);
    elements.runtimeLabel.textContent = error.message;
  }
});

elements.cancel.addEventListener("click", async () => {
  if (!state.runId) return;
  elements.cancel.disabled = true;
  try {
    await request(`/api/runs/${state.runId}/cancel`, { method: "POST" });
  } catch (error) {
    elements.cancel.disabled = false;
    elements.runtimeLabel.textContent = error.message;
  }
});

async function decide(approved) {
  const runId = state.runId;
  const approvalId = state.approvalId;
  if (!runId || !approvalId || state.submittingApprovalId) return;

  state.submittingApprovalId = approvalId;
  elements.approve.disabled = true;
  elements.deny.disabled = true;
  try {
    await request(`/api/runs/${runId}/approval`, {
      method: "POST",
      body: JSON.stringify({ approval_id: approvalId, approved }),
    });
    if (state.approvalId === approvalId) updateApproval(null);
  } catch (error) {
    elements.runtimeLabel.textContent = `Approval submission failed: ${error.message}. Waiting for run state to sync.`;
  } finally {
    if (state.submittingApprovalId === approvalId) state.submittingApprovalId = null;
    if (state.approvalId === approvalId) {
      elements.approve.disabled = false;
      elements.deny.disabled = false;
    }
  }
}

elements.approve.addEventListener("click", () => { void decide(true); });
elements.deny.addEventListener("click", () => { void decide(false); });

async function initialize() {
  const config = loadConfig();
  await restoreActiveRun();
  await config;
}

void initialize();
