"""Turn a tick response into one wake per newly fired intent.

tick remains the only scheduler. This module only wakes items that this tick
already marked handled. Desktop mode does not append session logs; it drops a
prompt file the desktop Cordis plugin turns into agent.followup.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable

from memory_kernel.kernel_client import KernelClient

SAFETY = "请先通知用户。未经用户明确确认，不要执行任何副作用（发邮件、付款、删除、下单、改外部系统）。"

Runner = Callable[[str, str, dict[str, str]], int]


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def default_wake_command(profile: str) -> str:
    """Headless fallback. Official desktop builds reject `dsh --profile desktop`."""
    return f"dsh --profile {shlex.quote(profile or 'headless')} -"


def desktop_inbox(env: dict[str, str] | None = None) -> Path:
    env = env if env is not None else os.environ
    override = (env.get("WAKE_INBOX") or "").strip()
    if override:
        return Path(override)
    home = (env.get("DSH_HOME") or "").strip() or str(Path.home() / ".dsh")
    return Path(home) / "wake-inbox"


def _file_id(fire: dict[str, Any]) -> str:
    raw = f"{fire.get('intent_id') or 'intent'}-{fire.get('episode_id') or 'wake'}"
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", raw)
    return cleaned[:120] or "wake"


def fires_from_report(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(report, dict):
        return []
    fired = report.get("fired") or []
    if not isinstance(fired, list):
        return []
    return [item for item in fired if isinstance(item, dict) and item.get("title")]


def build_wake_prompt(fire: dict[str, Any]) -> str:
    title = str(fire.get("title") or "待办")
    action = str(fire.get("action") or "notify")
    content = str(fire.get("content") or "").strip()

    if action == "act":
        # Opt-in autonomous execution. No "ask first" preamble: acting is the
        # whole point of mode=act, and the human is not watching.
        lines = [
            f"到期任务：{title}",
            "这是你先前设定为自动执行的任务，现在是执行时间。直接做完，不要等确认。",
            "完成后用一段话汇报：做了什么、结果如何、留下了哪些痕迹（文件、链接、提交）。",
            "只有缺少必要凭据、或目标本身有歧义到无法安全推进时，才停下来提问。",
        ]
        if content:
            lines.append("内核组装的上下文：")
            lines.append(content)
        return "\n".join(lines)

    lines = [f"到期提醒：{title}", SAFETY]
    if action == "propose":
        lines.append("这是高风险待办。内核只记录了提议，没有执行。")
        proposed = fire.get("proposed_action")
        if proposed:
            lines.append(f"提议：{proposed}")
    if content:
        lines.append("内核组装的上下文：")
        lines.append(content)
    return "\n".join(lines)


def enqueue_desktop(
    fires: list[dict[str, Any]],
    *,
    inbox: Path,
    session_id: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Write one inbox file per newly fired intent. Dry-run writes nothing."""
    wakes: list[dict[str, Any]] = []
    for fire in fires:
        prompt = build_wake_prompt(fire)
        filename = _file_id(fire) + ".json"
        path = inbox / filename
        payload = {
            "id": filename[:-5],
            "intentId": str(fire.get("intent_id") or ""),
            "episodeId": str(fire.get("episode_id") or ""),
            # Explicit CLI/env session wins; otherwise fall back to the session that
            # owns the intent, so a plain `wake --user` still reaches the conversation.
            "sessionId": session_id or fire.get("session_id") or None,
            "userId": str(fire.get("user_id") or ""),
            "title": str(fire.get("title") or ""),
            "action": str(fire.get("action") or "notify"),
            "prompt": prompt,
        }
        if not dry_run:
            inbox.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        wakes.append(
            {
                "intent_id": payload["intentId"],
                "user_id": payload["userId"],
                "title": payload["title"],
                "action": payload["action"],
                "prompt": prompt,
                "command": f"desktop-inbox:{path}",
                "path": None if dry_run else str(path),
                "dry_run": dry_run,
                "returncode": 0,
                "target": "desktop",
            }
        )
    return {"fired": len(fires), "wakes": wakes, "ok": True, "target": "desktop"}


def run_wakes(
    fires: list[dict[str, Any]],
    *,
    command: str,
    dry_run: bool,
    user_id: str,
    profile: str,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Wake once per item in this tick's fired list. An empty list wakes nothing."""
    execute = runner or subprocess_runner
    wakes: list[dict[str, Any]] = []
    for fire in fires:
        prompt = build_wake_prompt(fire)
        env = {
            **os.environ,
            "WAKE_PROMPT": prompt,
            "WAKE_TITLE": str(fire.get("title") or ""),
            "WAKE_INTENT_ID": str(fire.get("intent_id") or ""),
            "WAKE_ACTION": str(fire.get("action") or ""),
            "WAKE_USER_ID": str(fire.get("user_id") or user_id),
            "DSH_PROFILE": profile,
        }
        returncode = 0 if dry_run else int(execute(command, prompt, env))
        wakes.append(
            {
                "intent_id": str(fire.get("intent_id") or ""),
                "user_id": str(fire.get("user_id") or user_id),
                "title": str(fire.get("title") or ""),
                "action": str(fire.get("action") or ""),
                "prompt": prompt,
                "command": command,
                "dry_run": dry_run,
                "returncode": returncode,
            }
        )
    failed = [item for item in wakes if item["returncode"] not in (0, None)]
    return {"fired": len(fires), "wakes": wakes, "ok": not failed}


def subprocess_runner(command: str, prompt: str, env: dict[str, str]) -> int:
    completed = subprocess.run(
        command,
        shell=True,
        input=prompt,
        text=True,
        env=env,
        check=False,
    )
    return int(completed.returncode)


def wake_via_kernel(
    client: KernelClient,
    *,
    user_id: str | None,
    command: str,
    dry_run: bool,
    profile: str,
    now: str | None = None,
    limit: int = 50,
    runner: Runner | None = None,
    target: str = "desktop",
    session_id: str | None = None,
    inbox: Path | None = None,
) -> dict[str, Any]:
    report = client.tick(user_id=user_id or None, now=now, limit=limit)
    fires = fires_from_report(report)
    if target == "desktop":
        result = enqueue_desktop(
            fires,
            inbox=inbox or desktop_inbox(),
            session_id=session_id,
            dry_run=dry_run,
        )
    else:
        result = run_wakes(
            fires,
            command=command,
            dry_run=dry_run,
            user_id=user_id or "",
            profile=profile,
            runner=runner,
        )
        result["target"] = "headless"
    result["tick"] = report
    return result
