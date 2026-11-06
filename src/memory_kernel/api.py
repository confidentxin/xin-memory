from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from memory_kernel import __version__
from memory_kernel.models import (
    EpisodeIn,
    ForgetIn,
    IntentIn,
    ProfileUpdateIn,
    RecallIn,
    RememberIn,
    Scope,
)
from memory_kernel.service import MemoryError, MemoryService

app = FastAPI(title="记忆内核", version=__version__)
service = MemoryService()


@app.exception_handler(MemoryError)
def handle_memory_error(_request, exc: MemoryError):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.message})


@app.get("/")
def root() -> dict:
    return {
        "name": "memory-kernel",
        "version": __version__,
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health")
def health() -> dict:
    try:
        with service.connect() as conn:
            conn.execute("SELECT 1")
    except MemoryError as exc:
        return JSONResponse(status_code=exc.status_code, content={"status": "degraded", "error": exc.message})
    return {
        "status": "ok",
        "extractor": service.extractor.name,
        "embeddings": service.embedder.name,
    }


@app.post("/episodes")
def append_episode(body: EpisodeIn):
    scope = Scope(user_id=body.user_id, agent_id=body.agent_id, session_id=body.session_id)
    return service.append_episode(
        scope,
        body.role,
        body.content,
        metadata=body.metadata,
        occurred_at=body.occurred_at,
    )


@app.post("/remember")
def remember(body: RememberIn):
    scope = Scope(user_id=body.user_id, agent_id=body.agent_id, session_id=body.session_id)
    return service.remember(scope, body.text, importance=body.importance)


@app.post("/recall")
def recall(body: RecallIn):
    scope = Scope(user_id=body.user_id, agent_id=body.agent_id, session_id=body.session_id)
    return service.recall(scope, body.query, budget=body.budget, as_of=body.as_of)


@app.post("/forget")
def forget(body: ForgetIn):
    scope = Scope(user_id=body.user_id, agent_id=body.agent_id)
    return service.forget(scope, fact_id=body.fact_id, query=body.query, hard=body.hard)


@app.get("/profile")
def get_profile(user_id: str = Query(...), agent_id: str = Query("")):
    return service.get_profile(Scope(user_id=user_id, agent_id=agent_id))


@app.put("/profile")
def update_profile(body: ProfileUpdateIn):
    scope = Scope(user_id=body.user_id, agent_id=body.agent_id)
    return service.update_profile(scope, body.label, body.content, token_limit=body.token_limit)


@app.post("/extract")
def extract(limit: int = Query(50, ge=1, le=500), user_id: str | None = Query(None)):
    return service.process_pending(limit=limit, user_id=user_id)


@app.get("/facts")
def list_facts(
    user_id: str = Query(...),
    agent_id: str = Query(""),
    include_inactive: bool = Query(False),
):
    return service.list_facts(Scope(user_id=user_id, agent_id=agent_id), include_inactive=include_inactive)


@app.get("/episodes")
def list_episodes(
    user_id: str = Query(...),
    agent_id: str = Query(""),
    session_id: str | None = Query(None),
):
    return service.list_episodes(Scope(user_id=user_id, agent_id=agent_id, session_id=session_id))


@app.get("/entities")
def list_entities(user_id: str = Query(...)):
    return service.list_entities(user_id)


@app.post("/intents")
def create_intent(body: IntentIn):
    scope = Scope(user_id=body.user_id, agent_id=body.agent_id, session_id=body.session_id)
    return service.create_intent(
        scope,
        body.title,
        description=body.description,
        due_at=body.due_at,
        priority=body.priority,
        metadata=body.metadata,
    )


@app.get("/intents")
def list_intents(
    user_id: str = Query(...),
    agent_id: str = Query(""),
    status: str | None = Query(None),
):
    return service.list_intents(Scope(user_id=user_id, agent_id=agent_id), status=status)


@app.post("/intents/{intent_id}/complete")
def complete_intent(intent_id: UUID, user_id: str = Query(...), agent_id: str = Query("")):
    return service.complete_intent(Scope(user_id=user_id, agent_id=agent_id), intent_id)


@app.post("/intents/{intent_id}/cancel")
def cancel_intent(intent_id: UUID, user_id: str = Query(...), agent_id: str = Query("")):
    return service.cancel_intent(Scope(user_id=user_id, agent_id=agent_id), intent_id)


@app.post("/tick")
def tick(
    user_id: str | None = Query(None),
    now: datetime | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    return service.tick(user_id=user_id, now=now, limit=limit)


@app.get("/facts/{fact_id}")
def get_fact(fact_id: UUID):
    with service.connect() as conn:
        from memory_kernel import store

        fact = store.get_fact(conn, fact_id)
    if fact is None:
        raise MemoryError("事实不存在", 404)
    return fact
