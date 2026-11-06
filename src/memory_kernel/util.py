from __future__ import annotations

import re
from datetime import datetime, timezone

_CJK = re.compile(r"[\u4e00-\u9fff]")


def ensure_utc(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def is_cjk_text(text: str) -> bool:
    return _CJK.search(text) is not None


def clamp_importance(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def norm(text: str) -> str:
    """Normalize fact keys so 杭州市 and 杭州 compare equal, without fuzzy matching."""
    cleaned = text.strip().lower()
    cleaned = cleaned.strip("。．.，,！!？?、；;：:\"'“”‘’()（）")
    if _CJK.search(cleaned):
        cleaned = re.sub(r"\s+", "", cleaned)
        if cleaned.endswith("市") and len(cleaned) > 2:
            cleaned = cleaned[:-1]
        if cleaned.endswith("省") and len(cleaned) > 2:
            cleaned = cleaned[:-1]
    else:
        cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def estimate_tokens(text: str) -> int:
    """Rough budget counter: one CJK character ≈ one token, four other chars ≈ one token."""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def truncate_to_tokens(text: str, budget: int) -> str:
    if budget <= 0 or not text:
        return ""
    if estimate_tokens(text) <= budget:
        return text
    ellipsis = "…"
    keep = max(0, budget - estimate_tokens(ellipsis))
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= keep:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + ellipsis


def redact_dsn(url: str) -> str:
    return re.sub(r"://([^:/]+):[^@]+@", r"://\1:***@", url)
