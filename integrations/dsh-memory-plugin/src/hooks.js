/** Pure decisions for the DSH memory plugin. No Cordis import. */

import { randomUUID } from "node:crypto";

export const PLUGIN_NAME = "memory-kernel";

export function resolveConfig(config = {}, env = {}) {
  const budget = Number(config.budget ?? env.MEMORY_BUDGET ?? 1200);
  return {
    kernelUrl: String(config.kernelUrl || env.KERNEL_URL || "http://127.0.0.1:8741").replace(/\/$/, ""),
    userId: String(config.userId || env.MEMORY_USER_ID || "default"),
    agentId: String(config.agentId ?? env.MEMORY_AGENT_ID ?? ""),
    budget: Number.isFinite(budget) && budget > 0 ? budget : 1200,
    autoExtract: config.autoExtract !== false,
    sessionId: config.sessionId || null,
    wakeInbox: config.wakeInbox === false ? false : config.wakeInbox || null,
  };
}

export function textOf(message) {
  if (!message) return "";
  const content = message.content ?? message.data?.content;
  if (typeof content === "string") return content.trim();
  if (!Array.isArray(content)) return "";
  return content
    .filter((block) => block && (block.type === "text" || block.type === "output_text") && block.text)
    .map((block) => block.text)
    .join("\n")
    .trim();
}

export function sourceKind(message) {
  return message?.source?.kind || message?.data?.source?.kind || "";
}

export function latestUserQuery(messages) {
  const list = Array.isArray(messages) ? messages : [];
  for (let index = list.length - 1; index >= 0; index -= 1) {
    const message = list[index];
    const kind = sourceKind(message);
    if (kind && kind !== "user") continue;
    const text = textOf(message);
    if (text) return text;
  }
  return "";
}

export function injectionMessage(text) {
  // DSH persists every message it sends as a durable event and validates on
  // cold read that each one carries a stable identity; an id-less message makes
  // the stored session fail validation ("lacks an identified message"), so the
  // conversation can never be reopened.
  return {
    id: randomUUID(),
    role: "user",
    content: [{ type: "text", text }],
    source: { kind: `plugin:${PLUGIN_NAME}` },
  };
}

export function withMemory(decision, messages, prompt) {
  if (!prompt) return decision ?? { kind: "enter", messages: messages || [] };
  if (decision?.kind === "reject") return decision;
  const base = decision?.kind === "enter" ? decision.messages : messages || [];
  return {
    ...(decision || {}),
    kind: "enter",
    messages: [...base, injectionMessage(prompt)],
  };
}

export function emptyTurn() {
  return { userText: "", assistantText: "" };
}

export function captureEvent(state, event) {
  const next = { userText: state.userText || "", assistantText: state.assistantText || "" };
  if (!event || typeof event !== "object") return next;
  const type = event.type;
  const data = event.data || event;
  if (type === "user/message") {
    if (sourceKind(data) && sourceKind(data) !== "user") return next;
    const text = textOf(data);
    if (text) next.userText = text;
    return next;
  }
  if (type === "assistant/message") {
    // DSH appends `{ turn, step, message, usage, stream }`; the message is nested.
    // Older/other hosts append the message itself, so keep both shapes working.
    const text = textOf(data.message ?? data);
    if (text) next.assistantText = next.assistantText ? `${next.assistantText}\n${text}` : text;
  }
  return next;
}

export function sessionIdFrom(payload, settings) {
  if (settings.sessionId) return settings.sessionId;
  const agent = payload?.agent || {};
  return agent.id || agent.sessionId || "dsh";
}

export function formatInjection(prompt, intents) {
  const parts = [];
  if (prompt) parts.push(String(prompt).trim());
  const open = Array.isArray(intents) ? intents.slice(0, 5) : [];
  if (open.length) {
    const lines = open.map((intent) => {
      const due = intent.due_at ? `（${intent.due_at}）` : "";
      return `- ${intent.title || "待办"}${due}`;
    });
    parts.push(`【未完成待办】\n${lines.join("\n")}`);
  }
  return parts.filter(Boolean).join("\n\n");
}

export function planRecall(settings, query, sessionId) {
  return {
    method: "POST",
    path: "/recall",
    body: {
      user_id: settings.userId,
      agent_id: settings.agentId,
      session_id: sessionId,
      query,
      budget: settings.budget,
    },
  };
}

export function planPersist(state, settings, sessionId) {
  const ops = [];
  const base = {
    user_id: settings.userId,
    agent_id: settings.agentId,
    session_id: sessionId,
    metadata: { origin: PLUGIN_NAME },
  };
  if (state.userText?.trim()) {
    ops.push({ method: "POST", path: "/episodes", body: { ...base, role: "user", content: state.userText.trim() } });
  }
  if (state.assistantText?.trim()) {
    ops.push({
      method: "POST",
      path: "/episodes",
      body: { ...base, role: "assistant", content: state.assistantText.trim() },
    });
  }
  if (ops.length && settings.autoExtract) {
    ops.push({
      method: "POST",
      path: "/extract",
      query: { user_id: settings.userId, limit: 20 },
    });
  }
  return ops;
}
