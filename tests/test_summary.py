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
