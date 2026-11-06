import assert from "node:assert/strict";
import test from "node:test";

import { apply } from "../src/index.js";
import {
  captureEvent,
  emptyTurn,
  formatInjection,
  latestUserQuery,
  planPersist,
  planRecall,
  resolveConfig,
  withMemory,
} from "../src/hooks.js";

test("resolveConfig prefers explicit config and falls back to env", () => {
  const settings = resolveConfig(
    {},
    { KERNEL_URL: "http://kernel.test:8741/", MEMORY_USER_ID: "lin", MEMORY_BUDGET: "800" },
  );
  assert.equal(settings.kernelUrl, "http://kernel.test:8741");
  assert.equal(settings.userId, "lin");
  assert.equal(settings.budget, 800);
  assert.equal(settings.autoExtract, true);
});

test("latest user query skips plugin-sourced injections", () => {
  const query = latestUserQuery([
    { content: [{ type: "text", text: "旧问题" }], source: { kind: "user" } },
    { content: [{ type: "text", text: "【记忆内核】" }], source: { kind: "plugin:memory-kernel" } },
    { content: [{ type: "text", text: "我住在哪" }], source: { kind: "user" } },
  ]);
  assert.equal(query, "我住在哪");
});

test("withMemory appends one plugin message and keeps reject", () => {
  const entered = withMemory({ kind: "enter", messages: [{ role: "user" }] }, [], "画像");
  assert.equal(entered.messages.at(-1).source.kind, "plugin:memory-kernel");
  assert.match(entered.messages.at(-1).content[0].text, /画像/);
  const rejected = withMemory({ kind: "reject" }, [], "画像");
  assert.equal(rejected.kind, "reject");
  assert.equal(entered.messages[0].role, "user");
  assert.deepEqual(withMemory({ kind: "enter", messages: ["keep"] }, [], ""), {
    kind: "enter",
    messages: ["keep"],
  });
});

test("captureEvent records the user and assistant and ignores plugin echoes", () => {
  let state = emptyTurn();
  state = captureEvent(state, {
    type: "user/message",
    data: { content: [{ type: "text", text: "周五前提交方案" }], source: { kind: "user" } },
  });
  state = captureEvent(state, {
    type: "user/message",
    data: { content: [{ type: "text", text: "【记忆内核】" }], source: { kind: "plugin:memory-kernel" } },
  });
  state = captureEvent(state, {
    type: "assistant/message",
    data: { turn: 1, step: 1, message: { content: [{ type: "text", text: "记下了。" }] }, usage: {} },
  });
  assert.equal(state.userText, "周五前提交方案");
  assert.equal(state.assistantText, "记下了。");
});

test("captureEvent unwraps the DSH assistant envelope and keeps flat payloads", () => {
  const enveloped = captureEvent(emptyTurn(), {
    type: "assistant/message",
    data: { turn: 2, step: 1, message: { content: [{ type: "text", text: "成都。" }] }, stream: {} },
  });
  assert.equal(enveloped.assistantText, "成都。");

  const flat = captureEvent(emptyTurn(), {
    type: "assistant/message",
    data: { content: [{ type: "text", text: "成都。" }] },
  });
  assert.equal(flat.assistantText, "成都。");

  const streamed = captureEvent(emptyTurn(), {
    type: "assistant/message",
    data: { turn: 2, step: 2, message: { content: [{ type: "text", text: "第一句" }] } },
  });
  const twice = captureEvent(streamed, {
    type: "assistant/message",
    data: { turn: 2, step: 2, message: { content: [{ type: "text", text: "第二句" }] } },
  });
  assert.equal(twice.assistantText, "第一句\n第二句");
});

test("planPersist writes both roles then enqueues extract once", () => {
  const settings = resolveConfig({ userId: "lin", autoExtract: true });
  const ops = planPersist(
    { userText: "我叫周宁", assistantText: "好的" },
    settings,
    "sess-1",
  );
  assert.deepEqual(
    ops.map((op) => op.path),
    ["/episodes", "/episodes", "/extract"],
  );
  assert.equal(ops[0].body.role, "user");
  assert.equal(ops[1].body.role, "assistant");
  assert.equal(ops[0].body.session_id, "sess-1");
  assert.equal(ops[2].query.user_id, "lin");
  assert.equal(planPersist({ userText: "", assistantText: "" }, settings, "sess-1").length, 0);
});

test("planRecall targets the kernel user and budget", () => {
  const settings = resolveConfig({ userId: "lin", agentId: "web", budget: 900 });
  const op = planRecall(settings, "住在哪", "sess-1");
  assert.equal(op.path, "/recall");
  assert.equal(op.body.user_id, "lin");
  assert.equal(op.body.session_id, "sess-1");
  assert.equal(op.body.budget, 900);
});

test("formatInjection keeps open intents beside the prompt", () => {
  const text = formatInjection("【用户画像 L2】\n周宁", [{ title: "提交方案", due_at: "2026-11-06T09:00:00Z" }]);
  assert.match(text, /周宁/);
  assert.match(text, /提交方案/);
});

test("apply recalls before the step and persists on turn stop without ticking", async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url: String(url), method: init.method, body: init.body ? JSON.parse(init.body) : null });
    if (String(url).includes("/recall")) {
      return jsonResponse({ prompt: "【用户画像 L2】\n住在成都" });
    }
    if (String(url).includes("/intents")) {
      return jsonResponse([{ title: "提交方案", due_at: null, status: "open" }]);
    }
    return jsonResponse({ ok: true });
  };
  const handlers = {};
  const ctx = { on(name, fn) { handlers[name] = fn; } };
  apply(ctx, { fetch: fetchImpl, userId: "lin", kernelUrl: "http://kernel.test", autoExtract: true });

  handlers["session/event"](null, {
    type: "user/message",
    data: { content: [{ type: "text", text: "我住在哪" }], source: { kind: "user" } },
  });
  const decision = await handlers["agent/pre-step"](
    { messages: [{ content: [{ type: "text", text: "我住在哪" }], source: { kind: "user" } }], agent: { id: "agent-1" } },
    async () => ({ kind: "enter", messages: [{ content: "我住在哪" }] }),
  );
  assert.match(decision.messages.at(-1).content[0].text, /住在成都/);
  assert.match(decision.messages.at(-1).content[0].text, /提交方案/);
  assert.equal(calls.some((call) => call.url.includes("/tick")), false);

  handlers["session/event"](null, {
    type: "assistant/message",
    data: { turn: 1, step: 1, message: { content: [{ type: "text", text: "成都。" }] }, usage: {} },
  });
  const beforeStop = calls.length;
  await handlers["agent/turn-stopping"]({ agent: { id: "agent-1" } });
  const posts = calls.slice(beforeStop);
  assert.deepEqual(
    posts.map((call) => new URL(call.url).pathname),
    ["/episodes", "/episodes", "/extract"],
  );
  assert.equal(posts.some((call) => call.url.includes("/tick")), false);
});

function jsonResponse(payload) {
  return {
    ok: true,
    status: 200,
    async text() {
      return JSON.stringify(payload);
    },
  };
}
