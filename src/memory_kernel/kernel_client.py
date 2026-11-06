"""HTTP client for the memory kernel. MCP and other adapters share this."""

from __future__ import annotations

import os
from typing import Any

import httpx


class KernelApiError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class KernelClient:
    def __init__(
        self,
        base_url: str | None = None,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        http: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = (base_url or os.getenv("KERNEL_URL") or "http://127.0.0.1:8741").rstrip("/")
        self.user_id = user_id or os.getenv("MEMORY_USER_ID") or "default"
        self.agent_id = agent_id if agent_id is not None else os.getenv("MEMORY_AGENT_ID", "")
        self.session_id = session_id if session_id is not None else os.getenv("MEMORY_SESSION_ID") or None
        self._http = http or httpx.Client(base_url=self.base_url, timeout=timeout, trust_env=False)
        self._owns_http = http is None

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def recall(
        self,
        query: str,
        *,
        budget: int = 1200,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/recall",
            {
                "user_id": self._user(user_id),
                "agent_id": self.agent_id,
                "session_id": session_id or self.session_id,
                "query": query,
                "budget": budget,
            },
        )

    def remember(
        self,
        text: str,
        *,
        importance: float | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "user_id": self._user(user_id),
            "agent_id": self.agent_id,
            "text": text,
        }
        if self.session_id:
            body["session_id"] = self.session_id
        if importance is not None:
            body["importance"] = importance
        return self._post("/remember", body)

    def append_episode(
        self,
        content: str,
        *,
        role: str = "user",
        session_id: str | None = None,
        user_id: str | None = None,
        metadata: dict | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/episodes",
            {
                "user_id": self._user(user_id),
                "agent_id": self.agent_id,
                "session_id": session_id or self.session_id or "mcp",
                "role": role,
                "content": content,
                "metadata": metadata or {"origin": "mcp"},
            },
        )

    def get_profile(self, *, user_id: str | None = None) -> Any:
        return self._get("/profile", {"user_id": self._user(user_id), "agent_id": self.agent_id})

    def list_intents(self, *, status: str | None = None, user_id: str | None = None) -> Any:
        params = {"user_id": self._user(user_id), "agent_id": self.agent_id}
        if status:
            params["status"] = status
        return self._get("/intents", params)

    def tick(self, *, user_id: str | None = None, now: str | None = None, limit: int = 50) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if user_id:
            params["user_id"] = user_id
        if now:
            params["now"] = now
        response = self._http.post("/tick", params=params)
        data = self._decode(response)
        return data if isinstance(data, dict) else {"fired": []}

    def create_intent(
        self,
        title: str,
        *,
        description: str = "",
        due_at: str | None = None,
        priority: int = 0,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "user_id": self._user(user_id),
            "agent_id": self.agent_id,
            "session_id": session_id or self.session_id,
            "title": title,
            "description": description,
            "priority": priority,
        }
        if due_at:
            body["due_at"] = due_at
        return self._post("/intents", body)

    def _user(self, override: str | None) -> str:
        chosen = (override or self.user_id).strip()
        if not chosen:
            raise KernelApiError(400, "user_id 不能为空")
        return chosen

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        response = self._http.post(path, json=body)
        return self._decode(response)

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        response = self._http.get(path, params=params)
        return self._decode(response)

    def _decode(self, response: httpx.Response) -> Any:
        if response.status_code >= 400:
            detail = response.text
            try:
                payload = response.json()
                if isinstance(payload, dict) and payload.get("error"):
                    detail = str(payload["error"])
            except Exception:
                pass
            raise KernelApiError(response.status_code, detail)
        if not response.content:
            return None
        return response.json()
