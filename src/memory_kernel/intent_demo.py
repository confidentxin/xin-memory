"""Show a deadline utterance becoming a reminder written back to L0."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from memory_kernel.config import load_settings
from memory_kernel.models import Scope
from memory_kernel.service import MemoryService
from memory_kernel.util import redact_dsn

DEMO_USER = "demo-agenda"
OTHER_USER = "demo-agenda-other"


def run_intent_demo() -> None:
    settings = load_settings()
    service = MemoryService(settings)
    print(f"数据库 {redact_dsn(settings.database_url)}")
    print(f"抽取器 {service.extractor.name}")
    service.migrate()
    service.purge_user(DEMO_USER)
    service.purge_user(OTHER_USER)

    scope = Scope(user_id=DEMO_USER, session_id="sess-agenda")
    print("\n== 用户说了一句带截止的话 ==")
    service.append_episode(scope, "user", "我叫周宁。")
    service.append_episode(scope, "user", "周五前提交方案")
    report = service.process_pending(user_id=DEMO_USER)
    print(f"  冷路径 facts_added={report.facts_added} intents_added={report.intents_added}")

    intents = service.list_intents(scope)
    _expect(len(intents) == 1, "应从「周五前提交方案」写入一条待办")
    intent = intents[0]
    print(f"  intent {intent.title} status={intent.status} due={intent.due_at} trigger={intent.triggers[0].type}")
    _expect(intent.title == "提交方案", "标题应是提交方案")
    _expect(intent.triggers and intent.triggers[0].type == "once_at", "截止时间应变成 once_at 触发器")

    past = datetime.now(timezone.utc) - timedelta(hours=2)
    print("\n== 把 due 调成已经到期 ==")
    intent = service.reschedule_intent(scope, intent.id, past)
    print(f"  due_at={intent.due_at}")

    print("\n== tick ==")
    fired = service.tick(user_id=DEMO_USER, now=datetime.now(timezone.utc))
    _expect(len(fired.fired) == 1, "到期后 tick 应产生一次提醒")
    item = fired.fired[0]
    print(item.content)
    print(f"\n  action={item.action} episode={item.episode_id}")
    _expect(item.action == "notify", "普通待办应走 notify")
    _expect(item.content.startswith("提醒：提交方案"), "提醒应写明待办")
    _expect("周宁" in item.content, "提醒上下文应带上 L2 画像")

    episodes = service.list_episodes(scope)
    _expect(
        any(episode.role == "assistant" and episode.content.startswith("提醒：提交方案") for episode in episodes),
        "提醒应写回 L0",
    )
    again = service.tick(user_id=DEMO_USER, now=datetime.now(timezone.utc))
    _expect(again.fired == [], "同一 once_at 不应重复触发")

    done = service.complete_intent(scope, intent.id)
    _expect(done.status == "done", "完成后状态为 done")
    third = service.tick(user_id=DEMO_USER, now=datetime.now(timezone.utc) + timedelta(days=1))
    _expect(third.fired == [], "完成后不再触发")

    others = service.list_intents(Scope(user_id=OTHER_USER))
    _expect(others == [], "其他用户看不到这条待办")

    print("\nDEMO INTENT OK")


def _expect(condition: bool, message: str) -> None:
    if not condition:
        print(f"DEMO 失败: {message}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    run_intent_demo()
