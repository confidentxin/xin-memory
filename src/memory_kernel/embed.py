from __future__ import annotations

import logging
from typing import Protocol

import httpx

from memory_kernel.config import Settings

log = logging.getLogger(__name__)


class Embedder(Protocol):
    name: str
    available: bool

    def embed(self, texts: list[str]) -> list[list[float]] | None: ...


class NullEmbedder:
    name = "none"
    available = False

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        return None


class OpenAIEmbedder:
    name = "openai"
    available = True

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        if not texts:
            return []
        try:
            vectors = self._call(texts, with_dimensions=True)
        except httpx.HTTPStatusError:
            try:
                vectors = self._call(texts, with_dimensions=False)
            except Exception:
                log.warning("嵌入请求失败，本批事实不写向量", exc_info=True)
                return None
        except Exception:
            log.warning("嵌入请求失败，本批事实不写向量", exc_info=True)
            return None
        if any(len(vector) != self._settings.embed_dim for vector in vectors):
            log.warning(
                "嵌入维度与 schema 不一致（期望 %s），跳过写入",
                self._settings.embed_dim,
            )
            return None
        return vectors

    def _call(self, texts: list[str], *, with_dimensions: bool) -> list[list[float]]:
        url = self._settings.openai_base_url.rstrip("/") + "/embeddings"
        payload: dict = {"model": self._settings.openai_embed_model, "input": texts}
        if with_dimensions:
            payload["dimensions"] = self._settings.embed_dim
        headers = {"Authorization": f"Bearer {self._settings.openai_api_key}"}
        # trust_env=False: see extract.py — a malformed NO_PROXY breaks httpx
        # before the request, which turns embedding writes into silent no-ops.
        with httpx.Client(timeout=45, trust_env=False) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            body = response.json()
        rows = sorted(body["data"], key=lambda item: item.get("index", 0))
        return [row["embedding"] for row in rows]


def build_embedder(settings: Settings) -> Embedder:
    if settings.llm_enabled:
        return OpenAIEmbedder(settings)
    return NullEmbedder()


def vector_literal(values: list[float] | None, dim: int) -> str | None:
    if not values or len(values) != dim:
        return None
    return "[" + ",".join(format(value, ".7f") for value in values) + "]"
