from __future__ import annotations

import os
import uuid

import psycopg
import pytest

os.environ["OPENAI_API_KEY"] = ""

from memory_kernel.config import load_settings
from memory_kernel.models import Scope
from memory_kernel.service import MemoryService


@pytest.fixture(scope="session")
def svc() -> MemoryService:
    settings = load_settings()
    try:
        with psycopg.connect(settings.database_url, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        pytest.skip(f"Postgres 不可用: {exc}")
    service = MemoryService(settings)
    service.migrate()
    return service


@pytest.fixture
def users(svc: MemoryService):
    created: list[str] = []

    def make(agent_id: str = "", *, user_id: str | None = None) -> Scope:
        uid = user_id or f"test-{uuid.uuid4()}"
        created.append(uid)
        return Scope(user_id=uid, agent_id=agent_id, session_id=f"s-{uuid.uuid4().hex[:8]}")

    yield make
    for uid in created:
        svc.purge_user(uid)
