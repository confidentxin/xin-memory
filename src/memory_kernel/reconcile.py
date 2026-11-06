from __future__ import annotations

import re

from memory_kernel.models import Fact, FactDraft
from memory_kernel.util import clamp_importance, is_cjk_text, norm

MANAGED_LABELS = ("identity", "location", "work", "preferences", "notes")

PREDICATE_LABEL = {
    "name": "identity",
    "age": "identity",
    "language": "identity",
    "timezone": "identity",
    "occupation": "work",
    "works_at": "work",
    "title": "work",
    "lives_in": "location",
    "likes": "preferences",
    "dislikes": "preferences",
    "drinks": "preferences",
    "allergy": "preferences",
    "note": "notes",
}

# One current value per subject. A new object invalidates the previous row.
SINGLETON_PREDICATES = {
    "name",
    "age",
    "language",
    "timezone",
    "occupation",
    "works_at",
    "title",
    "lives_in",
    "email",
}

_PREDICATE = re.compile(r"[a-z][a-z0-9_]{0,40}")


def normalize_predicate(predicate: str) -> str:
    cleaned = predicate.strip().lower().replace(" ", "_").replace("-", "_")
    if _PREDICATE.fullmatch(cleaned):
        return cleaned
    return "note"


def label_for_predicate(predicate: str, profile_label: str | None = None) -> str:
    if profile_label in MANAGED_LABELS:
        return profile_label
    return PREDICATE_LABEL.get(predicate, "notes")


def label_for_fact(fact: Fact) -> str:
    raw = (fact.metadata or {}).get("profile_label")
    label = raw if isinstance(raw, str) else None
    return label_for_predicate(fact.predicate, label)


def sanitize_draft(draft: FactDraft) -> FactDraft:
    predicate = normalize_predicate(draft.predicate)
    label = label_for_predicate(predicate, draft.profile_label)
    cjk = is_cjk_text(draft.content) or is_cjk_text(draft.object) or is_cjk_text(draft.subject)
    subject = " ".join(draft.subject.split()) or ("用户" if cjk else "user")
    obj = clean_object(draft.object) or clean_object(draft.content)[:80]
    content = " ".join(draft.content.split()) or f"{subject} {predicate} {obj}"
    return draft.model_copy(
        update={
            "predicate": predicate,
            "profile_label": label,
            "subject": subject[:80],
            "object": obj[:200],
            "content": content[:500],
            "importance": clamp_importance(draft.importance),
        }
    )


def clean_object(value: str) -> str:
    text = value.strip().strip("\"'“”‘’")
    text = re.sub(r"[\s，,。；;！!？?]+$", "", text)
    text = re.sub(r"^[\s，,。；;！!？?]+", "", text)
    return text.strip()


def decide_fact(draft: FactDraft, active_same_key: list[Fact]) -> str:
    """Return skip, supersede, or add.

    active_same_key must already be the active facts with the same subject and predicate.
    Same object is a duplicate. Singleton predicates replace the previous object.
    Multi-value predicates (likes, allergy, note, ...) accumulate.
    """
    if any(norm(fact.object) == norm(draft.object) for fact in active_same_key):
        return "skip"
    if draft.predicate in SINGLETON_PREDICATES and active_same_key:
        return "supersede"
    return "add"


def fallback_note(text: str, importance: float | None = None) -> FactDraft:
    cjk = is_cjk_text(text)
    clean = " ".join(text.split())
    return sanitize_draft(
        FactDraft(
            subject="用户" if cjk else "user",
            predicate="note",
            object=clean[:80],
            content=clean[:500],
            importance=0.55 if importance is None else importance,
            profile_label="notes",
        )
    )
