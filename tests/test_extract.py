from memory_kernel.extract import extract_mock
from memory_kernel.models import FactDraft
from memory_kernel.reconcile import decide_fact, sanitize_draft
from memory_kernel.util import norm


def test_mock_extracts_demo_utterances():
    intro = extract_mock("你好，我叫林夏，住在杭州，在星澜科技做产品经理。")
    predicates = {draft.predicate: draft.object for draft in intro}
    assert predicates["name"] == "林夏"
    assert predicates["lives_in"] == "杭州"
    assert predicates["works_at"] == "星澜科技"
    assert predicates["occupation"] == "产品经理"

    prefs = extract_mock("我喜欢清晨跑步，咖啡只喝美式。")
    assert {draft.predicate for draft in prefs} >= {"likes", "drinks"}
    assert any(draft.object == "清晨跑步" for draft in prefs)
    assert any(draft.object == "美式" for draft in prefs)

    move = extract_mock("我下个月搬到成都。")
    assert len(move) == 1
    assert move[0].predicate == "lives_in"
    assert move[0].object == "成都"

    allergy = extract_mock("林夏对花生过敏")
    assert allergy[0].predicate == "allergy"
    assert allergy[0].object == "花生"


def test_openai_payload_drops_empty_items_and_bad_importance():
    from memory_kernel.extract import drafts_from_payload

    drafts = drafts_from_payload(
        {
            "facts": [
                {
                    "subject": "用户",
                    "predicate": "lives_in",
                    "object": "成都",
                    "content": "用户住在成都",
                    "importance": "high",
                    "profile_label": "location",
                    "entities": [{"name": "成都", "kind": "place"}],
                },
                {"content": ""},
            ]
        }
    )
    assert len(drafts) == 1
    assert drafts[0].predicate == "lives_in"
    assert drafts[0].importance == 0.5
    assert drafts[0].entities[0].kind == "place"


def test_mock_ignores_smalltalk_and_parses_english():
    assert extract_mock("你好") == []
    assert extract_mock("今天天气不错") == []
    english = extract_mock("My name is Lin Xia and I live in Chengdu.")
    assert {draft.predicate for draft in english} >= {"name", "lives_in"}


def test_norm_collapses_admin_suffix():
    assert norm("杭州市") == norm("杭州")
    assert norm("  Chengdu ") == "chengdu"


def test_decide_skip_duplicate_and_supersede_singleton():
    current = sanitize_draft(
        FactDraft(subject="用户", predicate="lives_in", object="杭州", content="用户住在杭州", profile_label="location")
    )
    existing = _bare_fact(current)
    assert decide_fact(current, [existing]) == "skip"
    moved = current.model_copy(update={"object": "成都", "content": "用户住在成都"})
    assert decide_fact(moved, [existing]) == "supersede"
    like = sanitize_draft(
        FactDraft(subject="用户", predicate="likes", object="跑步", content="用户喜欢跑步", profile_label="preferences")
    )
    other = sanitize_draft(
        FactDraft(subject="用户", predicate="likes", object="美式", content="用户喜欢美式", profile_label="preferences")
    )
    assert decide_fact(other, [_bare_fact(like)]) == "add"


def _bare_fact(draft: FactDraft):
    from datetime import datetime, timezone
    from uuid import uuid4

    from memory_kernel.models import Fact

    now = datetime.now(timezone.utc)
    return Fact(
        id=uuid4(),
        user_id="u",
        agent_id="",
        subject=draft.subject,
        predicate=draft.predicate,
        object=draft.object,
        content=draft.content,
        valid_from=now,
        origin="explicit",
        importance=draft.importance,
        metadata={"profile_label": draft.profile_label},
        created_at=now,
    )
