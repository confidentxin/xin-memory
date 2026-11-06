from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID

from memory_kernel.models import Episode, Fact, ProfileBlock, RecallHit, RecallResult
from memory_kernel.reconcile import MANAGED_LABELS, label_for_fact
from memory_kernel.util import ensure_utc, estimate_tokens, norm, truncate_to_tokens

L2_CAP = 800
L1_CAP = 1200
HALF_LIFE_DAYS = 45.0

# Predicates that live in L1 but must never be rendered into the L2 profile.
PROFILE_EXCLUDED_PREDICATES = {"session_summary"}

# Query hints pull the right predicate family into the candidate set even when
# the fact text does not share a long substring (住在哪 -> lives_in).
_HINTS: list[tuple[re.Pattern[str], set[str]]] = [
    (re.compile(r"住|搬家|搬到|哪里|哪儿|哪个城市"), {"lives_in"}),
    (re.compile(r"名字|叫什么|是谁"), {"name"}),
    (re.compile(r"工作|公司|上班|职业|做什么"), {"works_at", "occupation", "title"}),
    (re.compile(r"喜欢|偏好|爱好|口味"), {"likes", "dislikes", "drinks", "allergy"}),
    (re.compile(r"过敏"), {"allergy"}),
    (re.compile(r"咖啡|喝什么"), {"drinks"}),
    (re.compile(r"\blive\b|\bcity\b", re.I), {"lives_in"}),
    (re.compile(r"\bname\b", re.I), {"name"}),
    (re.compile(r"\bwork\b|\bjob\b", re.I), {"works_at", "occupation"}),
    (re.compile(r"\blike\b|\bprefer\b", re.I), {"likes", "dislikes"}),
]


def hinted_predicates(query: str) -> set[str]:
    found: set[str] = set()
    for pattern, predicates in _HINTS:
        if pattern.search(query):
            found |= predicates
    return found


def search_terms(query: str) -> list[str]:
    chunks = re.findall(r"[\u4e00-\u9fff]{2,8}|[A-Za-z0-9][A-Za-z0-9_+.-]{1,}", query)
    chars = re.findall(r"[\u4e00-\u9fff]", query)
    bigrams = [a + b for a, b in zip(chars, chars[1:])]
    terms: list[str] = []
    seen: set[str] = set()
    for term in bigrams + chunks:
        term = term.strip()
        if len(term) < 2 or term in seen:
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) >= 24:
            break
    return terms


def _bigrams(text: str) -> set[str]:
    chars = re.findall(r"[\u4e00-\u9fff]", text)
    if len(chars) < 2:
        return set(chars)
    return {a + b for a, b in zip(chars, chars[1:])}


def keyword_relevance(query: str, fact: Fact) -> float:
    """Lexical score in [0, 1]. Chinese uses character bigram overlap; English uses tokens."""
    query = query.strip()
    if not query:
        return 0.0
    hay = f"{fact.subject} {fact.predicate} {fact.object} {fact.content}"
    score = 0.0
    query_bi = _bigrams(query)
    hay_bi = _bigrams(hay)
    if query_bi and hay_bi:
        score = max(score, len(query_bi & hay_bi) / len(query_bi))
    query_tokens = set(re.findall(r"[a-z0-9]{2,}", query.lower()))
    hay_tokens = set(re.findall(r"[a-z0-9]{2,}", hay.lower()))
    if query_tokens and hay_tokens:
        score = max(score, len(query_tokens & hay_tokens) / len(query_tokens))
    obj = norm(fact.object)
    if obj and obj in norm(query):
        score = min(1.0, score + 0.35)
    if fact.predicate in hinted_predicates(query):
        score = min(1.0, score + 0.25)
    return score


def time_decay(valid_from: datetime, as_of: datetime, half_life_days: float = HALF_LIFE_DAYS) -> float:
    delta_days = (ensure_utc(as_of) - ensure_utc(valid_from)).total_seconds() / 86400
    if delta_days < 0:
        delta_days = 0
    return 0.5 ** (delta_days / half_life_days)


def final_score(relevance: float, importance: float, valid_from: datetime, as_of: datetime) -> float:
    decay = time_decay(valid_from, as_of)
    return relevance * (0.55 + 0.45 * importance) * (0.65 + 0.35 * decay)


def rrf(ranked_lists: list[list[UUID]], k: int = 60) -> dict[UUID, float]:
    scores: dict[UUID, float] = {}
    for ranked in ranked_lists:
        for index, fact_id in enumerate(ranked, start=1):
            scores[fact_id] = scores.get(fact_id, 0.0) + 1.0 / (k + index)
    return scores


def _blend(keyword: float, vector: float | None, rrf_norm: float | None) -> float:
    if vector is None:
        relevance = keyword
    elif keyword <= 0:
        relevance = min(1.0, vector * 0.9)
    else:
        # Items retrieved by both lists get a small agreement bonus (RRF's intent).
        relevance = min(1.0, 0.5 * keyword + 0.5 * vector + 0.1)
    if rrf_norm:
        relevance = min(1.0, 0.75 * relevance + 0.25 * rrf_norm)
    return relevance


def rank_facts(
    query: str,
    facts: list[Fact],
    *,
    as_of: datetime,
    vector_similarity: dict[UUID, float] | None = None,
) -> list[RecallHit]:
    """Rank facts for a query.

    Keyword overlap (and optional cosine similarity) is the relevance term.
    When both retrievers return hits, reciprocal rank fusion is mixed in as 25%
    of relevance so agreement changes order without flattening the keyword score.
    Importance and exponential time decay then rescale that relevance.
    An empty query keeps every candidate and ranks by importance and recency.
    """
    vector_similarity = vector_similarity or {}
    keyword_scores: dict[UUID, float] = {}
    for fact in facts:
        if not query.strip():
            keyword_scores[fact.id] = 1.0
        else:
            keyword_scores[fact.id] = keyword_relevance(query, fact)

    keyword_order = [
        fact_id
        for fact_id, score in sorted(keyword_scores.items(), key=lambda item: item[1], reverse=True)
        if score > 0
    ]
    vector_order = [
        fact_id
        for fact_id, _score in sorted(vector_similarity.items(), key=lambda item: item[1], reverse=True)
    ]
    rrf_norm: dict[UUID, float] = {}
    if keyword_order and vector_order:
        fused = rrf([keyword_order, vector_order])
        peak = max(fused.values()) if fused else 1.0
        rrf_norm = {fact_id: value / peak for fact_id, value in fused.items()}

    hits: list[RecallHit] = []
    for fact in facts:
        keyword = keyword_scores.get(fact.id, 0.0)
        vector = vector_similarity.get(fact.id)
        if query.strip() and keyword <= 0 and vector is None:
            continue
        relevance = 1.0 if not query.strip() else _blend(keyword, vector, rrf_norm.get(fact.id))
        if relevance <= 0:
            continue
        score = final_score(relevance, fact.importance, fact.valid_from, as_of)
        hits.append(RecallHit(score=score, relevance=relevance, fact=fact))
    hits.sort(key=lambda hit: (hit.score, hit.fact.importance, hit.fact.valid_from), reverse=True)
    return hits


def allocate_budgets(total: int) -> tuple[int, int, int]:
    """Maximum tokens for L2, L1, and L0 when each earlier layer is full.

    Caps are L2 <= 800 and L1 <= 1200. Assembly does not reserve an unused
    slice: tokens a layer does not spend fall through to the next one.
    """
    if total <= 0:
        return (0, 0, 0)
    l2 = min(L2_CAP, total)
    l1 = min(L1_CAP, max(0, total - l2))
    l0 = max(0, total - l2 - l1)
    return l2, l1, l0


def _pack(title: str, chunks: list[str], quota: int) -> tuple[str, int, int]:
    if quota <= 0 or not chunks:
        return "", 0, 0
    chosen: list[str] = []
    for chunk in chunks:
        proposed = chosen + [chunk]
        text = title + "\n" + "\n".join(proposed)
        if estimate_tokens(text) <= quota:
            chosen = proposed
            continue
        if not chosen:
            room = quota - estimate_tokens(title + "\n")
            snippet = truncate_to_tokens(chunk, room)
            if snippet and estimate_tokens(title + "\n" + snippet) <= quota:
                chosen = [snippet]
        break
    if not chosen:
        return "", 0, 0
    text = title + "\n" + "\n".join(chosen)
    return text, estimate_tokens(text), len(chosen)


def _ordered_blocks(blocks: list[ProfileBlock]) -> list[ProfileBlock]:
    order = {label: index for index, label in enumerate(MANAGED_LABELS)}
    return sorted(blocks, key=lambda block: (order.get(block.label, 99), block.label))


def render_profile_from_facts(facts: list[Fact], token_limit: int = 200) -> dict[str, str]:
    grouped: dict[str, list[Fact]] = {label: [] for label in MANAGED_LABELS}
    for fact in facts:
        # Session digests are recall material for L1, not a trait of the person.
        if fact.predicate in PROFILE_EXCLUDED_PREDICATES:
            continue
        label = label_for_fact(fact)
        if label not in grouped:
            label = "notes"
        grouped[label].append(fact)
    rendered: dict[str, str] = {}
    for label, items in grouped.items():
        if not items:
            continue
        items.sort(key=lambda fact: (fact.importance, fact.valid_from), reverse=True)
        content = "\n".join(f"- {fact.content}" for fact in items)
        rendered[label] = truncate_to_tokens(content, token_limit)
    return rendered


def _profile_chunks(blocks: list[ProfileBlock]) -> list[str]:
    chunks = []
    for block in _ordered_blocks(blocks):
        content = truncate_to_tokens(block.content.strip(), block.token_limit)
        if content:
            chunks.append(f"## {block.label}\n{content}")
    return chunks


def _select_episodes(episodes_desc: list[Episode], quota: int) -> list[Episode]:
    if quota <= 0 or not episodes_desc:
        return []
    chosen: list[Episode] = []
    title = "【近期对话 L0】"
    for episode in episodes_desc:
        proposed = sorted(chosen + [episode], key=lambda item: item.occurred_at)
        lines = [f"{item.role}: {item.content}" for item in proposed]
        text = title + "\n" + "\n".join(lines)
        if estimate_tokens(text) <= quota:
            chosen.append(episode)
            continue
        if not chosen:
            line = f"{episode.role}: {episode.content}"
            _text, tokens, kept = _pack(title, [line], quota)
            if kept:
                return [episode]
        break
    chosen.sort(key=lambda item: item.occurred_at)
    return chosen


def assemble_prompt(
    *,
    query: str,
    as_of: datetime,
    budget: int,
    profile: list[ProfileBlock],
    hits: list[RecallHit],
    episodes: list[Episode],
) -> RecallResult:
    separator = estimate_tokens("\n\n")
    l2_cap, _, _ = allocate_budgets(budget)
    # When the profile could fill the whole budget, keep a slice for the top fact.
    l1_floor = 48 if budget >= 160 else 0
    l2_quota = min(l2_cap, max(0, budget - l1_floor))
    l2_text, l2_tokens, _l2_kept = _pack("【用户画像 L2】", _profile_chunks(profile), l2_quota)
    used = l2_tokens + (separator if l2_text else 0)
    l1_quota = min(L1_CAP, max(0, budget - used))
    fact_lines = [f"- {hit.fact.content}" for hit in hits]
    l1_text, l1_tokens, l1_kept = _pack("【相关事实 L1】", fact_lines, l1_quota)
    kept_hits = hits[:l1_kept] if l1_text else []
    used = l2_tokens + (separator if l2_text else 0) + l1_tokens + (separator if l1_text else 0)
    l0_quota = max(0, budget - used)
    selected_episodes = _select_episodes(episodes, l0_quota)
    episode_lines = [f"{episode.role}: {episode.content}" for episode in selected_episodes]
    l0_text, l0_tokens, l0_kept = _pack("【近期对话 L0】", episode_lines, l0_quota)
    if l0_kept < len(selected_episodes):
        selected_episodes = selected_episodes[:l0_kept]
    parts = [part for part in (l2_text, l1_text, l0_text) if part]
    prompt = "\n\n".join(parts)
    if estimate_tokens(prompt) > budget:
        parts = [part for part in (l2_text, l1_text) if part]
        prompt = "\n\n".join(parts)
        l0_text, l0_tokens = "", 0
        selected_episodes = []
    if estimate_tokens(prompt) > budget:
        prompt = truncate_to_tokens(l2_text, budget)
        l1_text = ""
        kept_hits = []
        l0_text, l0_tokens = "", 0
        selected_episodes = []
    return RecallResult(
        query=query,
        as_of=as_of,
        budget=budget,
        tokens={
            "l2": estimate_tokens(l2_text) if l2_text and l2_text in prompt else 0,
            "l1": estimate_tokens(l1_text) if l1_text and "【相关事实 L1】" in prompt else 0,
            "l0": l0_tokens if l0_text and "【近期对话 L0】" in prompt else 0,
            "total": estimate_tokens(prompt),
        },
        profile=profile,
        hits=kept_hits,
        episodes=selected_episodes,
        prompt=prompt,
    )
