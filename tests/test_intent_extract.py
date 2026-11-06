from datetime import datetime, timezone

from memory_kernel.agenda import cron_matches, extract_intent_drafts, policy_for


def _assert_local_nine(value: datetime) -> None:
    """Due times are 09:00 in the machine's local zone, stored as UTC."""
    local = value.astimezone()
    assert (local.hour, local.minute) == (9, 0)


def test_deadline_and_explicit_phrases():
    wednesday = datetime(2026, 11, 4, 15, tzinfo=timezone.utc)
    deadline = extract_intent_drafts("周五前提交方案", wednesday)
    assert len(deadline) == 1
    assert deadline[0].title == "提交方案"
    assert deadline[0].explicit is False
    _assert_local_nine(deadline[0].due_at)

    explicit = extract_intent_drafts("提醒我周五前把方案交了", wednesday)
    assert explicit[0].explicit is True
    assert explicit[0].title == "方案交了"
    _assert_local_nine(explicit[0].due_at)

    watch = extract_intent_drafts("帮我盯着合同审批")
    assert watch[0].title == "合同审批"
    assert watch[0].due_at is None

    english = extract_intent_drafts("Please remind me to send the draft")
    assert english[0].title == "send the draft"
    assert english[0].explicit is True


def test_smalltalk_is_not_an_intent():
    assert extract_intent_drafts("我住在杭州，我叫林夏。") == []
    assert extract_intent_drafts("今天天气不错") == []


def test_policy_and_cron():
    assert policy_for("提交方案") == "notify"
    assert policy_for("给供应商转账") == "propose"
    # mode wins over the risk heuristic: act is an explicit opt-in.
    assert policy_for("给供应商转账", mode="act") == "act"
    assert policy_for("提交方案", mode="act") == "act"
    # Cron fields use the local wall clock, so build the moment in local time.
    monday = datetime(2026, 11, 2, 9, 30).astimezone()
    assert cron_matches("30 9 * * 1", monday) is True
    assert cron_matches("31 9 * * 1", monday) is False
    assert cron_matches("not a cron", monday) is False
