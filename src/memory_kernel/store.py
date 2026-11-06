from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from memory_kernel.models import Entity, Episode, Fact, Intent, ProfileBlock, Scope, Trigger

FACT_SELECT = """
id, user_id, agent_id, subject, predicate, "object", content,
valid_from, valid_to, superseded_by, source_episode_ids, origin,
importance, metadata, created_at,
(embedding IS NOT NULL) AS has_embedding
"""

EPISODE_SELECT = """
id, user_id, agent_id, session_id, role, content, metadata,
occurred_at, ingested_at, extracted_at
"""

PROFILE_SELECT = """
id, user_id, agent_id, label, content, token_limit, version, updated_at
"""

ENTITY_SELECT = """
id, user_id, name, kind, aliases, metadata, created_at, updated_at
"""

_AGENT_VISIBLE = """
(
    (%(agent_id)s = '' AND agent_id = '')
    OR (%(agent_id)s <> '' AND (agent_id = '' OR agent_id = %(agent_id)s))
)
"""

_PROFILE_ORDER = """
ORDER BY CASE label
    WHEN 'identity' THEN 1
    WHEN 'location' THEN 2
    WHEN 'work' THEN 3
    WHEN 'preferences' THEN 4
    WHEN 'notes' THEN 5
    ELSE 6
END, label
"""


def _fact(row: dict[str, Any]) -> Fact:
    payload = dict(row)
    payload["source_episode_ids"] = list(payload.get("source_episode_ids") or [])
    payload["metadata"] = payload.get("metadata") or {}
    return Fact.model_validate(payload)


def _episode(row: dict[str, Any]) -> Episode:
    payload = dict(row)
    payload["metadata"] = payload.get("metadata") or {}
    return Episode.model_validate(payload)


def _profile(row: dict[str, Any]) -> ProfileBlock:
    return ProfileBlock.model_validate(row)


def _entity(row: dict[str, Any]) -> Entity:
    payload = dict(row)
    payload["aliases"] = list(payload.get("aliases") or [])
    payload["metadata"] = payload.get("metadata") or {}
    return Entity.model_validate(payload)


def _scope_params(scope: Scope, **extra: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "user_id": scope.user_id,
        "agent_id": scope.agent_id or "",
    }
    params.update(extra)
    return params


def audit(
    conn,
    user_id: str,
    action: str,
    target_type: str | None = None,
    target_id: UUID | None = None,
    detail: dict | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO memory_audit (user_id, action, target_type, target_id, detail)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (user_id, action, target_type, target_id, Jsonb(detail or {})),
    )


def insert_episode(
    conn,
    *,
    scope: Scope,
    role: str,
    content: str,
    metadata: dict,
    occurred_at: datetime,
) -> Episode:
    row = conn.execute(
        f"""
        INSERT INTO episodes (
            user_id, agent_id, session_id, role, content, metadata, occurred_at
        )
        VALUES (
            %(user_id)s, %(agent_id)s, %(session_id)s, %(role)s,
            %(content)s, %(metadata)s, %(occurred_at)s
        )
        RETURNING {EPISODE_SELECT}
        """,
        {
            "user_id": scope.user_id,
            "agent_id": scope.agent_id or "",
            "session_id": scope.session_id,
            "role": role,
            "content": content,
            "metadata": Jsonb(metadata or {}),
            "occurred_at": occurred_at,
        },
    ).fetchone()
    return _episode(row)


def lock_episode(conn, episode_id: UUID) -> Episode | None:
    row = conn.execute(
        f"SELECT {EPISODE_SELECT} FROM episodes WHERE id = %s FOR UPDATE",
        (episode_id,),
    ).fetchone()
    return _episode(row) if row else None


def mark_extracted(conn, episode_id: UUID) -> None:
    conn.execute(
        "UPDATE episodes SET extracted_at = now() WHERE id = %s",
        (episode_id,),
    )


def list_pending_ids(conn, limit: int, user_id: str | None = None) -> list[UUID]:
    if user_id:
        rows = conn.execute(
            """
            SELECT id FROM episodes
            WHERE extracted_at IS NULL AND user_id = %s
            ORDER BY ingested_at ASC
            LIMIT %s
            """,
            (user_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id FROM episodes
            WHERE extracted_at IS NULL
            ORDER BY ingested_at ASC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [row["id"] for row in rows]


def list_episodes(conn, scope: Scope, limit: int = 50) -> list[Episode]:
    session_clause = ""
    params = _scope_params(scope, limit=limit)
    if scope.session_id:
        session_clause = "AND session_id = %(session_id)s"
        params["session_id"] = scope.session_id
    rows = conn.execute(
        f"""
        SELECT {EPISODE_SELECT}
        FROM episodes
        WHERE user_id = %(user_id)s
          AND agent_id = %(agent_id)s
          {session_clause}
        ORDER BY occurred_at ASC
        LIMIT %(limit)s
        """,
        params,
    ).fetchall()
    return [_episode(row) for row in rows]


def recent_episodes(conn, scope: Scope, as_of: datetime, limit: int = 40) -> list[Episode]:
    params = _scope_params(scope, as_of=as_of, limit=limit)
    if scope.session_id:
        session_id = scope.session_id
    else:
        row = conn.execute(
            f"""
            SELECT session_id FROM episodes
            WHERE user_id = %(user_id)s
              AND {_AGENT_VISIBLE}
              AND occurred_at <= %(as_of)s
            ORDER BY occurred_at DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        if row is None:
            return []
        session_id = row["session_id"]
    params["session_id"] = session_id
    rows = conn.execute(
        f"""
        SELECT {EPISODE_SELECT}
        FROM episodes
        WHERE user_id = %(user_id)s
          AND session_id = %(session_id)s
          AND occurred_at <= %(as_of)s
          -- Reminder deliveries are artifacts, not conversation. They embed a full
          -- recall snapshot, so feeding them back into L0 nests context inside
          -- context and crowds out the real dialogue.
          AND COALESCE(metadata->>'kind', '') <> 'intent_result'
        ORDER BY occurred_at DESC
        LIMIT %(limit)s
        """,
        params,
    ).fetchall()
    return [_episode(row) for row in rows]


def idle_sessions(conn, before: datetime, limit: int = 5, user_id: str | None = None) -> list[dict]:
    """Sessions whose newest real episode predates `before`."""
    params: dict[str, Any] = {"before": before, "limit": limit}
    user_clause = ""
    if user_id:
        user_clause = "AND user_id = %(user_id)s"
        params["user_id"] = user_id
    rows = conn.execute(
        f"""
        SELECT user_id, agent_id, session_id, max(occurred_at) AS last_at, count(*) AS episodes
        FROM episodes
        WHERE session_id IS NOT NULL
          AND COALESCE(metadata->>'kind', '') NOT IN ('session_summary', 'intent_result')
          {user_clause}
        GROUP BY user_id, agent_id, session_id
        HAVING max(occurred_at) < %(before)s
        ORDER BY last_at DESC
        LIMIT %(limit)s
        """,
        params,
    ).fetchall()
    return list(rows)


def has_session_summary(conn, session_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM facts
        WHERE metadata->>'kind' = 'session_summary'
          AND metadata->>'session_id' = %(session_id)s
        LIMIT 1
        """,
        {"session_id": session_id},
    ).fetchone()
    return row is not None


def insert_fact(
    conn,
    *,
    scope: Scope,
    subject: str,
    predicate: str,
    obj: str,
    content: str,
    valid_from: datetime,
    source_episode_ids: list[UUID],
    origin: str,
    importance: float,
    embedding: str | None,
    metadata: dict,
) -> Fact:
    row = conn.execute(
        f"""
        INSERT INTO facts (
            user_id, agent_id, subject, predicate, "object", content,
            valid_from, source_episode_ids, origin, importance, embedding, metadata
        )
        VALUES (
            %(user_id)s, %(agent_id)s, %(subject)s, %(predicate)s, %(object)s, %(content)s,
            %(valid_from)s, %(source_episode_ids)s::uuid[], %(origin)s, %(importance)s,
            %(embedding)s::vector, %(metadata)s
        )
        RETURNING {FACT_SELECT}
        """,
        {
            "user_id": scope.user_id,
            "agent_id": scope.agent_id or "",
            "subject": subject,
            "predicate": predicate,
            "object": obj,
            "content": content,
            "valid_from": valid_from,
            "source_episode_ids": "{" + ",".join(str(item) for item in source_episode_ids) + "}",
            "origin": origin,
            "importance": importance,
            "embedding": embedding,
            "metadata": Jsonb(metadata or {}),
        },
    ).fetchone()
    return _fact(row)


def find_active_by_predicate(conn, scope: Scope, predicate: str, as_of: datetime) -> list[Fact]:
    rows = conn.execute(
        f"""
        SELECT {FACT_SELECT}
        FROM facts
        WHERE user_id = %(user_id)s
          AND agent_id = %(agent_id)s
          AND predicate = %(predicate)s
          AND valid_from <= %(as_of)s
          AND (valid_to IS NULL OR valid_to > %(as_of)s)
        """,
        _scope_params(scope, predicate=predicate, as_of=as_of),
    ).fetchall()
    return [_fact(row) for row in rows]


def list_facts(
    conn,
    scope: Scope,
    *,
    include_inactive: bool = False,
    as_of: datetime | None = None,
    visible: bool = False,
    limit: int = 200,
) -> list[Fact]:
    params = _scope_params(scope, limit=limit)
    if as_of is not None:
        params["as_of"] = as_of
    agent_clause = _AGENT_VISIBLE if visible else "agent_id = %(agent_id)s"
    time_clause = ""
    if not include_inactive:
        if as_of is None:
            time_clause = "AND valid_to IS NULL"
        else:
            time_clause = """
            AND valid_from <= %(as_of)s
            AND (valid_to IS NULL OR valid_to > %(as_of)s)
            """
    rows = conn.execute(
        f"""
        SELECT {FACT_SELECT}
        FROM facts
        WHERE user_id = %(user_id)s
          AND {agent_clause}
          {time_clause}
        ORDER BY importance DESC, valid_from DESC
        LIMIT %(limit)s
        """,
        params,
    ).fetchall()
    return [_fact(row) for row in rows]


def search_fact_candidates(
    conn,
    scope: Scope,
    query: str,
    as_of: datetime,
    predicates: set[str],
    limit: int = 80,
) -> list[Fact]:
    params = _scope_params(scope, query=query, as_of=as_of, limit=limit)
    clauses = ["similarity(content, %(query)s) > 0.12"]
    terms = []
    from memory_kernel.retrieve import search_terms

    for term in search_terms(query):
        # ILIKE ANY does not take ESCAPE. Drop LIKE metacharacters instead.
        literal = term.replace("\\", "").replace("%", "").replace("_", "")
        if len(literal) < 2:
            continue
        terms.append(f"%{literal}%")
    if terms:
        params["likes"] = terms
        clauses.append("content ILIKE ANY(%(likes)s)")
        clauses.append("subject ILIKE ANY(%(likes)s)")
        clauses.append('"object" ILIKE ANY(%(likes)s)')
    if predicates:
        params["preds"] = list(predicates)
        clauses.append("predicate = ANY(%(preds)s)")
    clauses.append(
        "(char_length(%(query)s) > 0 AND tsv @@ plainto_tsquery('simple', %(query)s))"
    )
    rows = conn.execute(
        f"""
        SELECT {FACT_SELECT}
        FROM facts
        WHERE user_id = %(user_id)s
          AND {_AGENT_VISIBLE}
          AND valid_from <= %(as_of)s
          AND (valid_to IS NULL OR valid_to > %(as_of)s)
          AND ({' OR '.join(clauses)})
        LIMIT %(limit)s
        """,
        params,
    ).fetchall()
    return [_fact(row) for row in rows]


def vector_search(
    conn,
    scope: Scope,
    embedding: str,
    as_of: datetime,
    limit: int = 20,
) -> list[tuple[Fact, float]]:
    rows = conn.execute(
        f"""
        SELECT {FACT_SELECT},
               (1 - (embedding <=> %(embedding)s::vector)) AS similarity
        FROM facts
        WHERE user_id = %(user_id)s
          AND {_AGENT_VISIBLE}
          AND embedding IS NOT NULL
          AND valid_from <= %(as_of)s
          AND (valid_to IS NULL OR valid_to > %(as_of)s)
        ORDER BY embedding <=> %(embedding)s::vector
        LIMIT %(limit)s
        """,
        _scope_params(scope, embedding=embedding, as_of=as_of, limit=limit),
    ).fetchall()
    hits: list[tuple[Fact, float]] = []
    for row in rows:
        payload = dict(row)
        similarity = max(0.0, min(1.0, float(payload.pop("similarity"))))
        hits.append((_fact(payload), similarity))
    return hits


def get_fact(conn, fact_id: UUID) -> Fact | None:
    row = conn.execute(
        f"SELECT {FACT_SELECT} FROM facts WHERE id = %s",
        (fact_id,),
    ).fetchone()
    return _fact(row) if row else None


def supersede_fact(conn, fact_id: UUID, successor_id: UUID, valid_to: datetime) -> None:
    conn.execute(
        """
        UPDATE facts
        SET valid_to = %s, superseded_by = %s
        WHERE id = %s AND (valid_to IS NULL OR valid_to > %s)
        """,
        (valid_to, successor_id, fact_id, valid_to),
    )


def soft_forget(conn, fact_id: UUID, user_id: str, valid_to: datetime) -> None:
    conn.execute(
        """
        UPDATE facts
        SET valid_to = %s
        WHERE id = %s AND user_id = %s AND valid_to IS NULL
        """,
        (valid_to, fact_id, user_id),
    )


def hard_delete_fact(conn, fact_id: UUID, user_id: str) -> None:
    conn.execute(
        "DELETE FROM facts WHERE id = %s AND user_id = %s",
        (fact_id, user_id),
    )


def upsert_entity(conn, user_id: str, name: str, kind: str | None) -> None:
    conn.execute(
        """
        INSERT INTO entities (user_id, name, kind)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id, name) DO UPDATE SET
            kind = COALESCE(entities.kind, EXCLUDED.kind),
            updated_at = now()
        """,
        (user_id, name, kind),
    )


def list_entities(conn, user_id: str) -> list[Entity]:
    rows = conn.execute(
        f"""
        SELECT {ENTITY_SELECT}
        FROM entities
        WHERE user_id = %s
        ORDER BY name
        """,
        (user_id,),
    ).fetchall()
    return [_entity(row) for row in rows]


def upsert_profile_block(
    conn,
    scope: Scope,
    label: str,
    content: str,
    token_limit: int,
) -> ProfileBlock:
    row = conn.execute(
        f"""
        INSERT INTO profile_blocks (user_id, agent_id, label, content, token_limit)
        VALUES (%(user_id)s, %(agent_id)s, %(label)s, %(content)s, %(token_limit)s)
        ON CONFLICT (user_id, agent_id, label) DO UPDATE SET
            content = EXCLUDED.content,
            token_limit = EXCLUDED.token_limit,
            version = profile_blocks.version + 1,
            updated_at = now()
        WHERE profile_blocks.content IS DISTINCT FROM EXCLUDED.content
           OR profile_blocks.token_limit IS DISTINCT FROM EXCLUDED.token_limit
        RETURNING {PROFILE_SELECT}
        """,
        _scope_params(scope, label=label, content=content, token_limit=token_limit),
    ).fetchone()
    if row is None:
        row = conn.execute(
            f"""
            SELECT {PROFILE_SELECT}
            FROM profile_blocks
            WHERE user_id = %(user_id)s AND agent_id = %(agent_id)s AND label = %(label)s
            """,
            _scope_params(scope, label=label),
        ).fetchone()
    return _profile(row)


def delete_profile_block(conn, scope: Scope, label: str) -> bool:
    row = conn.execute(
        """
        DELETE FROM profile_blocks
        WHERE user_id = %(user_id)s AND agent_id = %(agent_id)s AND label = %(label)s
        RETURNING id
        """,
        _scope_params(scope, label=label),
    ).fetchone()
    return row is not None


def list_profile(conn, scope: Scope, *, visible: bool) -> list[ProfileBlock]:
    agent_clause = _AGENT_VISIBLE if visible else "agent_id = %(agent_id)s"
    rows = conn.execute(
        f"""
        SELECT {PROFILE_SELECT}
        FROM profile_blocks
        WHERE user_id = %(user_id)s
          AND {agent_clause}
        {_PROFILE_ORDER}
        """,
        _scope_params(scope),
    ).fetchall()
    return [_profile(row) for row in rows]


def purge_user(conn, user_id: str) -> None:
    conn.execute("UPDATE facts SET superseded_by = NULL WHERE user_id = %s", (user_id,))
    conn.execute("DELETE FROM memory_audit WHERE user_id = %s", (user_id,))
    conn.execute("DELETE FROM facts WHERE user_id = %s", (user_id,))
    conn.execute("DELETE FROM profile_blocks WHERE user_id = %s", (user_id,))
    conn.execute("DELETE FROM intents WHERE user_id = %s", (user_id,))
    conn.execute("DELETE FROM episodes WHERE user_id = %s", (user_id,))
    conn.execute("DELETE FROM entities WHERE user_id = %s", (user_id,))


INTENT_SELECT = """
id, user_id, agent_id, session_id, title, description, status, due_at, priority,
source_episode_ids, metadata, created_at, updated_at
"""

TRIGGER_SELECT = """
id, intent_id, type, expression, at, enabled, last_fired_at, created_at
"""


def _trigger(row: dict[str, Any]) -> Trigger:
    return Trigger.model_validate(row)


def _intent(row: dict[str, Any], triggers: list | None = None) -> Intent:
    payload = dict(row)
    payload["source_episode_ids"] = list(payload.get("source_episode_ids") or [])
    payload["metadata"] = payload.get("metadata") or {}
    payload["triggers"] = triggers or []
    return Intent.model_validate(payload)


def _attach_triggers(conn, intents: list[Intent]) -> list[Intent]:
    if not intents:
        return []
    ids = [intent.id for intent in intents]
    rows = conn.execute(
        f"""
        SELECT {TRIGGER_SELECT}
        FROM triggers
        WHERE intent_id = ANY(%s::uuid[])
        ORDER BY created_at
        """,
        ("{" + ",".join(str(item) for item in ids) + "}",),
    ).fetchall()
    grouped: dict = {}
    for row in rows:
        grouped.setdefault(row["intent_id"], []).append(_trigger(row))
    return [intent.model_copy(update={"triggers": grouped.get(intent.id, [])}) for intent in intents]


def insert_intent(
    conn,
    *,
    scope: Scope,
    title: str,
    description: str,
    due_at: datetime | None,
    priority: int,
    source_episode_ids: list[UUID],
    metadata: dict,
) -> Intent:
    row = conn.execute(
        f"""
        INSERT INTO intents (
            user_id, agent_id, session_id, title, description, due_at, priority,
            source_episode_ids, metadata
        )
        VALUES (
            %(user_id)s, %(agent_id)s, %(session_id)s, %(title)s, %(description)s,
            %(due_at)s, %(priority)s, %(source_episode_ids)s::uuid[], %(metadata)s
        )
        RETURNING {INTENT_SELECT}
        """,
        {
            "user_id": scope.user_id,
            "agent_id": scope.agent_id or "",
            "session_id": scope.session_id,
            "title": title,
            "description": description,
            "due_at": due_at,
            "priority": priority,
            "source_episode_ids": "{" + ",".join(str(item) for item in source_episode_ids) + "}",
            "metadata": Jsonb(metadata or {}),
        },
    ).fetchone()
    return _intent(row)


def insert_trigger(
    conn,
    intent_id: UUID,
    trigger_type: str,
    expression: str | None,
    at: datetime | None,
) -> Trigger:
    row = conn.execute(
        f"""
        INSERT INTO triggers (intent_id, type, expression, at)
        VALUES (%s, %s, %s, %s)
        RETURNING {TRIGGER_SELECT}
        """,
        (intent_id, trigger_type, expression, at),
    ).fetchone()
    return _trigger(row)


def find_open_intent_by_title(conn, scope: Scope, title: str) -> Intent | None:
    row = conn.execute(
        f"""
        SELECT {INTENT_SELECT}
        FROM intents
        WHERE user_id = %(user_id)s
          AND agent_id = %(agent_id)s
          AND title = %(title)s
          AND status IN ('open', 'in_progress')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        _scope_params(scope, title=title),
    ).fetchone()
    if row is None:
        return None
    return _attach_triggers(conn, [_intent(row)])[0]


def get_intent(conn, intent_id: UUID) -> Intent | None:
    row = conn.execute(
        f"SELECT {INTENT_SELECT} FROM intents WHERE id = %s",
        (intent_id,),
    ).fetchone()
    if row is None:
        return None
    return _attach_triggers(conn, [_intent(row)])[0]


def list_intents(conn, scope: Scope, status: str | None = None) -> list[Intent]:
    params = _scope_params(scope)
    status_clause = ""
    if status:
        status_clause = "AND status = %(status)s"
        params["status"] = status
    rows = conn.execute(
        f"""
        SELECT {INTENT_SELECT}
        FROM intents
        WHERE user_id = %(user_id)s
          AND agent_id = %(agent_id)s
          {status_clause}
        ORDER BY priority DESC, due_at NULLS LAST, created_at
        """,
        params,
    ).fetchall()
    return _attach_triggers(conn, [_intent(row) for row in rows])


def update_intent_progress(conn, intent_id: UUID, status: str, metadata: dict) -> Intent:
    row = conn.execute(
        f"""
        UPDATE intents
        SET status = %s, metadata = %s, updated_at = now()
        WHERE id = %s
        RETURNING {INTENT_SELECT}
        """,
        (status, Jsonb(metadata), intent_id),
    ).fetchone()
    return _attach_triggers(conn, [_intent(row)])[0]


def set_intent_status(conn, intent_id: UUID, status: str) -> Intent:
    row = conn.execute(
        f"""
        UPDATE intents
        SET status = %s, updated_at = now()
        WHERE id = %s
        RETURNING {INTENT_SELECT}
        """,
        (status, intent_id),
    ).fetchone()
    return _attach_triggers(conn, [_intent(row)])[0]


def disable_intent_triggers(conn, intent_id: UUID) -> None:
    conn.execute("UPDATE triggers SET enabled = FALSE WHERE intent_id = %s", (intent_id,))


def mark_trigger_fired(conn, trigger_id: UUID, fired_at: datetime, *, disable: bool) -> None:
    conn.execute(
        """
        UPDATE triggers
        SET last_fired_at = %s,
            enabled = CASE WHEN %s THEN FALSE ELSE enabled END
        WHERE id = %s
        """,
        (fired_at, disable, trigger_id),
    )


def reschedule_once_trigger(conn, intent_id: UUID, due_at: datetime, metadata: dict) -> Intent:
    conn.execute(
        """
        UPDATE intents
        SET due_at = %s, metadata = %s, updated_at = now()
        WHERE id = %s
        """,
        (due_at, Jsonb(metadata), intent_id),
    )
    updated = conn.execute(
        """
        UPDATE triggers
        SET at = %s, enabled = TRUE, last_fired_at = NULL
        WHERE intent_id = %s AND type = 'once_at'
        """,
        (due_at, intent_id),
    ).rowcount
    if not updated:
        insert_trigger(conn, intent_id, "once_at", None, due_at)
    intent = get_intent(conn, intent_id)
    if intent is None:
        raise RuntimeError("待办在改期时丢失")
    return intent


def _user_clause(user_id: str | None, params: dict[str, Any], column: str = "i.user_id") -> str:
    if not user_id:
        return ""
    params["user_id"] = user_id
    return f"AND {column} = %(user_id)s"


def due_once_triggers(conn, now: datetime, user_id: str | None, limit: int) -> list[tuple[Intent, Trigger]]:
    params: dict[str, Any] = {"now": now, "limit": limit}
    user_clause = _user_clause(user_id, params)
    rows = conn.execute(
        f"""
        SELECT
            i.id AS i_id, i.user_id, i.agent_id, i.session_id, i.title, i.description,
            i.status, i.due_at, i.priority, i.source_episode_ids, i.metadata,
            i.created_at, i.updated_at,
            t.id AS t_id, t.intent_id, t.type, t.expression, t.at, t.enabled,
            t.last_fired_at, t.created_at AS t_created_at
        FROM triggers t
        JOIN intents i ON i.id = t.intent_id
        WHERE t.enabled = TRUE
          AND t.type = 'once_at'
          AND t.at <= %(now)s
          AND t.last_fired_at IS NULL
          AND i.status IN ('open', 'in_progress')
          {user_clause}
        ORDER BY i.priority DESC, t.at
        LIMIT %(limit)s
        """,
        params,
    ).fetchall()
    items: list[tuple[Intent, Trigger]] = []
    for row in rows:
        intent = _intent(
            {
                "id": row["i_id"],
                "user_id": row["user_id"],
                "agent_id": row["agent_id"],
                "session_id": row["session_id"],
                "title": row["title"],
                "description": row["description"],
                "status": row["status"],
                "due_at": row["due_at"],
                "priority": row["priority"],
                "source_episode_ids": row["source_episode_ids"],
                "metadata": row["metadata"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
        trigger = _trigger(
            {
                "id": row["t_id"],
                "intent_id": row["intent_id"],
                "type": row["type"],
                "expression": row["expression"],
                "at": row["at"],
                "enabled": row["enabled"],
                "last_fired_at": row["last_fired_at"],
                "created_at": row["t_created_at"],
            }
        )
        items.append((intent, trigger))
    return items


def due_cron_triggers(conn, now: datetime, user_id: str | None, limit: int) -> list[tuple[Intent, Trigger]]:
    params: dict[str, Any] = {"now": now, "limit": limit}
    user_clause = _user_clause(user_id, params)
    rows = conn.execute(
        f"""
        SELECT
            i.id AS i_id, i.user_id, i.agent_id, i.session_id, i.title, i.description,
            i.status, i.due_at, i.priority, i.source_episode_ids, i.metadata,
            i.created_at, i.updated_at,
            t.id AS t_id, t.intent_id, t.type, t.expression, t.at, t.enabled,
            t.last_fired_at, t.created_at AS t_created_at
        FROM triggers t
        JOIN intents i ON i.id = t.intent_id
        WHERE t.enabled = TRUE
          AND t.type = 'cron'
          AND t.expression IS NOT NULL
          AND i.status IN ('open', 'in_progress')
          AND (t.last_fired_at IS NULL OR t.last_fired_at < date_trunc('minute', %(now)s))
          {user_clause}
        ORDER BY i.priority DESC
        LIMIT %(limit)s
        """,
        params,
    ).fetchall()
    items: list[tuple[Intent, Trigger]] = []
    for row in rows:
        intent = _intent(
            {
                "id": row["i_id"],
                "user_id": row["user_id"],
                "agent_id": row["agent_id"],
                "session_id": row["session_id"],
                "title": row["title"],
                "description": row["description"],
                "status": row["status"],
                "due_at": row["due_at"],
                "priority": row["priority"],
                "source_episode_ids": row["source_episode_ids"],
                "metadata": row["metadata"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
        trigger = _trigger(
            {
                "id": row["t_id"],
                "intent_id": row["intent_id"],
                "type": row["type"],
                "expression": row["expression"],
                "at": row["at"],
                "enabled": row["enabled"],
                "last_fired_at": row["last_fired_at"],
                "created_at": row["t_created_at"],
            }
        )
        items.append((intent, trigger))
    return items


def stale_cron_triggers(conn, day_start: datetime, user_id: str | None, limit: int) -> list[tuple[Intent, Trigger]]:
    """Enabled cron triggers that have not fired since `day_start`."""
    params: dict[str, Any] = {"day_start": day_start, "limit": limit}
    user_clause = _user_clause(user_id, params)
    rows = conn.execute(
        f"""
        SELECT
            i.id AS i_id, i.user_id, i.agent_id, i.session_id, i.title, i.description,
            i.status, i.due_at, i.priority, i.source_episode_ids, i.metadata,
            i.created_at, i.updated_at,
            t.id AS t_id, t.intent_id, t.type, t.expression, t.at, t.enabled,
            t.last_fired_at, t.created_at AS t_created_at
        FROM triggers t
        JOIN intents i ON i.id = t.intent_id
        WHERE t.enabled = TRUE
          AND t.type = 'cron'
          AND t.expression IS NOT NULL
          AND i.status IN ('open', 'in_progress')
          AND (t.last_fired_at IS NULL OR t.last_fired_at < %(day_start)s)
          {user_clause}
        ORDER BY i.priority DESC
        LIMIT %(limit)s
        """,
        params,
    ).fetchall()
    items: list[tuple[Intent, Trigger]] = []
    for row in rows:
        items.append(
            (
                _intent(
                    {
                        "id": row["i_id"],
                        "user_id": row["user_id"],
                        "agent_id": row["agent_id"],
                        "session_id": row["session_id"],
                        "title": row["title"],
                        "description": row["description"],
                        "status": row["status"],
                        "due_at": row["due_at"],
                        "priority": row["priority"],
                        "source_episode_ids": row["source_episode_ids"],
                        "metadata": row["metadata"],
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    }
                ),
                _trigger(
                    {
                        "id": row["t_id"],
                        "intent_id": row["intent_id"],
                        "type": row["type"],
                        "expression": row["expression"],
                        "at": row["at"],
                        "enabled": row["enabled"],
                        "last_fired_at": row["last_fired_at"],
                        "created_at": row["t_created_at"],
                    }
                ),
            )
        )
    return items


def due_intents_without_trigger(conn, now: datetime, user_id: str | None, limit: int) -> list[Intent]:
    params: dict[str, Any] = {"now": now, "limit": limit}
    user_clause = _user_clause(user_id, params)
    rows = conn.execute(
        f"""
        SELECT {INTENT_SELECT}
        FROM intents i
        WHERE i.status IN ('open', 'in_progress')
          AND i.due_at IS NOT NULL
          AND i.due_at <= %(now)s
          AND COALESCE(i.metadata->>'acted_at', '') = ''
          {user_clause}
          AND NOT EXISTS (
              SELECT 1 FROM triggers t
              WHERE t.intent_id = i.id AND t.type = 'once_at'
          )
        ORDER BY i.priority DESC, i.due_at
        LIMIT %(limit)s
        """,
        params,
    ).fetchall()
    return [_intent(row) for row in rows]
