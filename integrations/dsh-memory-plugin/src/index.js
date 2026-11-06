/**
 * Cordis plugin: inject kernel memory before the model step and persist the
 * turn afterward. Talks to memory-kernel over HTTP. Does not fire intents;
 * `memory-kernel tick` remains the only scheduler.
 */

import { inboxDir, startWakePoller } from "./desktop-wake.js";
import {
  captureEvent,
  emptyTurn,
  formatInjection,
  latestUserQuery,
  planPersist,
  planRecall,
  resolveConfig,
  sessionIdFrom,
  withMemory,
} from "./hooks.js";

export const name = "memory-kernel";

export function apply(ctx, config = {}) {
  const settings = resolveConfig(config, process.env);
  const fetchImpl = config.fetch || globalThis.fetch;
  let turn = emptyTurn();

  ctx.on("session/event", (_subject, event) => {
    turn = captureEvent(turn, event);
  });

  ctx.on("agent/pre-step", async (payload, next) => {
    const decision = typeof next === "function" ? await next() : undefined;
    const query = latestUserQuery(payload?.messages);
    if (!query || decision?.kind === "reject") return decision;
    const sessionId = sessionIdFrom(payload, settings);
    try {
      const recalled = await request(fetchImpl, settings, planRecall(settings, query, sessionId));
      let intents = [];
      try {
        const listed = await request(fetchImpl, settings, {
          method: "GET",
          path: "/intents",
          query: { user_id: settings.userId, agent_id: settings.agentId, status: "open" },
        });
        if (Array.isArray(listed)) intents = listed;
      } catch (error) {
        console.error(`memory-kernel intent list skipped: ${error.message}`);
      }
      const prompt = formatInjection(recalled?.prompt, intents);
      return withMemory(decision, payload?.messages, prompt);
    } catch (error) {
      console.error(`memory-kernel pre-step skipped: ${error.message}`);
      return decision;
    }
  });

  if (settings.wakeInbox !== false) {
    const dir = inboxDir(settings, process.env);
    try {
      startWakePoller(ctx, dir);
    } catch (error) {
      console.error(`memory-kernel desktop wake skipped: ${error.message}`);
    }
  }

  ctx.on("agent/turn-stopping", async (payload) => {
    const sessionId = sessionIdFrom(payload, settings);
    const ops = planPersist(turn, settings, sessionId);
    turn = emptyTurn();
    for (const op of ops) {
      try {
        await request(fetchImpl, settings, op);
      } catch (error) {
        console.error(`memory-kernel persist skipped: ${error.message}`);
      }
    }
  });
}

export async function request(fetchImpl, settings, op) {
  if (typeof fetchImpl !== "function") {
    throw new Error("fetch 不可用");
  }
  const url = new URL(settings.kernelUrl + op.path);
  if (op.query) {
    for (const [key, value] of Object.entries(op.query)) {
      if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, String(value));
    }
  }
  const response = await fetchImpl(url, {
    method: op.method || "GET",
    headers: op.body ? { "content-type": "application/json" } : undefined,
    body: op.body ? JSON.stringify(op.body) : undefined,
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `HTTP ${response.status}`);
  }
  if (response.status === 204) return null;
  const text = await response.text();
  if (!text) return null;
  return JSON.parse(text);
}
