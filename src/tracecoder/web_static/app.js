const TERMINAL_STATUSES = new Set(["finished", "interrupted", "failed"]);
const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
const MAX_ATTACHMENTS = 20;
const PROJECT_PLANNING_TASK = `制定项目落地方案。

请根据项目目标、技术偏好和已上传的代码，先给出一份简洁、可执行的落地方案。你可以使用只读文件工具了解现有代码，但暂时不要修改文件或执行命令。

方案请包含：目标理解、拟实现功能、文件或模块规划、测试与验收方式、需要用户确认的关键假设。最后请用户批准该方案；得到用户批准前不要开始编码。`;

const state = {
  conversationId: null,
  runId: null,
  activeRunId: null,
  lastEventId: 0,
  pollingTimer: null,
  pollingGeneration: 0,
  navigationGeneration: 0,
  approvalId: null,
  submittingApprovalId: null,
  processNodes: new Map(),
  deferredProcessEvents: new Map(),
  finalRenderedRunIds: new Set(),
  proofNodes: new Map(),
  deferScroll: false,
  running: false,
  uploading: false,
  attachments: [],
  conversations: [],
  projects: [],
  projectId: null,
  activeProject: null,
  projectAttachments: [],
  projectPlanningRequired: false,
  projectPlanningRunId: null,
};

const elements = {
  form: document.querySelector("#task-form"),
  input: document.querySelector("#task-input"),
  run: document.querySelector("#run-button"),
  cancel: document.querySelector("#cancel-button"),
  timeline: document.querySelector("#timeline"),
  empty: document.querySelector("#empty-state"),
  emptyTitle: document.querySelector("#empty-title"),
  emptyDescription: document.querySelector("#empty-description"),
  approval: document.querySelector("#approval-card"),
  approvalDescription: document.querySelector("#approval-description"),
  approvalCwd: document.querySelector("#approval-cwd"),
  approvalCommand: document.querySelector("#approval-command"),
  approvalExact: document.querySelector(".exact-command"),
  approve: document.querySelector("#approve-button"),
  deny: document.querySelector("#deny-button"),
  runtimeLabel: document.querySelector("#runtime-label"),
  model: document.querySelector("#model"),
  title: document.querySelector("#conversation-title"),
  history: document.querySelector("#history-list"),
  historyEmpty: document.querySelector("#history-empty"),
  newChat: document.querySelector("#new-chat-button"),
  projectsButton: document.querySelector("#projects-button"),
  conversationView: document.querySelector("#conversation-view"),
  projectsView: document.querySelector("#projects-view"),
  projectSearch: document.querySelector("#project-search"),
  projectList: document.querySelector("#project-list"),
  projectEmpty: document.querySelector("#project-empty"),
  newProject: document.querySelector("#new-project-button"),
  projectDetail: document.querySelector("#project-detail"),
  projectsPage: document.querySelector("#projects-page"),
  projectsBack: document.querySelector("#projects-back-button"),
  projectDetailName: document.querySelector("#project-detail-name"),
  projectDetailGoal: document.querySelector("#project-detail-goal"),
  projectDetailMeta: document.querySelector("#project-detail-meta"),
  projectConversationList: document.querySelector("#project-conversation-list"),
  projectNewChat: document.querySelector("#project-new-chat-button"),
  projectDialog: document.querySelector("#project-dialog"),
  projectForm: document.querySelector("#project-form"),
  projectName: document.querySelector("#project-name"),
  projectGoal: document.querySelector("#project-goal"),
  projectPreferences: document.querySelector("#project-preferences"),
  projectTargetPath: document.querySelector("#project-target-path"),
  projectFileInput: document.querySelector("#project-file-input"),
  projectAttach: document.querySelector("#project-attach-button"),
  projectAttachmentList: document.querySelector("#project-attachment-list"),
  projectUploadStatus: document.querySelector("#project-upload-status"),
  projectCancel: document.querySelector("#project-cancel-button"),
  projectCancelAction: document.querySelector("#project-cancel-action"),
  attach: document.querySelector("#attach-button"),
  fileInput: document.querySelector("#file-input"),
  attachmentList: document.querySelector("#attachment-list"),
  uploadStatus: document.querySelector("#upload-status"),
};

async function request(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (typeof options.body === "string" && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(url, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `请求失败 (${response.status})`);
  return data;
}

function eventLabel(type) {
  return ({
    run_started: "开始运行",
    model_reply: "模型中间输出",
    tool_requested: "请求调用工具",
    tool_result: "工具执行结果",
    verification_reminder: "验证提醒",
    approval_required: "等待命令审批",
    approval_resolved: "命令审批结果",
    run_finished: "运行结束",
    transaction_accepted: "已接受修改",
    transaction_rolled_back: "已回滚修改",
    cancel_requested: "停止请求",
    running: "正在运行",
    waiting_approval: "等待审批",
    interrupted: "已停止",
    failed: "运行失败",
    finished: "运行结束",
  })[type] || type.replaceAll("_", " ");
}

function trackProjectPlanning(snapshot) {
  if (!snapshot || snapshot.tool_policy !== "read_only" || !snapshot.project_id) return;
  state.projectPlanningRunId = snapshot.id;
  state.projectPlanningRequired = true;
  if (!TERMINAL_STATUSES.has(snapshot.status)) return;
  const completed = snapshot.status === "finished"
    && snapshot.result?.termination_reason === "completed";
  state.projectPlanningRequired = !completed;
  state.projectPlanningRunId = null;
  if (!completed) {
    elements.runtimeLabel.textContent = "落地方案未成功完成；再次发送将先重试只读规划";
  }
}

function formatPayload(event) {
  const payload = event.payload || {};
  if (event.event_type === "model_reply") {
    const parts = [];
    if (payload.content) parts.push(payload.content);
    if (payload.tool_calls?.length) parts.push(`工具请求：\n${JSON.stringify(payload.tool_calls, null, 2)}`);
    return parts.join("\n\n") || "模型没有返回可展示的文本";
  }
  if (event.event_type === "tool_requested") {
    return `${payload.tool || "tool"}\n${JSON.stringify(payload.arguments || {}, null, 2)}`;
  }
  if (event.event_type === "tool_result") {
    return `${payload.tool || "tool"}\n${JSON.stringify(payload.result || {}, null, 2)}`;
  }
  if (event.event_type === "approval_required") return "等待你确认是否允许执行这条命令。";
  if (event.event_type === "approval_resolved") return payload.approved ? "命令已批准。" : "命令已拒绝。";
  if (event.event_type === "run_finished") return JSON.stringify(payload, null, 2);
  return Object.keys(payload).length ? JSON.stringify(payload, null, 2) : event.event_type;
}

function scrollToBottom() {
  if (state.deferScroll) return;
  elements.timeline.scrollTop = elements.timeline.scrollHeight;
}

function showConversationView() {
  elements.conversationView.hidden = false;
  elements.projectsView.hidden = true;
}

function showProjectsPage() {
  elements.conversationView.hidden = true;
  elements.projectsView.hidden = false;
  elements.projectsPage.hidden = false;
  elements.projectDetail.hidden = true;
}

function updateEmptyStateCopy() {
  if (state.projectId && state.activeProject) {
    elements.emptyTitle.textContent = `继续项目：${state.activeProject.name}`;
    elements.emptyDescription.textContent = "这里的新对话会共享该项目已有对话上下文和上传文件。";
  } else {
    elements.emptyTitle.textContent = "今天想完成什么编程任务？";
    elements.emptyDescription.textContent = "TraceCoder 会在当前工作区内读取和修改文件；需要执行命令时会先请求你的批准。";
  }
}

function resetTimeline() {
  elements.timeline.replaceChildren(elements.empty);
  elements.empty.hidden = false;
  elements.title.textContent = state.activeProject?.name || "新对话";
  updateEmptyStateCopy();
  state.lastEventId = 0;
  state.processNodes = new Map();
  state.deferredProcessEvents = new Map();
  state.finalRenderedRunIds = new Set();
  state.proofNodes = new Map();
  updateApproval(null);
}

function appendMessage(role, text, attachments = []) {
  elements.empty.hidden = true;
  const node = document.querySelector("#message-template").content.firstElementChild.cloneNode(true);
  node.classList.add(role);
  node.querySelector(".message-role").textContent = role === "user" ? "你" : "TraceCoder";
  const body = node.querySelector(".message-body");
  const attachmentText = attachments.length ? `\n\n附件：${attachments.join("、")}` : "";
  if (role === "assistant") {
    body.classList.add("markdown-body");
    body.innerHTML = TraceCoderMarkdown.render(text);
  } else {
    body.textContent = `${text}${attachmentText}`;
  }
  elements.timeline.appendChild(node);
  scrollToBottom();
  return node;
}

function ensureProcessNode(runId) {
  const existing = state.processNodes.get(runId);
  if (existing) return existing;
  elements.empty.hidden = true;
  const node = document.querySelector("#process-template").content.firstElementChild.cloneNode(true);
  state.processNodes.set(runId, node);
  elements.timeline.appendChild(node);
  return node;
}

function appendProcessEvent(runId, event) {
  const processNode = ensureProcessNode(runId);
  const node = document.querySelector("#process-event-template").content.firstElementChild.cloneNode(true);
  node.querySelector(".process-kind").textContent = eventLabel(event.event_type);
  node.querySelector(".process-index").textContent = `#${event.web_event_id}`;
  node.querySelector(".process-content").textContent = formatPayload(event);
  if (event.event_type === "tool_result" && event.payload?.result?.ok === false) node.classList.add("error");
  const events = processNode.querySelector(".process-events");
  events.appendChild(node);
  processNode.querySelector(".process-count").textContent = `${events.childElementCount} 条`;
  scrollToBottom();
}

function isFinalModelReply(event, snapshot) {
  return Boolean(
    snapshot.result?.final_text
    && event.event_type === "model_reply"
    && !event.payload?.tool_calls?.length
    && event.payload?.content === snapshot.result.final_text,
  );
}

function isPotentialFinalModelReply(event) {
  return event.event_type === "model_reply" && !event.payload?.tool_calls?.length;
}

function flushDeferredProcessEvents(runId, snapshot, terminal) {
  const deferred = state.deferredProcessEvents.get(runId) || [];
  for (const event of deferred) {
    if (!terminal || !isFinalModelReply(event, snapshot)) appendProcessEvent(runId, event);
  }
  state.deferredProcessEvents.delete(runId);
}

function renderProcessEvents(runId, events, snapshot) {
  // RunResult can lag its final model event by one poll. Keep that event pending
  // until a later event proves it is intermediate or terminal state confirms it.
  const terminal = TERMINAL_STATUSES.has(snapshot.status);
  for (const event of events) {
    if (isPotentialFinalModelReply(event)) {
      flushDeferredProcessEvents(runId, snapshot, false);
      state.deferredProcessEvents.set(runId, [event]);
    } else if (event.event_type === "run_finished" && state.deferredProcessEvents.has(runId)) {
      state.deferredProcessEvents.get(runId).push(event);
    } else {
      flushDeferredProcessEvents(runId, snapshot, false);
      appendProcessEvent(runId, event);
    }
  }
  if (terminal) flushDeferredProcessEvents(runId, snapshot, true);
}

function renderFinalAnswer(runId, result, error) {
  if (state.finalRenderedRunIds.has(runId)) return;
  state.finalRenderedRunIds.add(runId);
  if (error) {
    const node = appendMessage("assistant", `本地运行失败：${error}`);
    node.classList.add("error");
    return;
  }
  const finalText = result?.final_text || "任务已经结束，但模型没有返回最终文本。";
  appendMessage("assistant", finalText);
}

function parseUnifiedDiff(diff) {
  const rows = [];
  let oldLine = null;
  let newLine = null;
  let inHunk = false;
  let removed = [];
  let added = [];

  const flushChanges = () => {
    const count = Math.max(removed.length, added.length);
    for (let index = 0; index < count; index += 1) {
      rows.push({ kind: "change", old: removed[index] || null, new: added[index] || null });
    }
    removed = [];
    added = [];
  };

  const lines = String(diff || "").split("\n");
  if (lines.at(-1) === "") lines.pop();
  for (const rawLine of lines) {
    const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
    if (line.startsWith("@@")) {
      flushChanges();
      inHunk = true;
      const match = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (match) {
        oldLine = Number(match[1]);
        newLine = Number(match[2]);
      }
      rows.push({ kind: "hunk", text: line });
    } else if ((!inHunk && (line.startsWith("---") || line.startsWith("+++"))) || line.startsWith("\\ No newline")) {
      continue;
    } else if (line.startsWith("-")) {
      removed.push({ number: oldLine, text: line.slice(1) });
      if (oldLine !== null) oldLine += 1;
    } else if (line.startsWith("+")) {
      added.push({ number: newLine, text: line.slice(1) });
      if (newLine !== null) newLine += 1;
    } else {
      flushChanges();
      const text = line.startsWith(" ") ? line.slice(1) : line;
      rows.push({
        kind: "context",
        old: { number: oldLine, text },
        new: { number: newLine, text },
      });
      if (oldLine !== null) oldLine += 1;
      if (newLine !== null) newLine += 1;
    }
  }
  flushChanges();
  return rows;
}

function diffCell(tag, className, text) {
  const cell = document.createElement(tag);
  cell.className = className;
  cell.textContent = text;
  return cell;
}

function appendSideBySideDiff(container, change) {
  const details = document.createElement("details");
  details.className = "proof-change";
  const summary = document.createElement("summary");
  summary.textContent = `${change.path || "未知文件"} · ${change.kind || "changed"}`;
  details.appendChild(summary);

  if (typeof change.diff !== "string") {
    const fallback = document.createElement("pre");
    fallback.textContent = `Diff 不可用：${change.diff_unavailable_reason || "unknown"}`;
    details.appendChild(fallback);
    container.appendChild(details);
    return;
  }

  let rendered = false;
  details.addEventListener("toggle", () => {
    if (!details.open || rendered) return;
    rendered = true;
    const scroll = document.createElement("div");
    scroll.className = "diff-scroll";
    const table = document.createElement("table");
    table.className = "diff-table";
    for (const row of parseUnifiedDiff(change.diff)) {
      const tr = document.createElement("tr");
      if (row.kind === "hunk") {
        tr.className = "diff-row-hunk";
        tr.append(
          diffCell("td", "diff-line-number diff-hunk", ""),
          diffCell("td", "diff-code diff-hunk", row.text),
          diffCell("td", "diff-line-number diff-hunk", ""),
          diffCell("td", "diff-code diff-hunk", row.text),
        );
      } else {
        const oldPart = row.old;
        const newPart = row.new;
        const removedClass = row.kind === "change" && oldPart ? " removed" : "";
        const addedClass = row.kind === "change" && newPart ? " added" : "";
        tr.className = [
          "diff-row",
          removedClass ? "diff-row-removed" : "",
          addedClass ? "diff-row-added" : "",
        ].filter(Boolean).join(" ");
        tr.append(
          diffCell("td", `diff-line-number diff-old${removedClass}`, oldPart?.number ?? ""),
          diffCell("td", `diff-code diff-old${removedClass || (oldPart ? "" : " empty")}`, oldPart ? `${removedClass ? "-" : " "}${oldPart.text}` : ""),
          diffCell("td", `diff-line-number diff-new${addedClass}`, newPart?.number ?? ""),
          diffCell("td", `diff-code diff-new${addedClass || (newPart ? "" : " empty")}`, newPart ? `${addedClass ? "+" : " "}${newPart.text}` : ""),
        );
      }
      table.appendChild(tr);
    }
    scroll.appendChild(table);
    details.appendChild(scroll);
  });
  container.appendChild(details);
}

function fillProofNode(runId, node, result) {
  const proof = result?.proof || {};
  const verification = proof.verification_status || result?.verification_status || "unknown";
  const changes = Array.isArray(proof.file_changes) ? proof.file_changes : [];
  const transaction = proof.transaction || {};
  const transactionState = result?.transaction_state || transaction.state || "not_required";
  const rollbackAvailable = Boolean(result?.rollback_available ?? transaction.rollback_available);

  node.querySelector(".proof-status").textContent = verification === "verify_command_passed"
    ? "验证命令通过（由模型选择）"
    : `验证：${verification}`;
  node.querySelector(".proof-summary").textContent = `${changes.length} 个文件证据 · ${proof.steps || result?.steps || 0} 个模型步骤`;

  const files = node.querySelector(".proof-files");
  files.replaceChildren();
  if (changes.length) {
    for (const change of changes) {
      appendSideBySideDiff(files, change);
    }
  } else {
    files.textContent = "没有记录到文件工具产生的净变化。";
  }

  const warning = node.querySelector(".proof-warning");
  warning.hidden = proof.shell_side_effects_unknown !== true;
  warning.textContent = "本次运行执行过命令；回滚只覆盖 TraceCoder 文件工具的修改，不能撤销命令产生的任意副作用。";
  const message = node.querySelector(".transaction-message");
  message.textContent = ({
    pending: "修改尚未确认：你可以接受或回滚。开始下一轮对话会自动接受本轮修改。",
    accepted: "修改已接受，事务快照已关闭。",
    rolled_back: "文件工具产生的修改已回滚。",
    not_required: "本轮没有需要回滚的文件工具修改。",
  })[transactionState] || `事务状态：${transactionState}`;

  const accept = node.querySelector(".proof-accept");
  const rollback = node.querySelector(".proof-rollback");
  accept.hidden = !rollbackAvailable;
  rollback.hidden = !rollbackAvailable;
  accept.disabled = false;
  rollback.disabled = false;
  const download = node.querySelector(".proof-download");
  download.href = `/api/runs/${runId}/proof.md`;
  download.setAttribute("download", `tracecoder-proof-${runId}.md`);
}

async function resolveTransaction(runId, action, node) {
  const accept = node.querySelector(".proof-accept");
  const rollback = node.querySelector(".proof-rollback");
  accept.disabled = true;
  rollback.disabled = true;
  try {
    const snapshot = await request(`/api/runs/${runId}/transaction`, {
      method: "POST",
      body: JSON.stringify({ action }),
    });
    fillProofNode(runId, node, snapshot.result);
    elements.runtimeLabel.textContent = action === "rollback" ? "修改已回滚" : "修改已接受";
  } catch (error) {
    accept.disabled = false;
    rollback.disabled = false;
    elements.runtimeLabel.textContent = `事务操作失败：${error.message}`;
  }
}

function renderProof(runId, result) {
  if (!result?.proof) return;
  let node = state.proofNodes.get(runId);
  if (!node) {
    node = document.querySelector("#proof-template").content.firstElementChild.cloneNode(true);
    node.querySelector(".proof-accept").addEventListener("click", () => { void resolveTransaction(runId, "accept", node); });
    node.querySelector(".proof-rollback").addEventListener("click", () => { void resolveTransaction(runId, "rollback", node); });
    state.proofNodes.set(runId, node);
    elements.timeline.appendChild(node);
  }
  fillProofNode(runId, node, result);
  scrollToBottom();
}

function appendTurn(snapshot) {
  appendMessage("user", snapshot.task, snapshot.attachments || []);
  renderProcessEvents(snapshot.id, snapshot.events || [], snapshot);
  if (TERMINAL_STATUSES.has(snapshot.status)) {
    renderFinalAnswer(snapshot.id, snapshot.result, snapshot.error);
    renderProof(snapshot.id, snapshot.result);
  }
}

function renderConversation(conversation) {
  showConversationView();
  state.projectId = conversation.project_id || null;
  state.activeProject = state.projectId
    ? state.projects.find((project) => project.id === state.projectId) || {
      id: state.projectId,
      name: conversation.project_name || "项目",
    }
    : null;
  resetTimeline();
  state.conversationId = conversation.id;
  elements.title.textContent = conversation.title;
  state.deferScroll = true;
  try {
    for (const turn of conversation.turns || []) appendTurn(turn);
  } finally {
    state.deferScroll = false;
  }
  scrollToBottom();
  const latest = conversation.turns?.at(-1) || null;
  state.runId = latest?.id || null;
  state.lastEventId = latest?.next_event_id || 0;
  updateApproval(latest?.approval || null);
  renderHistory();
}

function renderSingleRun(snapshot) {
  renderConversation({
    id: snapshot.conversation_id,
    title: snapshot.conversation_title,
    project_id: snapshot.project_id,
    project_name: snapshot.project_name,
    turns: [snapshot],
  });
}

function updateApproval(approval) {
  const previousApprovalId = state.approvalId;
  state.approvalId = approval?.id || null;
  elements.approval.hidden = !approval;
  if (approval) {
    elements.approvalDescription.textContent = approval.description || "申请执行一条本地命令。";
    elements.approvalCwd.textContent = `工作目录：${approval.cwd}`;
    elements.approvalCommand.textContent = JSON.stringify(approval.argv);
    if (previousApprovalId !== approval.id) elements.approvalExact.open = false;
  }
  const submitting = state.approvalId && state.submittingApprovalId === state.approvalId;
  elements.approve.disabled = !state.approvalId || submitting;
  elements.deny.disabled = !state.approvalId || submitting;
}

function updateControls() {
  const blocked = state.running || state.uploading;
  elements.run.disabled = blocked;
  elements.input.disabled = state.running;
  elements.attach.disabled = blocked;
  elements.fileInput.disabled = blocked;
  elements.projectsButton.disabled = state.running;
  elements.newProject.disabled = state.running || state.uploading;
  elements.projectAttach.disabled = blocked;
  elements.projectFileInput.disabled = blocked;
  elements.cancel.disabled = !state.running;
  elements.run.textContent = state.running ? "…" : "↑";
}

function setRunning(running) {
  state.running = running;
  updateControls();
}

function conversationSummaryFromRun(snapshot) {
  return {
    id: snapshot.conversation_id,
    title: snapshot.conversation_title,
    status: snapshot.status,
    turn_count: snapshot.turn_index,
    project_id: snapshot.project_id || null,
    project_name: snapshot.project_name || null,
  };
}

function upsertConversation(snapshot) {
  const summary = conversationSummaryFromRun(snapshot);
  const current = state.conversations[0];
  if (
    current?.id === summary.id
    && current.title === summary.title
    && current.status === summary.status
    && current.turn_count === summary.turn_count
    && current.project_id === summary.project_id
    && state.conversations.length <= 50
  ) return;
  state.conversations = [
    summary,
    ...state.conversations.filter((conversation) => conversation.id !== summary.id),
  ].slice(0, 50);
  renderHistory();
}

function renderHistory() {
  elements.history.replaceChildren();
  elements.historyEmpty.hidden = state.conversations.length > 0;
  for (const conversation of state.conversations) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "history-item";
    if (conversation.id === state.conversationId) button.classList.add("active");
    button.title = conversation.title;

    const title = document.createElement("span");
    title.className = "history-title";
    title.textContent = conversation.title;
    let tag = null;
    if (conversation.project_name) {
      tag = document.createElement("span");
      tag.className = "history-project-tag";
      tag.textContent = conversation.project_name;
    }
    const meta = document.createElement("span");
    meta.className = "history-meta";
    const turns = document.createElement("span");
    turns.textContent = `${conversation.turn_count} 轮`;
    const status = document.createElement("span");
    status.className = `history-status ${conversation.status}`;
    status.title = eventLabel(conversation.status);
    meta.append(turns, status);
    button.append(title);
    if (tag) button.append(tag);
    button.append(meta);
    button.addEventListener("click", () => { void selectConversation(conversation.id); });
    elements.history.appendChild(button);
  }
}

async function loadConfig() {
  try {
    const config = await request("/api/config");
    elements.model.textContent = config.model;
    elements.runtimeLabel.textContent = "本地运行时已连接";
  } catch (error) {
    elements.runtimeLabel.textContent = error.message;
  }
}

async function loadHistory() {
  const data = await request("/api/conversations");
  state.conversations = data.conversations || [];
  renderHistory();
}

function formatProjectDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric" }).format(date);
}

function renderProjects() {
  const query = elements.projectSearch.value.trim().toLocaleLowerCase();
  const projects = state.projects.filter((project) => (
    !query || `${project.name} ${project.goal}`.toLocaleLowerCase().includes(query)
  ));
  elements.projectList.replaceChildren();
  elements.projectEmpty.hidden = projects.length > 0;
  for (const project of projects) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "project-row";
    const identity = document.createElement("span");
    identity.className = "project-identity";
    const folder = document.createElement("span");
    folder.className = "project-folder";
    folder.setAttribute("aria-hidden", "true");
    const copy = document.createElement("span");
    copy.className = "project-row-copy";
    const name = document.createElement("strong");
    name.textContent = project.name;
    const goal = document.createElement("small");
    goal.textContent = project.goal;
    copy.append(name, goal);
    identity.append(folder, copy);
    const count = document.createElement("span");
    count.className = "project-count";
    count.textContent = `${project.conversation_count || 0} 个对话`;
    const updated = document.createElement("time");
    updated.className = "project-updated";
    updated.textContent = formatProjectDate(project.updated_at);
    button.append(identity, count, updated);
    button.addEventListener("click", () => { void selectProject(project.id); });
    elements.projectList.appendChild(button);
  }
}

async function loadProjects() {
  const data = await request("/api/projects");
  state.projects = data.projects || [];
  renderProjects();
}

function renderProjectDetail(project) {
  state.projectId = project.id;
  state.activeProject = project;
  elements.projectsPage.hidden = true;
  elements.projectDetail.hidden = false;
  elements.projectDetailName.textContent = project.name;
  elements.projectDetailGoal.textContent = project.goal;
  const attachmentCount = project.attachments?.length || 0;
  const target = project.target_path ? ` · 路径 ${project.target_path}` : "";
  elements.projectDetailMeta.textContent = `${project.conversation_count || 0} 个对话 · ${attachmentCount} 个项目文件${target}`;
  elements.projectConversationList.replaceChildren();
  const conversations = project.conversations || [];
  if (!conversations.length) {
    const empty = document.createElement("p");
    empty.className = "project-conversation-empty";
    empty.textContent = "这个项目还没有对话。点击“新对话”开始。";
    elements.projectConversationList.appendChild(empty);
    return;
  }
  for (const conversation of conversations) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "project-conversation-item";
    const title = document.createElement("strong");
    title.textContent = conversation.title;
    const meta = document.createElement("span");
    meta.textContent = `${conversation.turn_count} 轮 · ${eventLabel(conversation.status)}`;
    button.append(title, meta);
    button.addEventListener("click", () => { void selectConversation(conversation.id); });
    elements.projectConversationList.appendChild(button);
  }
}

async function selectProject(projectId) {
  const navigationGeneration = ++state.navigationGeneration;
  try {
    const project = await request(`/api/projects/${projectId}`);
    if (navigationGeneration !== state.navigationGeneration) return;
    showProjectsPage();
    renderProjectDetail(project);
  } catch (error) {
    if (navigationGeneration !== state.navigationGeneration) return;
    elements.runtimeLabel.textContent = error.message;
  }
}

async function openProjects() {
  if (state.running) {
    elements.runtimeLabel.textContent = "当前任务仍在运行，请先停止或等待完成";
    return;
  }
  state.navigationGeneration += 1;
  stopPolling();
  showProjectsPage();
  try {
    await loadProjects();
  } catch (error) {
    elements.runtimeLabel.textContent = `项目加载失败：${error.message}`;
  }
}

function beginProjectConversation() {
  if (!state.activeProject || state.running) return;
  state.navigationGeneration += 1;
  stopPolling();
  state.conversationId = null;
  state.runId = null;
  state.attachments = [];
  state.projectPlanningRequired = state.activeProject.phase === "planning";
  renderAttachments();
  showConversationView();
  resetTimeline();
  renderHistory();
  elements.input.focus();
}

function stopPolling() {
  if (state.pollingTimer) clearTimeout(state.pollingTimer);
  state.pollingTimer = null;
  state.pollingGeneration += 1;
}

async function pollingLoop(generation) {
  const runId = state.activeRunId;
  if (!runId || generation !== state.pollingGeneration) return;
  try {
    const data = await request(`/api/runs/${runId}?after=${state.lastEventId}`);
    if (runId !== state.activeRunId || generation !== state.pollingGeneration) return;

    upsertConversation(data);
    if (state.conversationId === data.conversation_id && state.runId === runId) {
      renderProcessEvents(runId, data.events, data);
      state.lastEventId = data.next_event_id;
      updateApproval(data.approval);
    }

    const terminal = TERMINAL_STATUSES.has(data.status);
    elements.runtimeLabel.textContent = eventLabel(data.status);
    trackProjectPlanning(data);
    if (terminal) {
      state.activeRunId = null;
      state.pollingTimer = null;
      setRunning(false);
      if (state.conversationId === data.conversation_id && state.runId === runId) {
        updateApproval(null);
        renderFinalAnswer(runId, data.result, data.error);
        renderProof(runId, data.result);
      }
      return;
    }
    state.pollingTimer = setTimeout(() => { void pollingLoop(generation); }, 650);
  } catch (error) {
    if (generation !== state.pollingGeneration) return;
    elements.runtimeLabel.textContent = error.message;
    state.pollingTimer = setTimeout(() => { void pollingLoop(generation); }, 1500);
  }
}

function startPolling() {
  stopPolling();
  const generation = state.pollingGeneration;
  void pollingLoop(generation);
}

async function selectConversation(conversationId) {
  if (state.running && !state.activeRunId) {
    elements.runtimeLabel.textContent = "正在提交任务，请稍候";
    return;
  }
  if (state.activeRunId && conversationId !== state.conversationId) {
    elements.runtimeLabel.textContent = "当前任务仍在运行，请先停止或等待完成";
    return;
  }
  const navigationGeneration = ++state.navigationGeneration;
  const selectingActiveConversation = Boolean(state.activeRunId && conversationId === state.conversationId);
  stopPolling();
  try {
    const conversation = await request(`/api/conversations/${conversationId}`);
    if (navigationGeneration !== state.navigationGeneration) return;
    renderConversation(conversation);
    const latest = conversation.turns?.at(-1) || null;
    if (!latest || TERMINAL_STATUSES.has(latest.status)) {
      if (state.activeRunId === latest?.id) state.activeRunId = null;
      setRunning(false);
      elements.runtimeLabel.textContent = latest ? eventLabel(latest.status) : "新对话";
      return;
    }
    state.activeRunId = latest.id;
    state.runId = latest.id;
    state.lastEventId = latest.next_event_id || 0;
    setRunning(true);
    elements.runtimeLabel.textContent = eventLabel(latest.status);
    startPolling();
  } catch (error) {
    if (navigationGeneration !== state.navigationGeneration) return;
    elements.runtimeLabel.textContent = error.message;
    if (selectingActiveConversation && state.activeRunId) startPolling();
  }
}

function renderAttachmentChips(attachments, list, ariaPrefix, rerender) {
  list.replaceChildren();
  attachments.forEach((attachment, index) => {
    const chip = document.createElement("div");
    chip.className = "attachment-chip";
    const name = document.createElement("span");
    name.textContent = attachment.name;
    name.title = attachment.path;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "attachment-remove";
    remove.setAttribute("aria-label", `${ariaPrefix} ${attachment.name}`);
    remove.textContent = "×";
    remove.addEventListener("click", () => {
      attachments.splice(index, 1);
      rerender();
    });
    chip.append(name, remove);
    list.appendChild(chip);
  });
}

function renderAttachments() {
  renderAttachmentChips(state.attachments, elements.attachmentList, "移除附件", renderAttachments);
}

function renderProjectAttachments() {
  renderAttachmentChips(
    state.projectAttachments,
    elements.projectAttachmentList,
    "移除项目文件",
    renderProjectAttachments,
  );
}

function openProjectDialog() {
  if (state.running || state.uploading) return;
  elements.projectName.value = "";
  elements.projectGoal.value = "";
  elements.projectPreferences.value = "";
  elements.projectTargetPath.value = "";
  elements.projectFileInput.value = "";
  elements.projectUploadStatus.textContent = "可选；文件保存到工作区 uploads/";
  state.projectAttachments = [];
  renderProjectAttachments();
  elements.projectDialog.showModal();
  elements.projectName.focus();
}

function closeProjectDialog() {
  if (!state.uploading) elements.projectDialog.close();
}

async function uploadProjectFiles(files) {
  if (state.running || state.uploading || !files.length) return;
  state.uploading = true;
  updateControls();
  try {
    for (const file of files) {
      if (state.projectAttachments.length >= MAX_ATTACHMENTS) {
        elements.projectUploadStatus.textContent = `每个项目最多添加 ${MAX_ATTACHMENTS} 个初始文件`;
        break;
      }
      if (file.size > MAX_UPLOAD_BYTES) {
        elements.projectUploadStatus.textContent = `${file.name} 超过 10 MiB，未上传`;
        continue;
      }
      elements.projectUploadStatus.textContent = `正在上传 ${file.name}…`;
      try {
        const uploaded = await request(`/api/uploads?filename=${encodeURIComponent(file.name)}`, {
          method: "POST",
          headers: { "Content-Type": "application/octet-stream" },
          body: file,
        });
        state.projectAttachments.push(uploaded);
        renderProjectAttachments();
        elements.projectUploadStatus.textContent = `${uploaded.name} 已保存到 ${uploaded.path}`;
      } catch (error) {
        elements.projectUploadStatus.textContent = `${file.name} 上传失败：${error.message}`;
      }
    }
  } finally {
    state.uploading = false;
    elements.projectFileInput.value = "";
    updateControls();
  }
}

async function uploadFiles(files) {
  if (state.running || !files.length) return;
  const uploadGeneration = state.navigationGeneration;
  state.uploading = true;
  updateControls();
  try {
    for (const file of files) {
      if (state.attachments.length >= MAX_ATTACHMENTS) {
        elements.uploadStatus.textContent = `每个任务最多添加 ${MAX_ATTACHMENTS} 个附件`;
        break;
      }
      if (file.size > MAX_UPLOAD_BYTES) {
        elements.uploadStatus.textContent = `${file.name} 超过 10 MiB，未上传`;
        continue;
      }
      elements.uploadStatus.textContent = `正在上传 ${file.name}…`;
      try {
        const uploaded = await request(`/api/uploads?filename=${encodeURIComponent(file.name)}`, {
          method: "POST",
          headers: { "Content-Type": "application/octet-stream" },
          body: file,
        });
        if (uploadGeneration !== state.navigationGeneration) {
          elements.uploadStatus.textContent = `${uploaded.name} 已保存，但因页面已切换而未附加`;
          break;
        }
        state.attachments.push(uploaded);
        renderAttachments();
        elements.uploadStatus.textContent = `${uploaded.name} 已保存到 ${uploaded.path}`;
      } catch (error) {
        elements.uploadStatus.textContent = `${file.name} 上传失败：${error.message}`;
      }
    }
  } finally {
    state.uploading = false;
    elements.fileInput.value = "";
    updateControls();
  }
}

function resetComposerHeight() {
  elements.input.style.height = "auto";
  elements.input.style.height = `${Math.min(elements.input.scrollHeight, 160)}px`;
}

async function submitTask(task, { toolPolicy = "full" } = {}) {
  if (!task || state.running || state.uploading) return false;
  const submissionGeneration = ++state.navigationGeneration;
  stopPolling();
  try {
    setRunning(true);
    const started = await request("/api/runs", {
      method: "POST",
      body: JSON.stringify({
        task,
        attachments: state.attachments.map((attachment) => attachment.path),
        conversation_id: state.conversationId,
        project_id: state.projectId,
        tool_policy: toolPolicy,
      }),
    });
    const continuingVisibleConversation = state.conversationId === started.conversation_id;
    state.conversationId = started.conversation_id;
    state.runId = started.id;
    state.activeRunId = started.id;
    if (continuingVisibleConversation) {
      appendTurn(started);
      elements.title.textContent = started.conversation_title;
    } else {
      renderSingleRun(started);
    }
    state.lastEventId = started.next_event_id || 0;
    updateApproval(started.approval);
    upsertConversation(started);
    elements.input.value = "";
    state.attachments = [];
    renderAttachments();
    resetComposerHeight();
    if (TERMINAL_STATUSES.has(started.status)) {
      state.activeRunId = null;
      setRunning(false);
      elements.runtimeLabel.textContent = eventLabel(started.status);
    } else {
      elements.runtimeLabel.textContent = "正在运行";
      startPolling();
    }
    trackProjectPlanning(started);
  } catch (error) {
    try {
      const active = await request("/api/runs/active");
      if (await renderActiveRun(active, submissionGeneration)) return true;
    } catch (_recoveryError) {
      // Keep the original submission error; recovery is best-effort.
    }
    if (submissionGeneration !== state.navigationGeneration) return false;
    state.activeRunId = null;
    setRunning(false);
    elements.runtimeLabel.textContent = error.message;
    return false;
  }
  return true;
}

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.projectPlanningRequired && state.projectId) {
    const draft = elements.input.value;
    const started = await submitTask(PROJECT_PLANNING_TASK, { toolPolicy: "read_only" });
    if (started) {
      elements.input.value = draft;
      resetComposerHeight();
    } else {
      elements.runtimeLabel.textContent = "落地方案生成失败；再次发送将先重试只读规划";
    }
    return;
  }
  await submitTask(elements.input.value.trim());
});

elements.input.addEventListener("input", resetComposerHeight);
elements.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    elements.form.requestSubmit();
  }
});

elements.attach.addEventListener("click", () => elements.fileInput.click());
elements.fileInput.addEventListener("change", () => { void uploadFiles(Array.from(elements.fileInput.files || [])); });
elements.projectAttach.addEventListener("click", () => elements.projectFileInput.click());
elements.projectFileInput.addEventListener("change", () => {
  void uploadProjectFiles(Array.from(elements.projectFileInput.files || []));
});
elements.projectsButton.addEventListener("click", () => { void openProjects(); });
elements.newProject.addEventListener("click", openProjectDialog);
elements.projectCancel.addEventListener("click", closeProjectDialog);
elements.projectCancelAction.addEventListener("click", closeProjectDialog);
elements.projectsBack.addEventListener("click", showProjectsPage);
elements.projectNewChat.addEventListener("click", beginProjectConversation);
elements.projectSearch.addEventListener("input", renderProjects);

elements.projectForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.running || state.uploading) return;
  const name = elements.projectName.value.trim();
  const goal = elements.projectGoal.value.trim();
  if (!name || !goal) return;
  state.uploading = true;
  updateControls();
  try {
    const project = await request("/api/projects", {
      method: "POST",
      body: JSON.stringify({
        name,
        goal,
        preferences: elements.projectPreferences.value.trim(),
        target_path: elements.projectTargetPath.value.trim(),
        attachments: state.projectAttachments.map((attachment) => attachment.path),
      }),
    });
    state.projects = [project, ...state.projects.filter((item) => item.id !== project.id)];
    elements.projectDialog.close();
    renderProjects();
    state.projectId = project.id;
    state.activeProject = project;
    state.uploading = false;
    updateControls();
    beginProjectConversation();
    state.projectPlanningRequired = true;
    elements.runtimeLabel.textContent = "项目已创建，正在生成落地方案";
    const started = await submitTask(PROJECT_PLANNING_TASK, { toolPolicy: "read_only" });
    if (!started) {
      elements.runtimeLabel.textContent = "项目已创建，但落地方案生成失败；再次发送将先重试只读规划";
    }
  } catch (error) {
    elements.projectUploadStatus.textContent = `创建失败：${error.message}`;
  } finally {
    state.uploading = false;
    updateControls();
  }
});

elements.newChat.addEventListener("click", () => {
  if (state.running) {
    elements.runtimeLabel.textContent = "当前任务仍在运行，请先停止或等待完成";
    return;
  }
  state.navigationGeneration += 1;
  stopPolling();
  state.conversationId = null;
  state.runId = null;
  state.projectId = null;
  state.activeProject = null;
  state.projectPlanningRequired = false;
  state.projectPlanningRunId = null;
  state.attachments = [];
  renderAttachments();
  showConversationView();
  resetTimeline();
  renderHistory();
  elements.input.focus();
});

elements.cancel.addEventListener("click", async () => {
  const runId = state.activeRunId;
  if (!runId) return;
  elements.cancel.disabled = true;
  try {
    await request(`/api/runs/${runId}/cancel`, { method: "POST" });
  } catch (error) {
    elements.cancel.disabled = false;
    elements.runtimeLabel.textContent = error.message;
  }
});

async function decide(approved) {
  const runId = state.activeRunId;
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
    elements.runtimeLabel.textContent = `审批提交失败：${error.message}，正在等待状态同步`;
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

async function renderActiveRun(active, navigationGeneration) {
  if (!active || navigationGeneration !== state.navigationGeneration) return false;

  let latest = active;
  try {
    const conversation = await request(`/api/conversations/${active.conversation_id}`);
    if (navigationGeneration !== state.navigationGeneration) return false;
    renderConversation(conversation);
    latest = conversation.turns?.at(-1) || active;
  } catch (_error) {
    if (navigationGeneration !== state.navigationGeneration) return false;
    renderSingleRun(active);
  }

  if (TERMINAL_STATUSES.has(latest.status)) {
    state.activeRunId = null;
    setRunning(false);
    elements.runtimeLabel.textContent = eventLabel(latest.status);
    trackProjectPlanning(latest);
    return true;
  }

  state.activeRunId = latest.id;
  state.runId = latest.id;
  state.lastEventId = latest.next_event_id || 0;
  setRunning(true);
  elements.runtimeLabel.textContent = eventLabel(latest.status);
  trackProjectPlanning(latest);
  startPolling();
  return true;
}

async function initialize() {
  const initializationGeneration = state.navigationGeneration;
  try {
    const [, historyResult, activeResult] = await Promise.allSettled([
      loadConfig(),
      loadHistory(),
      request("/api/runs/active"),
    ]);
    if (initializationGeneration !== state.navigationGeneration) return;
    if (activeResult.status === "rejected") throw activeResult.reason;
    const active = activeResult.value;
    if (await renderActiveRun(active, initializationGeneration)) return;
    if (historyResult.status === "rejected") {
      elements.runtimeLabel.textContent = `历史对话加载失败：${historyResult.reason.message}`;
      return;
    }
    if (state.conversations.length) await selectConversation(state.conversations[0].id);
  } catch (error) {
    elements.runtimeLabel.textContent = error.message;
  }
}

updateControls();
resetComposerHeight();
void initialize();
