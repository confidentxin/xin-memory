import json
from pathlib import Path

import httpx

from memory_kernel.kernel_client import KernelClient
from memory_kernel.wake import (
    build_wake_prompt,
    default_wake_command,
    enqueue_desktop,
    fires_from_report,
    run_wakes,
    wake_via_kernel,
)


def test_prompt_is_notify_first_and_includes_proposal():
    notify = build_wake_prompt({"title": "提交方案", "action": "notify", "content": "画像"})
    assert notify.startswith("到期提醒：提交方案")
    assert "未经用户明确确认" in notify
    assert "画像" in notify
    proposed = build_wake_prompt(
        {"title": "给供应商转账", "action": "propose", "proposed_action": "执行：给供应商转账"}
    )
    assert "没有执行" in proposed
    assert "执行：给供应商转账" in proposed


def test_only_this_tick_fires_are_woken():
    assert fires_from_report({}) == []
    assert fires_from_report({"fired": [{"title": ""}]}) == []
    fired = [{"intent_id": "a", "title": "提交方案", "action": "notify", "user_id": "lin"}]
    calls = []

    def runner(command, prompt, env):
        calls.append((command, prompt, env["WAKE_TITLE"]))
        return 0

    result = run_wakes(
        fired,
        command=default_wake_command("headless"),
        dry_run=False,
        user_id="lin",
        profile="headless",
        runner=runner,
    )
    assert result["ok"] is True
    assert len(calls) == 1
    assert calls[0][0] == "dsh --profile headless -"
    assert "提交方案" not in calls[0][0]
    assert "到期提醒：提交方案" in calls[0][1]
    again = run_wakes([], command=calls[0][0], dry_run=False, user_id="lin", profile="headless", runner=runner)
    assert again["wakes"] == []
    assert len(calls) == 1


def test_dry_run_does_not_call_the_runner():
    calls = []
    result = run_wakes(
        [{"intent_id": "a", "title": "交周报", "action": "notify", "user_id": "lin"}],
        command="dsh --profile headless -",
        dry_run=True,
        user_id="lin",
        profile="headless",
        runner=lambda *args: calls.append(args) or 0,
    )
    assert calls == []
    assert result["wakes"][0]["dry_run"] is True
    assert result["wakes"][0]["returncode"] == 0


def test_wake_posts_tick_then_skips_dsh_on_dry_run():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["user"] = request.url.params.get("user_id")
        return httpx.Response(
            200,
            json={
                "fired": [
                    {
                        "intent_id": "11111111-1111-1111-1111-111111111111",
                        "user_id": "lin",
                        "title": "提交方案",
                        "action": "notify",
                        "episode_id": "22222222-2222-2222-2222-222222222222",
                        "proposed_action": None,
                        "content": "提醒：提交方案",
                    }
                ]
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://kernel.test")
    client = KernelClient("http://kernel.test", user_id="lin", http=http)
    calls = []
    result = wake_via_kernel(
        client,
        user_id="lin",
        command=default_wake_command("headless"),
        dry_run=True,
        profile="headless",
        runner=lambda *args: calls.append(args) or 0,
        # Pin the headless branch: the default target is the desktop inbox.
        target="headless",
    )
    assert seen == {"path": "/tick", "user": "lin"}
    assert calls == []
    assert result["fired"] == 1
    payload = json.dumps(result["wakes"][0], ensure_ascii=False)
    assert "到期提醒：提交方案" in payload
    assert "dsh --profile headless -" in payload


def test_desktop_enqueue_prefers_explicit_session_then_the_intent(tmp_path):
    fire = {
        "intent_id": "11111111-1111-1111-1111-111111111111",
        "episode_id": "22222222-2222-2222-2222-222222222222",
        "user_id": "lin",
        "title": "提交方案",
        "action": "notify",
        "content": "提醒：提交方案",
        "session_id": "session-from-intent",
    }

    explicit = enqueue_desktop([fire], inbox=tmp_path, session_id="session-from-cli", dry_run=False)
    explicit_payload = json.loads(Path(explicit["wakes"][0]["path"]).read_text(encoding="utf-8"))
    assert explicit_payload["sessionId"] == "session-from-cli"

    fallback = enqueue_desktop([fire], inbox=tmp_path, session_id=None, dry_run=False)
    fallback_payload = json.loads(Path(fallback["wakes"][0]["path"]).read_text(encoding="utf-8"))
    assert fallback_payload["sessionId"] == "session-from-intent"

    orphan = dict(fire, session_id=None)
    unresolvable = enqueue_desktop([orphan], inbox=tmp_path, session_id=None, dry_run=False)
    assert unresolvable["wakes"][0]["path"] is not None
    orphan_payload = json.loads(Path(unresolvable["wakes"][0]["path"]).read_text(encoding="utf-8"))
    assert orphan_payload["sessionId"] is None


def test_act_prompt_carries_no_ask_first_preamble():
    prompt = build_wake_prompt({"title": "整理目录", "action": "act", "content": "上下文"})
    assert prompt.startswith("到期任务：整理目录")
    assert "未经用户明确确认" not in prompt
    assert "直接做完，不要等确认" in prompt
    assert "上下文" in prompt


def test_notify_prompt_keeps_the_confirmation_guard():
    prompt = build_wake_prompt({"title": "交周报", "action": "notify"})
    assert "未经用户明确确认" in prompt