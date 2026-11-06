from __future__ import annotations

import json
import logging
import re
from typing import Protocol

import httpx

from memory_kernel.config import Settings
from memory_kernel.models import EntityDraft, FactDraft
from memory_kernel.reconcile import sanitize_draft
from memory_kernel.util import is_cjk_text, norm

log = logging.getLogger(__name__)

_MAX_FACTS = 12

_EXTRACT_SYSTEM = """你是记忆抽取器。从一条用户发言中抽取稳定、原子的个人事实。
只输出 JSON 对象，格式如下：
{"facts":[{"subject":"用户","predicate":"lives_in","object":"成都","content":"用户住在成都","importance":0.8,"profile_label":"location","entities":[{"name":"成都","kind":"place"}]}]}
规则：
- predicate 用英文蛇形：name, lives_in, works_at, occupation, likes, dislikes, drinks, allergy, age, note。
- 姓名、居住地、雇主、职业是单值；喜好、过敏、备注可以多条。
- subject 用「用户」。content 用一句完整中文（英文发言则用英文）。
- importance 取 0 到 1。profile_label 只能是 identity, location, work, preferences, notes。
- 没有稳定个人信息时返回 {"facts":[]}。不要臆造，不要复述寒暄。
"""


class Extractor(Protocol):
    name: str

    def extract(self, text: str) -> list[FactDraft]: ...

    def summarize(self, transcript: str) -> str: ...


class Rule:
    def __init__(
        self,
        pattern: str,
        predicate: str,
        label: str,
        importance: float,
        template: str,
        *,
        flags: int = 0,
    ) -> None:
        self.pattern = re.compile(pattern, flags)
        self.predicate = predicate
        self.label = label
        self.importance = importance
        self.template = template


_ZH_RULES = [
    Rule(r"我(?:的名字)?叫\s*([^\s，,。；;！!？?]{1,32})", "name", "identity", 0.9, "用户的名字是{0}"),
    Rule(r"我住在\s*([^\s，,。；;！!？?]{1,32})", "lives_in", "location", 0.82, "用户住在{0}"),
    Rule(r"(?:^|[，,。\s])住在\s*([^\s，,。；;！!？?]{1,32})", "lives_in", "location", 0.8, "用户住在{0}"),
    Rule(r"搬到了?\s*([^\s，,。；;！!？?]{1,32})", "lives_in", "location", 0.86, "用户住在{0}"),
    Rule(r"我(?:现在)?在\s*([^\s，,。；;！!？?]{2,32})(?:工作|上班)", "works_at", "work", 0.75, "用户在{0}工作"),
    Rule(r"我喜欢\s*([^，,。；;！!？?\n]{1,40})", "likes", "preferences", 0.62, "用户喜欢{0}"),
    Rule(r"我(?:不喜欢|讨厌)\s*([^，,。；;！!？?\n]{1,40})", "dislikes", "preferences", 0.62, "用户不喜欢{0}"),
    Rule(r"咖啡只喝\s*([^\s，,。；;！!？?]{1,20})", "drinks", "preferences", 0.6, "用户喝{0}"),
    Rule(r"对\s*([^\s，,。；;！!？?]{1,24})过敏", "allergy", "preferences", 0.9, "用户对{0}过敏"),
]

_EN_RULES = [
    Rule(r"\bmy name is ([A-Za-z][A-Za-z .'-]{0,40})", "name", "identity", 0.9, "User's name is {0}", flags=re.I),
    Rule(r"\bi live in ([A-Za-z][A-Za-z .'-]{0,40})", "lives_in", "location", 0.8, "User lives in {0}", flags=re.I),
    Rule(r"\bi work at ([A-Za-z0-9][A-Za-z0-9 .&'-]{0,40})", "works_at", "work", 0.75, "User works at {0}", flags=re.I),
    Rule(r"\bi (?:like|love) ([A-Za-z0-9][A-Za-z0-9 .'-]{0,40})", "likes", "preferences", 0.6, "User likes {0}", flags=re.I),
    Rule(r"\bi am allergic to ([A-Za-z0-9][A-Za-z0-9 .'-]{0,40})", "allergy", "preferences", 0.9, "User is allergic to {0}", flags=re.I),
]

_WORK_ROLE = re.compile(
    r"在\s*([^\s，,。；;！!？?]{2,30}?)做\s*([^\s，,。；;！!？?]{2,16})"
)


def extract_mock(text: str) -> list[FactDraft]:
    """Deterministic extractor used when no API key is configured, and as a fallback."""
    found: list[tuple[int, int, FactDraft]] = []
    cjk = is_cjk_text(text)
    subject = "用户" if cjk else "user"
    order = 0

    def push(pos: int, predicate: str, label: str, importance: float, template: str, obj: str) -> None:
        nonlocal order
        obj = obj.strip().strip("\"'“”‘’")
        obj = re.sub(r"[\s，,。；;！!？?]+$", "", obj).strip()
        if not obj:
            return
        found.append(
            (
                pos,
                order,
                FactDraft(
                    subject=subject,
                    predicate=predicate,
                    object=obj,
                    content=template.format(obj),
                    importance=importance,
                    profile_label=label,
                ),
            )
        )
        order += 1

    rules = _ZH_RULES if cjk else _EN_RULES
    # Run both when a message mixes languages.
    if cjk:
        rules = _ZH_RULES + _EN_RULES
    for rule in rules:
        for match in rule.pattern.finditer(text):
            push(match.start(), rule.predicate, rule.label, rule.importance, rule.template, match.group(1))

    if cjk:
        for match in _WORK_ROLE.finditer(text):
            push(match.start(), "works_at", "work", 0.76, "用户在{0}工作", match.group(1))
            push(match.start(), "occupation", "work", 0.7, "用户的职业是{0}", match.group(2))

    found.sort(key=lambda item: (item[0], item[1]))
    drafts: list[FactDraft] = []
    seen: set[tuple[str, str]] = set()
    for _, _, draft in found:
        clean = sanitize_draft(draft)
        key = (clean.predicate, norm(clean.object))
        if key in seen:
            continue
        seen.add(key)
        drafts.append(clean)
        if len(drafts) >= _MAX_FACTS:
            break
    return drafts


def drafts_from_payload(data: dict) -> list[FactDraft]:
    raw_facts = data.get("facts") or []
    if not isinstance(raw_facts, list):
        raise ValueError("facts 必须是数组")
    drafts: list[FactDraft] = []
    for item in raw_facts:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        obj = str(item.get("object") or "").strip()
        if not content and not obj:
            continue
        entities: list[EntityDraft] = []
        for ent in item.get("entities") or []:
            if isinstance(ent, dict) and str(ent.get("name") or "").strip():
                kind = str(ent.get("kind") or "").strip() or None
                entities.append(EntityDraft(name=str(ent["name"]), kind=kind))
        try:
            importance = float(item.get("importance") if item.get("importance") is not None else 0.5)
        except (TypeError, ValueError):
            importance = 0.5
        drafts.append(
            sanitize_draft(
                FactDraft(
                    subject=str(item.get("subject") or "用户"),
                    predicate=str(item.get("predicate") or "note"),
                    object=obj or content,
                    content=content or obj,
                    importance=importance,
                    profile_label=str(item.get("profile_label") or "notes"),
                    entities=entities,
                )
            )
        )
        if len(drafts) >= _MAX_FACTS:
            break
    return drafts


_SUMMARY_SYSTEM = """你是会话纪要员。把一段多轮对话压缩成 3-5 条要点，供以后检索。
要求：
- 每条一行，以 "- " 开头，不超过 40 字
- 只写结论、决定、未完成的事、用户偏好；不要复述寒暄
- 原文中文就写中文，原文英文就写英文
- 只输出这几行，不要 JSON、不要标题、不要解释
"""


def summarize_fallback(transcript: str, limit: int = 3) -> str:
    """Deterministic digest used when no model is available."""
    lines = [line.strip() for line in transcript.splitlines() if line.strip()]
    if not lines:
        return ""
    picked = lines[:2] + lines[-1:]
    seen: list[str] = []
    for line in picked:
        text = line[:60]
        if text not in seen:
            seen.append(text)
    return "\n".join(f"- {text}" for text in seen[:limit])


class MockExtractor:
    name = "mock"

    def extract(self, text: str) -> list[FactDraft]:
        return extract_mock(text)

    def summarize(self, transcript: str) -> str:
        return summarize_fallback(transcript)


class OpenAIExtractor:
    name = "openai"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def extract(self, text: str) -> list[FactDraft]:
        try:
            payload = self._complete(text)
            return drafts_from_payload(payload)
        except Exception:
            log.warning("OpenAI 抽取失败，改用 mock 抽取器", exc_info=True)
            return extract_mock(text)

    def summarize(self, transcript: str) -> str:
        try:
            return self._complete_text(transcript).strip() or summarize_fallback(transcript)
        except Exception:
            log.warning("会话纪要生成失败，改用截断摘要", exc_info=True)
            return summarize_fallback(transcript)

    def _complete_text(self, text: str) -> str:
        url = self._settings.openai_base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self._settings.openai_api_key}"}
        payload = {
            "model": self._settings.openai_extract_model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": _SUMMARY_SYSTEM},
                {"role": "user", "content": text},
            ],
        }
        with httpx.Client(timeout=45, trust_env=False) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            body = response.json()
        return str(body["choices"][0]["message"]["content"] or "")

    def _complete(self, text: str) -> dict:
        url = self._settings.openai_base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self._settings.openai_api_key}"}
        base = {
            "model": self._settings.openai_extract_model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": _EXTRACT_SYSTEM},
                {"role": "user", "content": text},
            ],
        }
        # trust_env=False: a malformed NO_PROXY (e.g. "[::1]") makes httpx raise
        # InvalidURL before the request is sent, which silently degrades extraction
        # to the mock rule set. This service talks to a local/regional endpoint only.
        with httpx.Client(timeout=45, trust_env=False) as client:
            response = client.post(
                url,
                headers=headers,
                json={**base, "response_format": {"type": "json_object"}},
            )
            if response.status_code == 400:
                response = client.post(url, headers=headers, json=base)
            response.raise_for_status()
            body = response.json()
        content = body["choices"][0]["message"]["content"]
        return _parse_json_object(content)


def _parse_json_object(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("抽取结果不是 JSON 对象")
    return data


def build_extractor(settings: Settings) -> Extractor:
    if settings.llm_enabled:
        return OpenAIExtractor(settings)
    return MockExtractor()
