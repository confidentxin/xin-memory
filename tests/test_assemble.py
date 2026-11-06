from datetime import datetime, timedelta, timezone
from uuid import uuid4

from memory_kernel.models import Episode, Fact, ProfileBlock, RecallHit
from memory_kernel.retrieve import allocate_budgets, assemble_prompt, keyword_relevance, rank_facts
from memory_kernel.util import estimate_tokens


def test_allocate_respects_caps():
    l2, l1, l0 = allocate_budgets(2500)
    assert l2 == 800
    assert l1 == 1200
    assert l0 > 0
    assert l2 + l1 + l0 <= 2500

    small_l2, small_l1, small_l0 = allocate_budgets(90)
    assert small_l2 + small_l1 + small_l0 <= 90
    assert small_l0 == 0


def test_keyword_match_beats_unrelated_fact():
    coffee = _fact(content="用户喜欢手冲咖啡", object="手冲咖啡", predicate="likes")
    tea = _fact(content="用户喜欢红茶", object="红茶", predicate="likes")
    assert keyword_relevance("手冲咖啡", coffee) > keyword_relevance("手冲咖啡", tea)


def test_rank_uses_importance_and_recency_when_relevance_ties():
    as_of = datetime(2026, 11, 1, tzinfo=timezone.utc)
    low = _fact(content="用户喜欢红茶", object="红茶", importance=0.2, valid_from=as_of)
    high = _fact(content="用户喜欢绿茶", object="绿茶", importance=0.9, valid_from=as_of)
    ranked = rank_facts("喜欢", [low, high], as_of=as_of)
    assert [hit.fact.id for hit in ranked] == [high.id, low.id]

    older = _fact(
        content="用户住在南京",
        object="南京",
        predicate="lives_in",
        importance=0.5,
        valid_from=as_of - timedelta(days=400),
        profile_label="location",
    )
    newer = _fact(
        content="用户住在苏州",
        object="苏州",
        predicate="lives_in",
        importance=0.5,
        valid_from=as_of - timedelta(days=1),
        profile_label="location",
    )
    cities = rank_facts("住在哪里", [older, newer], as_of=as_of)
    assert cities[0].fact.id == newer.id


def test_unused_profile_budget_falls_through_to_dialogue():
    as_of = datetime(2026, 11, 1, tzinfo=timezone.utc)
    profile = [
        ProfileBlock(
            id=uuid4(),
            user_id="u",
            agent_id="",
            label="location",
            content="- 用户住在成都",
            token_limit=200,
            version=1,
            updated_at=as_of,
        )
    ]
    episodes = [
        Episode(
            id=uuid4(),
            user_id="u",
            agent_id="",
            session_id="s",
            role="user",
            content="我下个月搬到成都。",
            occurred_at=as_of,
            ingested_at=as_of,
        )
    ]
    result = assemble_prompt(
        query="住在哪里",
        as_of=as_of,
        budget=800,
        profile=profile,
        hits=[RecallHit(score=1, relevance=1, fact=_fact(content="用户住在成都", object="成都", predicate="lives_in"))],
        episodes=episodes,
    )
    assert "【近期对话 L0】" in result.prompt
    assert result.tokens["total"] <= 800


def test_assemble_stays_inside_budget_and_prefers_profile():
    as_of = datetime(2026, 11, 1, tzinfo=timezone.utc)
    profile = [
        ProfileBlock(
            id=uuid4(),
            user_id="u",
            agent_id="",
            label="identity",
            content="- 用户的名字是林夏\n" + ("画像" * 500),
            token_limit=40,
            version=1,
            updated_at=as_of,
        )
    ]
    hits = [
        RecallHit(score=1, relevance=1, fact=_fact(content="事实" * 80, object="事实"))
        for _ in range(6)
    ]
    episodes = [
        Episode(
            id=uuid4(),
            user_id="u",
            agent_id="",
            session_id="s",
            role="user",
            content="对话" * 40,
            occurred_at=as_of,
            ingested_at=as_of,
        )
    ]
    result = assemble_prompt(query="林夏", as_of=as_of, budget=180, profile=profile, hits=hits, episodes=episodes)
    assert result.tokens["total"] <= 180
    assert result.prompt.startswith("【用户画像 L2】")
    assert estimate_tokens(result.prompt) == result.tokens["total"]


def _fact(**overrides) -> Fact:
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    payload = dict(
        id=uuid4(),
        user_id="u",
        agent_id="",
        subject="用户",
        predicate="likes",
        object="茶",
        content="用户喜欢茶",
        valid_from=now,
        origin="explicit",
        importance=0.5,
        metadata={"profile_label": overrides.pop("profile_label", "preferences")},
        created_at=now,
    )
    payload.update(overrides)
    return Fact(**payload)
