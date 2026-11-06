"""End-to-end demo: dialogue → L0, cold extraction → L1/L2, explicit remember, recall."""

from __future__ import annotations

import sys

from memory_kernel.config import load_settings
from memory_kernel.models import Scope
from memory_kernel.service import MemoryService
from memory_kernel.util import norm, redact_dsn

DEMO_USER = "demo-linxia"
OTHER_USER = "demo-other"


def run_demo() -> None:
    settings = load_settings()
    service = MemoryService(settings)
    print(f"数据库 {redact_dsn(settings.database_url)}")
    print(f"抽取器 {service.extractor.name}（嵌入 {service.embedder.name}）")
    service.migrate()
    service.purge_user(DEMO_USER)
    service.purge_user(OTHER_USER)

    scope = Scope(user_id=DEMO_USER, session_id="sess-demo-1")
    turns = [
        ("user", "你好，我叫林夏，住在杭州，在星澜科技做产品经理。"),
        ("assistant", "你好林夏，杭州最近适合出门走走。"),
        ("user", "我喜欢清晨跑步，咖啡只喝美式。"),
        ("assistant", "记下了：晨跑，还有美式。"),
        ("user", "我下个月搬到成都。"),
        ("assistant", "成都很安逸，搬家前我帮你记着偏好。"),
    ]
    print("\n== 写入 L0 ==")
    for role, content in turns:
        episode = service.append_episode(scope, role, content)
        print(f"  {role}: {content}  [{episode.id}]")

    print("\n== 热路径 remember ==")
    remembered = service.remember(scope, "林夏对花生过敏", importance=0.95)
    for fact in remembered.added:
        print(f"  + [{fact.origin}] {fact.predicate}={fact.object}  {fact.content}")

    print("\n== 冷路径抽取 ==")
    report = service.process_pending(user_id=DEMO_USER)
    print(
        f"  episodes={report.episodes_seen} added={report.facts_added} "
        f"superseded={report.facts_superseded} skipped={report.facts_skipped}"
    )

    facts = service.list_facts(scope, include_inactive=True)
    print("\n== L1 事实 ==")
    for fact in sorted(facts, key=lambda item: (item.predicate, item.valid_from)):
        state = "有效" if fact.valid_to is None else f"失效→{fact.superseded_by}"
        print(f"  [{fact.origin}] {fact.predicate}={fact.object} | {fact.content} | {state}")

    profile = service.get_profile(scope)
    print("\n== L2 画像 ==")
    for block in profile:
        print(f"  ## {block.label} v{block.version}\n{block.content}")

    entities = service.list_entities(DEMO_USER)
    print("\n== entities ==")
    for entity in entities:
        print(f"  {entity.name} ({entity.kind})")

    print("\n== recall ==")
    result = service.recall(scope, "林夏现在住在哪，有什么偏好？", budget=2500)
    print(result.prompt)
    print(f"\n  tokens={result.tokens}")

    other = service.recall(Scope(user_id=OTHER_USER, session_id="sess-other"), "林夏现在住在哪，有什么偏好？")
    _expect(not other.hits, "其他用户不应召回到林夏的事实")
    _expect("林夏" not in other.prompt, "其他用户的 prompt 不应包含林夏")

    active_city = [fact for fact in facts if fact.predicate == "lives_in" and fact.valid_to is None]
    old_city = [fact for fact in facts if fact.predicate == "lives_in" and fact.valid_to is not None]
    _expect(len(active_city) == 1 and norm(active_city[0].object) == norm("成都"), "当前居住地应为成都")
    _expect(len(old_city) == 1 and norm(old_city[0].object) == norm("杭州"), "杭州应被失效")
    _expect(old_city[0].superseded_by == active_city[0].id, "杭州.superseded_by 应指向成都")
    _expect(any(fact.predicate == "name" and fact.object == "林夏" for fact in facts), "应抽到姓名")
    _expect(any(fact.predicate == "works_at" and "星澜" in fact.object for fact in facts), "应抽到雇主")
    _expect(any(fact.predicate == "allergy" and fact.origin == "explicit" for fact in facts), "过敏应为显式 remember")
    location = next(block for block in profile if block.label == "location")
    identity = next(block for block in profile if block.label == "identity")
    preferences = next(block for block in profile if block.label == "preferences")
    _expect("成都" in location.content and "杭州" not in location.content, "L2 居住地只保留成都")
    _expect("林夏" in identity.content, "L2 应包含姓名")
    _expect("花生" in preferences.content, "L2 应包含过敏")
    _expect("【用户画像 L2】" in result.prompt, "prompt 应包含 L2")
    _expect("【相关事实 L1】" in result.prompt, "prompt 应包含 L1")
    _expect("【近期对话 L0】" in result.prompt, "prompt 应包含近期 L0")
    _expect("成都" in result.prompt and "林夏" in result.prompt and "花生" in result.prompt, "prompt 应覆盖画像与事实")
    hit_text = "\n".join(hit.fact.content for hit in result.hits)
    _expect("杭州" not in hit_text, "失效的杭州不应进入 L1 命中")
    _expect(result.tokens["total"] <= result.budget, "组装结果不应超过 token 预算")
    episodes = service.list_episodes(scope)
    _expect(episodes and all(episode.extracted_at is not None for episode in episodes), "demo 对话应全部标记已抽取")
    _expect(any(entity.name == "林夏" for entity in entities), "应写入人物实体")
    _expect(any(entity.name == "成都" and entity.kind == "place" for entity in entities), "应写入地点实体")

    print("\nDEMO OK")


def _expect(condition: bool, message: str) -> None:
    if not condition:
        print(f"DEMO 失败: {message}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    run_demo()
