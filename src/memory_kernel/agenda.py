"""Turn utterances into intents, and decide whether a due item may run by itself."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from memory_kernel.models import IntentDraft
from memory_kernel.util import ensure_utc

_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

_EXPLICIT = re.compile(r"(?:提醒我|帮我盯着|记得提醒我|别忘了)\s*[:：]?\s*(.+)")
_ENGLISH = re.compile(r"\bremind me(?: to)?\s+(.+)", re.I)
_DEADLINE = re.compile(
    r"(?:周|星期)[一二三四五六日天]\s*(?:之前|以前|前)\s*(?:把|将)?\s*(.+)"
)
_LEADING_WHEN = re.compile(
    r"^(?:(?:周|星期)[一二三四五六日天]\s*(?:之前|以前|前)|(?:后天|明天|今天)\s*(?:之前|以前|前)?)\s*(?:把|将)?"
)
_RISK = re.compile(
    r"转账|汇款|付款|支付|删除|清空|发邮件|发送邮件|下单|购买|买下|汇出|"
    r"transfer|delete|\bpay\b|send email",
    re.I,
)


def clean_title(text: str) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    return collapsed.strip("，,。！!？?；;：:、").strip()[:120]


def _nine_local(day: date) -> datetime:
    """09:00 on `day` in the machine's local zone, returned as UTC.

    The old code used 09:00 UTC, which is 17:00 in CST — every reminder parsed
    from speech fired eight hours late.
    """
    zone = datetime.now().astimezone().tzinfo or timezone.utc
    return datetime(day.year, day.month, day.day, 9, 0, tzinfo=zone).astimezone(timezone.utc)


def parse_due(text: str, now: datetime) -> datetime | None:
    moment = ensure_utc(now)
    match = re.search(r"(?:周|星期)([一二三四五六日天])", text)
    if match:
        target = _WEEKDAY[match.group(1)]
        delta = (target - moment.weekday()) % 7
        return _nine_local((moment + timedelta(days=delta)).date())
    for word, days in (("后天", 2), ("明天", 1), ("今天", 0)):
        if word in text:
            return _nine_local((moment + timedelta(days=days)).date())
    return None


def split_task(fragment: str, now: datetime) -> tuple[str, datetime | None]:
    due = parse_due(fragment, now)
    title = _LEADING_WHEN.sub("", fragment.strip())
    title = clean_title(title) or clean_title(fragment) or "待办"
    return title, due


def extract_intent_drafts(text: str, now: datetime | None = None) -> list[IntentDraft]:
    """Heuristic intents. Explicit reminders are flagged; deadlines are cold-path only."""
    moment = ensure_utc(now)
    found: list[IntentDraft] = []
    seen: set[str] = set()

    def push(fragment: str, *, explicit: bool, priority: int) -> None:
        title, due = split_task(fragment, moment)
        key = title.casefold()
        if not title or key in seen:
            return
        seen.add(key)
        found.append(
            IntentDraft(
                title=title,
                description=text.strip()[:2000],
                due_at=due,
                priority=priority,
                explicit=explicit,
            )
        )

    for pattern in (_EXPLICIT, _ENGLISH):
        for match in pattern.finditer(text):
            push(match.group(1), explicit=True, priority=1)
    if not found:
        for match in _DEADLINE.finditer(text):
            push(match.group(0), explicit=False, priority=1)
    return found


def policy_for(title: str, description: str = "", mode: str | None = None) -> str:
    """Decide what a due intent may do by itself.

    notify  — surface it; a person decides.
    propose — high-risk wording; record the proposal only.
    act     — opt-in autonomous execution. Set per intent via mode="act"; a
              risk hit does NOT downgrade it, because act is the explicit intent.
              The emergency stop lives in MemoryService._act_halted().
    """
    chosen = (mode or "").strip().lower()
    if chosen in {"act", "notify", "propose"}:
        return chosen
    if _RISK.search(f"{title}\n{description}"):
        return "propose"
    return "notify"


def proposed_action_text(title: str) -> str:
    return f"执行：{title}"


def valid_cron(expression: str) -> bool:
    """5 fields of * or a single in-range integer, matching cron_matches' limits."""
    parts = expression.split()
    if len(parts) != 5:
        return False
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    for field, (low, high) in zip(parts, bounds, strict=True):
        if field == "*":
            continue
        if not field.isdigit():
            return False
        if not low <= int(field) <= high:
            return False
    return True


def last_occurrence_today(expression: str, moment: datetime) -> datetime | None:
    """Latest time today (<= moment, local clock) that the expression matches.

    cron only matches its exact minute, so a machine that was asleep or off at
    10:00 would never run a daily job. This lets tick catch up once per day.
    """
    now = ensure_utc(moment).astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cursor = now.replace(second=0, microsecond=0)
    while cursor >= start:
        if cron_matches(expression, cursor):
            return cursor
        cursor -= timedelta(minutes=1)
    return None


def cron_matches(expression: str, moment: datetime) -> bool:
    """Match a 5-field cron (minute hour dom month dow) of stars and integers.

    Fields are evaluated on the machine's local wall clock, so "0 9 * * *" means
    09:00 local rather than 09:00 UTC (which is 17:00 in CST).
    dow uses cron numbering: 0 or 7 is Sunday, 1 is Monday. Lists, ranges, and
    steps are not supported.
    """
    parts = expression.split()
    if len(parts) != 5:
        return False
    when = ensure_utc(moment).astimezone()
    cron_dow = (when.weekday() + 1) % 7
    expected = [when.minute, when.hour, when.day, when.month, cron_dow]
    for index, (field, value) in enumerate(zip(parts, expected, strict=True)):
        if field == "*":
            continue
        if not field.isdigit():
            return False
        number = int(field)
        if index == 4 and number == 7:
            number = 0
        if number != value:
            return False
    return True


def _act_heading(title: str) -> str:
    """Prefix the title once, even when the task already says 自动执行：."""
    for marker in ("自动执行：", "自动执行:"):
        if title.startswith(marker):
            title = title[len(marker):].lstrip()
            break
    return f"自动执行：{title}"


def render_action_episode(*, title: str, due_at: datetime | None, policy: str, context: str, proposed: str | None) -> str:
    due = due_at.isoformat() if due_at else "无"
    if policy == "propose":
        head = (
            f"待确认：{title}\n"
            f"提议动作：{proposed}\n"
            "这是高风险动作，我只记下提议，没有自动执行。"
        )
    elif policy == "act":
        head = f"{_act_heading(title)}\n触发时间：{due}"
    else:
        head = f"提醒：{title}\n截止：{due}"
    body = context.strip()
    if not body:
        return head
    return f"{head}\n\n{body}"
