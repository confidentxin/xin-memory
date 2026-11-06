/**
 * Desktop bubble delivery. Official Electron builds do not accept an external
 * session/prompt call, and `dsh --profile desktop` is reserved. The wake CLI
 * writes JSON here; this module, running inside the desktop Host, queues it
 * with agent.followup — the same inbox as the Send button.
 */

import fs from "node:fs/promises";
import path from "node:path";
import { randomUUID } from "node:crypto";

export function inboxDir(settings = {}, env = {}) {
  if (settings.wakeInbox) return settings.wakeInbox;
  if (env.WAKE_INBOX) return env.WAKE_INBOX;
  const home = env.DSH_HOME || path.join(env.USERPROFILE || env.HOME || "", ".dsh");
  return path.join(home, "wake-inbox");
}

export function unwrapAgent(value) {
  if (!value) return null;
  if (typeof value.followup === "function") return value;
  if (value.agent && typeof value.agent.followup === "function") return value.agent;
  return null;
}

export function followupTarget(agents, payload) {
  if (!agents) return null;
  const sessionId = payload?.sessionId;
  if (sessionId && typeof agents.get === "function") {
    const found = unwrapAgent(agents.get(sessionId));
    if (found) return found;
  }
  if (!sessionId && typeof agents.entries === "function") {
    const list = [...agents.entries()];
    if (list.length === 1) return unwrapAgent(list[0][1]);
  }
  return null;
}

export async function resolveAgent(agents, payload) {
  const live = followupTarget(agents, payload);
  if (live) return live;
  if (payload?.sessionId && agents && typeof agents.resume === "function") {
    const resumed = await agents.resume(payload.sessionId);
    return unwrapAgent(resumed);
  }
  return null;
}

export function followupMessage(prompt) {
  // Host schedule reminders use createUserMessage({ content, source:{kind:"schedule"} }).
  // API serialization still requires role (+ id) on the durable user message.
  return {
    id: randomUUID(),
    role: "user",
    content: [{ type: "text", text: prompt }],
    source: { kind: "schedule" },
  };
}

export async function consumeInbox(dir, agents, fsImpl = fs) {
  let names = [];
  try {
    names = await fsImpl.readdir(dir);
  } catch {
    return [];
  }
  const results = [];
  for (const name of names) {
    if (!name.endsWith(".json")) continue;
    const full = path.join(dir, name);
    let payload;
    try {
      payload = JSON.parse(await fsImpl.readFile(full, "utf8"));
    } catch (error) {
      results.push({ file: name, ok: false, reason: error.message });
      continue;
    }
    if (!payload?.prompt) {
      results.push({ file: name, ok: false, reason: "empty-prompt" });
      continue;
    }
    const agent = await resolveAgent(agents, payload);
    if (!agent) {
      results.push({ file: name, ok: false, reason: "session-not-live" });
      continue;
    }
    agent.followup(followupMessage(payload.prompt));
    const doneDir = path.join(dir, "done");
    await fsImpl.mkdir(doneDir, { recursive: true });
    await fsImpl.rename(full, path.join(doneDir, name));
    results.push({ file: name, ok: true });
  }
  return results;
}

export function readAgents(ctx) {
  if (!ctx || typeof ctx.get !== "function") return null;
  try {
    return ctx.get("agents");
  } catch (error) {
    console.error(`memory-kernel desktop wake skipped: ${error.message}`);
    return null;
  }
}

function logWake(error) {
  const message = error && error.message ? error.message : String(error);
  console.error(`memory-kernel desktop wake skipped: ${message}`);
}

export function startWakePoller(ctx, dir, options = {}) {
  const setIntervalImpl = options.setInterval || setInterval;
  const consume = options.consume || consumeInbox;
  const start = (agentCtx) => {
    const pump = () => {
      try {
        const agents = readAgents(agentCtx);
        return Promise.resolve(consume(dir, agents, fs)).catch(logWake);
      } catch (error) {
        logWake(error);
        return Promise.resolve();
      }
    };
    const timer = setIntervalImpl(pump, options.intervalMs || 2000);
    if (timer && typeof timer.unref === "function") timer.unref();
    if (typeof ctx.effect === "function") ctx.effect(() => () => clearInterval(timer));
    return timer;
  };

  if (ctx && typeof ctx.inject === "function") {
    try {
      ctx.inject(["agents"], function onAgents(agentCtx) {
        try {
          const scope = agentCtx && typeof agentCtx.get === "function" ? agentCtx : this;
          start(scope);
        } catch (error) {
          logWake(error);
        }
      });
      return;
    } catch (error) {
      logWake(error);
    }
  }

  start({
    get(name) {
      if (name !== "agents" || !ctx || typeof ctx.get !== "function") return null;
      try {
        return ctx.get("agents");
      } catch (error) {
        logWake(error);
        return null;
      }
    },
  });
}

export function mergeDesktopPatch(existing, snippet, id = "memory-kernel-plugin") {
  if (existing && existing.includes(`id: ${id}`)) return { text: existing, changed: false };
  const base = existing && existing.trim() ? `${existing.replace(/\s*$/, "")}\n` : "";
  return { text: `${base}${snippet.trim()}\n`, changed: true };
}
