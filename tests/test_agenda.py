from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from memory_kernel.api import app
from memory_kernel.service import MemoryService


def test_utterance_becomes_intent_and_due_tick_notifies(svc: MemoryService, users):
    scope = users()
    svc.append_episode(scope, "user", "我叫周宁。")
    svc.append_episode(scope, "user", "周五前提交方案")
    report = svc.process_pending(user_id=scope.user_id)
    assert report.intents_added == 1

    intent = svc.list_intents(scope)[0]
    assert intent.title == "提交方案"
    assert intent.status == "open"
    assert intent.triggers[0].type == "once_at"
    assert intent.source_episode_ids

    past = datetime(2026, 11, 1, tzinfo=timezone.utc)
    svc.reschedule_intent(scope, intent.id, past)
    fired = svc.tick(user_id=scope.user_id, now=datetime(2026, 11, 3, tzinfo=timezone.utc))
    assert len(fired.fired) == 1
    assert fired.fired[0].action == "notify"
    assert fired.fired[0].content.startswith("提醒：提交方案")
    assert "周宁" in fired.fired[0].content

    episodes = svc.list_episodes(scope)
    assert any(episode.role == "assistant" and episode.metadata.get("action") == "notify" for episode in episodes)
    assert svc.tick(user_id=scope.user_id, now=datetime(2026, 11, 4, tzinfo=timezone.utc)).fired == []

    done = svc.complete_intent(scope, intent.id)
    assert done.status == "done"
    assert all(trigger.enabled is False for trigger in done.triggers)
    assert svc.tick(user_id=scope.user_id, now=datetime(2026, 11, 5, tzinfo=timezone.utc)).fired == []


def test_completed_intent_does_not_fire(svc: MemoryService, users):
    scope = users()
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    intent = svc.create_intent(scope, "交周报", due_at=past, description="本周周报")
    svc.complete_intent(scope, intent.id)
    assert svc.tick(user_id=scope.user_id).fired == []


def test_explicit_reminder_is_captured_on_append(svc: MemoryService, users):
    scope = users()
    episode = svc.append_episode(scope, "user", "提醒我喝水")
    intents = svc.list_intents(scope)
    assert len(intents) == 1
    assert intents[0].title == "喝水"
    assert episode.id in intents[0].source_episode_ids
    report = svc.process_pending(user_id=scope.user_id)
    assert report.intents_added == 0
    assert len(svc.list_intents(scope)) == 1


def test_tick_isolates_users(svc: MemoryService, users):
    left = users()
    right = users()
    past = datetime(2026, 10, 1, tzinfo=timezone.utc)
    now = datetime(2026, 11, 3, tzinfo=timezone.utc)
    svc.create_intent(left, "给甲的提醒", due_at=past)
    svc.create_intent(right, "给乙的提醒", due_at=past)

    fired = svc.tick(user_id=left.user_id, now=now)
    assert [item.user_id for item in fired.fired] == [left.user_id]
    assert all(item.title == "给甲的提醒" for item in fired.fired)

    right_episodes = svc.list_episodes(right)
    assert all("给甲的提醒" not in episode.content for episode in right_episodes)
    assert [intent.title for intent in svc.list_intents(right)] == ["给乙的提醒"]
    assert svc.list_intents(right)[0].status == "open"
    assert [intent.title for intent in svc.list_intents(left)] == ["给甲的提醒"]


def test_high_risk_is_proposed_not_executed(svc: MemoryService, users):
    scope = users()
    past = datetime(2026, 10, 2, tzinfo=timezone.utc)
    intent = svc.create_intent(scope, "给供应商转账", due_at=past, description="转 2 万")
    assert intent.metadata["policy"] == "propose"
    assert intent.metadata["proposed_action"] == "执行：给供应商转账"

    fired = svc.tick(user_id=scope.user_id, now=datetime(2026, 11, 3, tzinfo=timezone.utc))
    assert fired.fired[0].action == "propose"
    assert "没有自动执行" in fired.fired[0].content
    assert fired.fired[0].proposed_action == "执行：给供应商转账"
    stored = svc.list_intents(scope)[0]
    assert stored.status == "open"
    assert stored.metadata["proposed_action"] == "执行：给供应商转账"
    assert svc.tick(user_id=scope.user_id, now=datetime(2026, 11, 3, 1, tzinfo=timezone.utc)).fired == []


def test_cancel_and_foreign_user(svc: MemoryService, users):
    scope = users()
    other = users()
    intent = svc.create_intent(scope, "买咖啡豆")
    cancelled = svc.cancel_intent(scope, intent.id)
    assert cancelled.status == "cancelled"
    try:
        svc.complete_intent(other, intent.id)
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 404
    else:
        raise AssertionError("其他用户不应完成这条待办")


def test_http_intent_roundtrip(svc: MemoryService, users):
    scope = users()
    with TestClient(app) as client:
        created = client.post(
            "/intents",
            json={
                "user_id": scope.user_id,
                "session_id": scope.session_id,
                "title": "回复邮件",
                "due_at": "2026-10-01T00:00:00Z",
            },
        )
        assert created.status_code == 200
        body = created.json()
        assert body["triggers"][0]["type"] == "once_at"
        listed = client.get("/intents", params={"user_id": scope.user_id})
        assert listed.status_code == 200
        assert len(listed.json()) == 1
        done = client.post(f"/intents/{body['id']}/complete", params={"user_id": scope.user_id})
        assert done.status_code == 200
        assert done.json()["status"] == "done"
        tick = client.post("/tick", params={"user_id": scope.user_id})
        assert tick.status_code == 200
        assert tick.json()["fired"] == []


def test_schema_includes_agenda_tables(svc: MemoryService):
    svc.migrate()
    with svc.connect() as conn:
        tables = {
            row["tablename"]
            for row in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()
        }
    assert {"intents", "triggers", "episodes", "facts", "profile_blocks"} <= tables

def test_act_mode_fires_autonomously_and_retires_the_intent(svc: MemoryService, users):
    scope = users()
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    intent = svc.create_intent(
        scope,
        "整理记忆测试目录",
        due_at=past,
        description="自动执行",
        metadata={"mode": "act"},
    )
    fired = svc.tick(user_id=scope.user_id)
    assert len(fired.fired) == 1
    assert fired.fired[0].action == "act"
    assert fired.fired[0].content.startswith("自动执行：整理记忆测试目录")

    stored = {item.id: item for item in svc.list_intents(scope)}[intent.id]
    assert stored.status == "done"
    assert all(trigger.enabled is False for trigger in stored.triggers)
    assert svc.tick(user_id=scope.user_id).fired == []


def test_act_halt_downgrades_to_notify(svc: MemoryService, users, monkeypatch):
    monkeypatch.setenv("MEMORY_ACT_HALT", "1")
    scope = users()
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    svc.create_intent(scope, "清理临时文件", due_at=past, metadata={"mode": "act"})
    fired = svc.tick(user_id=scope.user_id)
    assert len(fired.fired) == 1
    assert fired.fired[0].action == "notify"
    assert fired.fired[0].content.startswith("提醒：清理临时文件")


def test_parse_due_lands_on_local_nine():
    from memory_kernel.agenda import parse_due

    due = parse_due("明天交方案", datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc))
    local = due.astimezone()
    assert (local.hour, local.minute) == (9, 0)


def test_act_heading_is_never_doubled():
    from memory_kernel.agenda import render_action_episode

    once = render_action_episode(
        title="自动执行：清理临时文件", due_at=None, policy="act", context="", proposed=None
    )
    assert once.count("自动执行：") == 1
    assert once.startswith("自动执行：清理临时文件")

    plain = render_action_episode(
        title="清理临时文件", due_at=None, policy="act", context="", proposed=None
    )
    assert plain.startswith("自动执行：清理临时文件")


def test_act_mode_executes_even_when_the_title_sounds_risky(svc: MemoryService, users, monkeypatch):
    monkeypatch.delenv("MEMORY_ACT_HALT", raising=False)
    scope = users()
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    svc.create_intent(scope, "删除旧文件", due_at=past, metadata={"mode": "act"})
    fired = svc.tick(user_id=scope.user_id)
    assert len(fired.fired) == 1
    # 删除 would normally be proposed; mode=act is the explicit override.
    assert fired.fired[0].action == "act"
    assert fired.fired[0].content.startswith("自动执行：删除旧文件")

def test_cron_intent_refires_and_is_not_retired(svc: MemoryService, users):
    scope = users()
    intent = svc.create_intent(scope, "每天早上自主检查项目", cron="30 9 * * *", metadata={"mode": "act"})
    assert intent.triggers[0].type == "cron"
    assert intent.triggers[0].expression == "30 9 * * *"

    local_nine_thirty = datetime(2026, 11, 4, 9, 30).astimezone()
    first = svc.tick(user_id=scope.user_id, now=local_nine_thirty)
    assert len(first.fired) == 1
    assert first.fired[0].action == "act"

    stored = {item.id: item for item in svc.list_intents(scope)}[intent.id]
    assert stored.status != "done", "a cron task must survive its first run"

    # Same minute, a few seconds later: the trigger must not fire twice.
    repeat = svc.tick(user_id=scope.user_id, now=local_nine_thirty + timedelta(seconds=20))
    assert repeat.fired == []

    tomorrow = svc.tick(user_id=scope.user_id, now=datetime(2026, 11, 5, 9, 30).astimezone())
    assert len(tomorrow.fired) == 1


def test_invalid_cron_is_rejected(svc: MemoryService, users):
    import pytest

    from memory_kernel.service import MemoryError

    scope = users()
    with pytest.raises(MemoryError):
        svc.create_intent(scope, "坏表达式", cron="every day at nine")


def test_valid_cron_helper():
    from memory_kernel.agenda import valid_cron

    assert valid_cron("0 9 * * *") is True
    assert valid_cron("30 9 * * 1") is True
    assert valid_cron("* * * * *") is True
    assert valid_cron("0 9 * *") is False
    assert valid_cron("0 25 * * *") is False
    assert valid_cron("0 9 * * mom") is False

def test_cron_catches_up_after_a_missed_minute(svc: MemoryService, users):
    """10:00 job still runs at 10:37 if the machine was asleep at 10:00."""
    scope = users()
    intent = svc.create_intent(scope, "每天十点的活", cron="0 10 * * *", metadata={"mode": "act"})

    at_1037 = datetime(2026, 11, 7, 10, 37).astimezone()
    fired = svc.tick(user_id=scope.user_id, now=at_1037)
    assert len(fired.fired) == 1
    assert fired.fired[0].intent_id == intent.id

    # Same day, later: no second catch-up.
    assert svc.tick(user_id=scope.user_id, now=datetime(2026, 11, 7, 15, 0).astimezone()).fired == []

    # Next day it runs again.
    assert len(svc.tick(user_id=scope.user_id, now=datetime(2026, 11, 8, 10, 5).astimezone()).fired) == 1


def test_cron_does_not_catch_up_before_its_time(svc: MemoryService, users):
    scope = users()
    svc.create_intent(scope, "还没到点", cron="0 10 * * *")
    assert svc.tick(user_id=scope.user_id, now=datetime(2026, 11, 7, 9, 30).astimezone()).fired == []


def test_last_occurrence_today_helper():
    from memory_kernel.agenda import last_occurrence_today

    at = datetime(2026, 11, 7, 10, 37).astimezone()
    found = last_occurrence_today("0 10 * * *", at)
    assert found is not None
    assert (found.hour, found.minute) == (10, 0)
    assert last_occurrence_today("0 23 * * *", at) is None