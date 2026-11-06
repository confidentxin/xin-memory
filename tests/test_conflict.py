from memory_kernel.service import MemoryService
from memory_kernel.util import norm


def test_remember_supersedes_singleton_fact(svc: MemoryService, users):
    scope = users()
    first = svc.remember(scope, "我住在杭州")
    second = svc.remember(scope, "我住在杭州市")
    # 市后缀归一后视为同一事实，不制造第二条。
    assert second.added == []
    assert second.skipped

    third = svc.remember(scope, "我住在成都")
    assert len(third.added) == 1
    assert len(third.superseded) == 1
    old = third.superseded[0]
    new = third.added[0]
    assert old.id == first.added[0].id
    assert old.valid_to is not None
    assert old.superseded_by == new.id
    assert norm(new.object) == norm("成都")

    location = next(block for block in third.profile if block.label == "location")
    assert "成都" in location.content
    assert "杭州" not in location.content

    active = svc.list_facts(scope)
    assert [fact.object for fact in active if fact.predicate == "lives_in"] == ["成都"]


def test_cold_path_supersedes_and_skips_assistant(svc: MemoryService, users):
    scope = users()
    svc.append_episode(scope, "user", "我住在杭州，我叫林夏。")
    svc.append_episode(scope, "assistant", "我住在月球。")
    svc.append_episode(scope, "user", "今天天气不错。")
    svc.append_episode(scope, "user", "我搬到了成都。")

    report = svc.process_pending(user_id=scope.user_id)
    assert report.episodes_seen == 4
    assert report.facts_superseded == 1

    facts = svc.list_facts(scope, include_inactive=True)
    assert not any("月球" in fact.content for fact in facts)
    active = [fact for fact in facts if fact.valid_to is None and fact.predicate == "lives_in"]
    inactive = [fact for fact in facts if fact.valid_to is not None and fact.predicate == "lives_in"]
    assert len(active) == 1 and active[0].object == "成都"
    assert len(inactive) == 1 and inactive[0].object == "杭州"
    assert inactive[0].superseded_by == active[0].id
    assert active[0].origin == "extracted"
    assert active[0].source_episode_ids

    episodes = svc.list_episodes(scope)
    assert len(episodes) == 4
    assert all(episode.extracted_at is not None for episode in episodes)
    name = next(fact for fact in facts if fact.predicate == "name")
    assert name.source_episode_ids
