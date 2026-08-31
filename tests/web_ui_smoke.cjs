"use strict";

const assert = require("node:assert/strict");

class FakeClassList {
  constructor() {
    this.values = new Set();
  }

  add(...names) {
    names.forEach((name) => this.values.add(name));
  }

  contains(name) {
    return this.values.has(name);
  }
}

class FakeElement {
  constructor(name = "div", children = {}) {
    this.name = name;
    this.childrenBySelector = children;
    this.children = [];
    this.listeners = new Map();
    this.classList = new FakeClassList();
    this.style = {};
    this.hidden = false;
    this.disabled = false;
    this.open = false;
    this.textContent = "";
    this.innerHTML = "";
    this.value = "";
    this.files = [];
    this.scrollTop = 0;
    this.scrollHeight = 48;
  }

  querySelector(selector) {
    return this.childrenBySelector[selector] || null;
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  async emit(type, event = {}) {
    for (const listener of this.listeners.get(type) || []) await listener(event);
  }

  click() {
    return this.emit("click", { preventDefault() {} });
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  append(...children) {
    this.children.push(...children);
  }

  replaceChildren(...children) {
    this.children = children;
  }

  get childElementCount() {
    return this.children.length;
  }

  setAttribute(name, value) {
    this[name] = value;
  }

  focus() {}

  requestSubmit() {
    return this.emit("submit", { preventDefault() {} });
  }
}

function messageTemplate() {
  const role = new FakeElement("span");
  const body = new FakeElement("div");
  return new FakeElement("article", { ".message-role": role, ".message-body": body });
}

function processTemplate() {
  const events = new FakeElement("div");
  const count = new FakeElement("span");
  return new FakeElement("details", { ".process-events": events, ".process-count": count });
}

function processEventTemplate() {
  return new FakeElement("article", {
    ".process-kind": new FakeElement("span"),
    ".process-index": new FakeElement("span"),
    ".process-content": new FakeElement("pre"),
  });
}

function proofTemplate() {
  const root = new FakeElement("article", {
    ".proof-status": new FakeElement("span"),
    ".proof-summary": new FakeElement("p"),
    ".proof-files": new FakeElement("div"),
    ".proof-commands": new FakeElement("div"),
    ".proof-warning": new FakeElement("p"),
    ".transaction-message": new FakeElement("p"),
    ".proof-accept": new FakeElement("button"),
    ".proof-rollback": new FakeElement("button"),
    ".proof-download": new FakeElement("a"),
  });
  root.classList.add("proof-card");
  return root;
}

const selectors = [
  "#task-form", "#task-input", "#run-button", "#cancel-button", "#timeline", "#empty-state",
  "#approval-card", "#approval-description", "#approval-cwd", "#approval-command", ".exact-command",
  "#approve-button", "#deny-button", "#runtime-label", "#model", "#conversation-title", "#history-list",
  "#history-empty", "#new-chat-button", "#attach-button", "#file-input", "#attachment-list", "#upload-status",
  "#repair-preset", "#generate-preset", "#scenario-label",
];
const elements = Object.fromEntries(selectors.map((selector) => [selector, new FakeElement(selector)]));
const templates = {
  "#message-template": { content: { firstElementChild: { cloneNode: messageTemplate } } },
  "#process-template": { content: { firstElementChild: { cloneNode: processTemplate } } },
  "#process-event-template": { content: { firstElementChild: { cloneNode: processEventTemplate } } },
  "#proof-template": { content: { firstElementChild: { cloneNode: proofTemplate } } },
};

global.document = {
  querySelector(selector) {
    return templates[selector] || elements[selector] || null;
  },
  createElement(name) {
    return new FakeElement(name);
  },
};

const startedPayloads = [];
let resolvePendingConversation = null;
let activeSnapshot = null;
let recoverySnapshot = null;
let failNextPost = false;
let resolvePendingUpload = null;
function runSnapshot(index, requestBody) {
  const conversationId = index < 2 ? "a".repeat(32) : "b".repeat(32);
  const finalText = index === 0 ? "## Markdown reply\n\n**bold**" : `Reply ${index + 1}`;
  const events = index === 0 ? [
    { web_event_id: 1, event_type: "run_started", payload: {} },
    { web_event_id: 2, event_type: "tool_requested", payload: { tool: "read_file", arguments: { path: "x.py" } } },
    { web_event_id: 3, event_type: "tool_result", payload: { tool: "read_file", result: { ok: true } } },
    { web_event_id: 4, event_type: "model_reply", payload: { content: finalText, tool_calls: [] } },
    { web_event_id: 5, event_type: "run_finished", payload: { reason: "completed" } },
  ] : [];
  return {
    id: `run-${index + 1}`,
    conversation_id: conversationId,
    conversation_title: requestBody.task,
    turn_index: index < 2 ? index + 1 : 1,
    task: requestBody.task,
    attachments: requestBody.attachments,
    status: "finished",
    events,
    next_event_id: events.length,
    approval: null,
    result: {
      final_text: finalText,
      termination_reason: "completed",
      verification_status: "not_required",
      changed_files: [],
      steps: 1,
      successful: true,
      proof: index === 0 ? {
        source: "tracecoder_runtime",
        verification_status: "verified",
        steps: 1,
        file_changes: [{ path: "x.py", kind: "modified", diff: "-old\n+new\n" }],
        commands: [{ argv: ["python", "-m", "pytest"], purpose: "verify", exit_code: 0 }],
        transaction: { state: "not_required", rollback_available: false },
      } : null,
      transaction_state: "not_required",
      rollback_available: false,
    },
    error: null,
  };
}

function jsonResponse(data, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return data; },
  };
}

global.fetch = async (url, options = {}) => {
  if (url === "/api/config") return jsonResponse({ model: "test-model" });
  if (url === "/api/conversations") return jsonResponse({ conversations: [] });
  if (url === `/api/conversations/${"a".repeat(32)}`) {
    return new Promise((resolve) => {
      resolvePendingConversation = () => resolve(jsonResponse({
        id: "a".repeat(32),
        title: "First turn",
        status: "finished",
        turn_count: 2,
        turns: [runSnapshot(0, startedPayloads[0]), runSnapshot(1, startedPayloads[1])],
      }));
    });
  }
  if (url === "/api/runs/active") return jsonResponse(activeSnapshot);
  if (url.startsWith("/api/uploads?filename=")) {
    return new Promise((resolve) => {
      resolvePendingUpload = () => resolve(jsonResponse({
        name: "late.txt",
        path: "uploads/late.txt",
        size: 4,
      }, 201));
    });
  }
  if (url === "/api/runs/run-recovery?after=0") {
    activeSnapshot = null;
    return jsonResponse(recoverySnapshot);
  }
  if (url === "/api/runs" && options.method === "POST") {
    const body = JSON.parse(options.body);
    startedPayloads.push(body);
    if (failNextPost) {
      failNextPost = false;
      recoverySnapshot = {
        ...runSnapshot(startedPayloads.length - 1, body),
        id: "run-recovery",
        conversation_id: "c".repeat(32),
        conversation_title: body.task,
        status: "finished",
        result: { ...runSnapshot(0, body).result, final_text: "Recovered reply" },
      };
      activeSnapshot = {
        ...recoverySnapshot,
        status: "running",
        events: [],
        next_event_id: 0,
        result: null,
      };
      throw new Error("connection lost after accept");
    }
    return jsonResponse(runSnapshot(startedPayloads.length - 1, body), 201);
  }
  throw new Error(`Unexpected request: ${options.method || "GET"} ${url}`);
};

require("../src/tracecoder/web_static/markdown.js");
require("../src/tracecoder/web_static/app.js");

const flush = () => new Promise((resolve) => setImmediate(resolve));

(async () => {
  await flush();
  await flush();
  assert.equal(elements["#empty-state"].hidden, false);

  elements["#task-input"].value = "First turn";
  await elements["#task-form"].emit("submit", { preventDefault() {} });
  assert.equal(elements["#empty-state"].hidden, true);
  assert.equal(startedPayloads[0].conversation_id, null);

  const messages = elements["#timeline"].children.filter((node) => node.classList.contains("assistant"));
  assert.match(messages[0].querySelector(".message-body").innerHTML, /<h2>Markdown reply<\/h2>/);
  assert.match(messages[0].querySelector(".message-body").innerHTML, /<strong>bold<\/strong>/);
  const process = elements["#timeline"].children.find((node) => node.name === "details");
  assert.ok(process);
  assert.equal(process.open, false);
  const proof = elements["#timeline"].children.find((node) => node.classList.contains("proof-card"));
  assert.ok(proof);
  assert.match(proof.querySelector(".proof-summary").textContent, /1 个文件证据/);

  elements["#task-input"].value = "Second turn";
  await elements["#task-form"].emit("submit", { preventDefault() {} });
  assert.equal(startedPayloads[1].conversation_id, "a".repeat(32));
  assert.equal(elements["#empty-state"].hidden, true);

  const staleSelection = elements["#history-list"].children[0].click();
  await flush();
  await elements["#new-chat-button"].click();
  assert.ok(resolvePendingConversation);
  resolvePendingConversation();
  await staleSelection;
  await flush();
  assert.equal(elements["#empty-state"].hidden, false);
  assert.equal(elements["#conversation-title"].textContent, "新对话");

  elements["#task-input"].value = "Separate conversation";
  await elements["#task-form"].emit("submit", { preventDefault() {} });
  assert.equal(startedPayloads[2].conversation_id, null);
  assert.equal(elements["#empty-state"].hidden, true);

  await elements["#new-chat-button"].click();
  failNextPost = true;
  elements["#task-input"].value = "Recover accepted run";
  await elements["#task-form"].emit("submit", { preventDefault() {} });
  await flush();
  await flush();
  assert.equal(startedPayloads[3].conversation_id, null);
  assert.equal(elements["#runtime-label"].textContent, "运行结束");
  const recoveredMessages = elements["#timeline"].children
    .filter((node) => node.classList.contains("assistant"));
  assert.equal(recoveredMessages.at(-1).querySelector(".message-body").innerHTML, "<p>Recovered reply</p>");

  elements["#file-input"].files = [{ name: "late.txt", size: 4 }];
  await elements["#file-input"].emit("change");
  await flush();
  assert.ok(resolvePendingUpload);
  await elements["#new-chat-button"].click();
  resolvePendingUpload();
  await flush();
  await flush();
  assert.equal(elements["#attachment-list"].children.length, 0);
  assert.match(elements["#upload-status"].textContent, /页面已切换而未附加/);

  console.log("web UI smoke passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
