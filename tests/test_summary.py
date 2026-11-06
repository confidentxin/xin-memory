from datetime import datetime, timedelta, timezone

from memory_kernel.service import MemoryService


def _old(hours_ago: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours_ago)


def _seed(svc: MemoryService, scope, base_hours: int) -> None:
    """A session with enough substance to be worth a digest."""
    svc.append_episode(
        scope,
        "user",
        "我们讨论了记忆系统要怎么压缩老会话，最后决定按三天无活动来判定结束",
        occurred_at=_old(base_hours),
    )
    svc.append_episode(
        scope,
        "assistant",
        "结论是生成一条纪要存成事实，这样换到新会话也能召回，不再随 L0 一起丢掉",
        occurred_at=_old(base_hours - 1),
    )


def test_idle_session_gets_a_digest(svc: MemoryService, users):
    scope = users()
    _seed(svc, scope, 24 * 5)

    report = svc.summarize_idle_sessions(days=3, user_id=scope.user_id)
    assert len(report["summarized"]) == 1

    digests = [f for f in svc.list_facts(scope) if f.predicate == "session_summary"]
    assert len(digests) == 1
    assert digests[0].metadata.get("kind") == "session_summary"
    assert digests[0].metadata.get("session_id") == scope.session_id
    # Single line: L1 already adds the bullet, so the content must not start with one.
    assert "\n" not in digests[0].content
    assert not digests[0].content.startswith("-")


def test_summary_is_not_repeated(svc: MemoryService, users):
    scope = users()
    _seed(svc, scope, 24 * 4)

    assert len(svc.summarize_idle_sessions(days=3, user_id=scope.user_id)["summarized"]) == 1
    assert svc.summarize_idle_sessions(days=3, user_id=scope.user_id)["summarized"] == []


def test_fresh_session_is_left_alone(svc: MemoryService, users):
    scope = users()
    _seed(svc, scope, 0)
    assert svc.summarize_idle_sessions(days=3, user_id=scope.user_id)["summarized"] == []


def test_trivial_session_is_skipped(svc: MemoryService, users):
    scope = users()
    svc.append_episode(scope, "user", "1", occurred_at=_old(24 * 6))
    assert svc.summarize_idle_sessions(days=3, user_id=scope.user_id)["summarized"] == []


def test_digest_reaches_l1_but_not_the_profile(svc: MemoryService, users):
    scope = users()
    _seed(svc, scope, 24 * 6)
    svc.summarize_idle_sessions(days=3, user_id=scope.user_id)

    result = svc.recall(scope, "摘要压缩")
    profile_section = result.prompt.split("【相关事实")[0]
    assert "纪要" not in profile_section


def test_last_episodes_returns_the_newest_rows(svc: MemoryService, users):
    """A digest must see today's tail, not the oldest 200 rows of a long session."""
    from memory_kernel import store

    scope = users()
    base = datetime.now(timezone.utc) - timedelta(hours=5)
    for index in range(5):
        svc.append_episode(scope, "user", f"第{index}条", occurred_at=base + timedelta(minutes=index))

    with svc.connect() as conn:
        newest = store.last_episodes(conn, scope, limit=3)
        oldest = store.list_episodes(conn, scope, limit=3)

    assert [item.content for item in newest] == ["第2条", "第3条", "第4条"]
    assert [item.content for item in oldest] == ["第0条", "第1条", "第2条"]