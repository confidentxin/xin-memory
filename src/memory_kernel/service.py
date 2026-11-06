from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid5, NAMESPACE_URL

import psycopg

from memory_kernel.agenda import (
    cron_matches,
    extract_intent_drafts,
    last_occurrence_today,
    policy_for,
    proposed_action_text,
    render_action_episode,
    valid_cron as _valid_cron,
)
from memory_kernel.config import Settings, load_settings, sql_paths
from memory_kernel.db import apply_sql_script, connect
from memory_kernel.embed import Embedder, build_embedder, vector_literal
from memory_kernel.extract import Extractor, build_extractor
from memory_kernel.models import (
    ApplyResult,
    Episode,
    ExtractReport,
    Fact,
    FactDraft,
    ForgetResult,
    Intent,
    IntentDraft,
    ProfileBlock,
    RecallResult,
    RememberResult,
    Scope,
    TickReport,
    TickFire,
    Trigger,
)
from memory_kernel.reconcile import (
    MANAGED_LABELS,
    clean_object,
    decide_fact,
    fallback_note,
    sanitize_draft,
)
from memory_kernel.retrieve import (
    assemble_prompt,
    hinted_predicates,
    rank_facts,
    render_profile_from_facts,
)
from memory_kernel.util import clamp_importance, ensure_utc, norm, truncate_to_tokens
from memory_kernel import store

log = logging.getLogger(__name__)

_GENERIC_SUBJECTS = {"用户", "user", "我", "i", "me"}
_ROLES = {"user", "assistant", "system", "tool"}
_ENTITY_KINDS = {
    "lives_in": "place",
    "works_at": "org",
    "name": "person",
    "occupation": "role",
}


class MemoryError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class MemoryService:
    def __init__(
        self,
        settings: Settings | None = None,
        extractor: Extractor | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.extractor = extractor or build_extractor(self.settings)
        self.embedder = embedder or build_embedder(self.settings)

    @contextmanager
    def connect(self):
        try:
            conn = connect(self.settings.database_url)
        except psycopg.OperationalError as exc:
            raise MemoryError(
                "无法连接 Postgres。请先执行 docker compose up -d，并检查 DATABASE_URL。"
                f" 详情: {exc}",
                503,
            ) from exc
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def migrate(self) -> None:
        scripts = [path.read_text(encoding="utf-8") for path in sql_paths()]
        with self.connect() as conn:
            for script in scripts:
                apply_sql_script(conn, script)

    def append_episode(
        self,
        scope: Scope,
        role: str,
        content: str,
        *,
        metadata: dict | None = None,
        occurred_at: datetime | None = None,
        mark_extracted: bool = False,
    ) -> Episode:
        self._check_scope(scope, require_session=True)
        if role not in _ROLES:
            raise MemoryError("role 必须是 user、assistant、system 或 tool", 400)
        self._check_text(content, "content")
        moment = ensure_utc(occurred_at)
        with self.connect() as conn:
            episode = store.insert_episode(
                conn,
                scope=scope,
                role=role,
                content=content,
                metadata=metadata or {},
                occurred_at=moment,
            )
            store.audit(
                conn,
                scope.user_id,
                "append_episode",
                "episode",
                episode.id,
                {"role": role, "session_id": scope.session_id},
            )
            if role == "user":
                for draft in extract_intent_drafts(content, moment):
                    if not draft.explicit:
                        continue
                    self._save_intent(conn, scope, draft, [episode.id], dedupe=True)
            if mark_extracted:
                store.mark_extracted(conn, episode.id)
                episode = episode.model_copy(update={"extracted_at": moment})
            return episode

    def remember(
        self,
        scope: Scope,
        text: str,
        *,
        importance: float | None = None,
        at: datetime | None = None,
    ) -> RememberResult:
        self._check_scope(scope)
        self._check_text(text, "text")
        if importance is not None:
            importance = clamp_importance(importance)
        drafts = self.extractor.extract(text)
        if not drafts:
            drafts = [fallback_note(text, importance)]
        elif importance is not None:
            drafts = [draft.model_copy(update={"importance": importance}) for draft in drafts]
        moment = ensure_utc(at)
        with self.connect() as conn:
            applied = self.apply_fact_drafts(
                conn,
                scope,
                drafts,
                origin="explicit",
                source_episode_ids=[],
                at=moment,
                refresh=True,
            )
            profile = store.list_profile(conn, scope, visible=False)
            return RememberResult(
                added=applied.added,
                superseded=applied.superseded,
                skipped=applied.skipped,
                profile=profile,
            )

    def apply_fact_drafts(
        self,
        conn,
        scope: Scope,
        drafts: list,
        *,
        origin: str,
        source_episode_ids: list[UUID],
        at: datetime | None = None,
        refresh: bool = True,
        metadata: dict | None = None,
    ) -> ApplyResult:
        moment = ensure_utc(at)
        clean_drafts = [sanitize_draft(draft) for draft in drafts]
        vectors = None
        if clean_drafts and self.embedder.available:
            vectors = self.embedder.embed([draft.content for draft in clean_drafts])
        added: list[Fact] = []
        superseded: list[Fact] = []
        skipped: list[Fact] = []
        for index, draft in enumerate(clean_drafts):
            active = store.find_active_by_predicate(conn, scope, draft.predicate, moment)
            matched = [fact for fact in active if norm(fact.subject) == norm(draft.subject)]
            decision = decide_fact(draft, matched)
            if decision == "skip":
                existing = next(fact for fact in matched if norm(fact.object) == norm(draft.object))
                skipped.append(existing)
                store.audit(
                    conn,
                    scope.user_id,
                    "skip_fact",
                    "fact",
                    existing.id,
                    {"origin": origin, "predicate": draft.predicate, "object": draft.object},
                )
                continue
            embedding = None
            if vectors and index < len(vectors):
                embedding = vector_literal(vectors[index], self.settings.embed_dim)
            fact = store.insert_fact(
                conn,
                scope=scope,
                subject=draft.subject,
                predicate=draft.predicate,
                obj=draft.object,
                content=draft.content,
                valid_from=moment,
                source_episode_ids=source_episode_ids,
                origin=origin,
                importance=draft.importance,
                embedding=embedding,
                metadata={
                    "profile_label": draft.profile_label,
                    "extractor": self.extractor.name,
                    **(metadata or {}),
                },
            )
            added.append(fact)
            replaced: list[Fact] = []
            if decision == "supersede":
                for old in matched:
                    if norm(old.object) == norm(draft.object):
                        continue
                    store.supersede_fact(conn, old.id, fact.id, moment)
                    replaced.append(old.model_copy(update={"valid_to": moment, "superseded_by": fact.id}))
                superseded.extend(replaced)
            action = "remember" if origin == "explicit" else "extract_add"
            store.audit(
                conn,
                scope.user_id,
                action,
                "fact",
                fact.id,
                {
                    "origin": origin,
                    "predicate": fact.predicate,
                    "object": fact.object,
                    "content": fact.content,
                    "superseded": [str(old.id) for old in replaced],
                },
            )
            for old in replaced:
                store.audit(
                    conn,
                    scope.user_id,
                    "supersede",
                    "fact",
                    old.id,
                    {"superseded_by": str(fact.id), "origin": origin, "object": old.object},
                )
            for name, kind in entity_pairs(draft):
                store.upsert_entity(conn, scope.user_id, name, kind)
        if refresh:
            self.refresh_profile(conn, scope, moment)
        return ApplyResult(added=added, superseded=superseded, skipped=skipped)

    def refresh_profile(self, conn, scope: Scope, at: datetime | None = None) -> list[ProfileBlock]:
        moment = ensure_utc(at)
        facts = store.list_facts(conn, scope, include_inactive=False, as_of=moment, visible=False, limit=500)
        rendered = render_profile_from_facts(facts)
        touched: list[str] = []
        for label in MANAGED_LABELS:
            content = rendered.get(label)
            if not content:
                if store.delete_profile_block(conn, scope, label):
                    touched.append(label)
                continue
            store.upsert_profile_block(conn, scope, label, content, token_limit=200)
            touched.append(label)
        store.audit(
            conn,
            scope.user_id,
            "profile_refresh",
            "profile_block",
            None,
            {"labels": touched, "agent_id": scope.agent_id or ""},
        )
        return store.list_profile(conn, scope, visible=False)

    def get_profile(self, scope: Scope) -> list[ProfileBlock]:
        self._check_scope(scope)
        with self.connect() as conn:
            return store.list_profile(conn, scope, visible=True)

    def update_profile(
        self,
        scope: Scope,
        label: str,
        content: str,
        *,
        token_limit: int = 200,
    ) -> ProfileBlock:
        self._check_scope(scope)
        label = label.strip()
        if not label or len(label) > 80:
            raise MemoryError("label 无效", 400)
        self._check_text(content, "content")
        token_limit = max(16, min(800, int(token_limit)))
        stored = truncate_to_tokens(content.strip(), token_limit)
        with self.connect() as conn:
            block = store.upsert_profile_block(conn, scope, label, stored, token_limit)
            store.audit(
                conn,
                scope.user_id,
                "profile_update",
                "profile_block",
                block.id,
                {"label": label},
            )
            return block

    def recall(
        self,
        scope: Scope,
        query: str,
        *,
        budget: int = 2500,
        as_of: datetime | None = None,
    ) -> RecallResult:
        self._check_scope(scope)
        if budget < 1 or budget > 20000:
            raise MemoryError("budget 需在 1 到 20000 之间", 400)
        explicit_time = as_of is not None
        moment = ensure_utc(as_of)
        query = query or ""
        with self.connect() as conn:
            vector_similarity: dict[UUID, float] = {}
            if query.strip():
                facts = store.search_fact_candidates(
                    conn,
                    scope,
                    query,
                    moment,
                    hinted_predicates(query),
                )
                embedded = self._query_vector(query)
                if embedded is not None:
                    literal = vector_literal(embedded, self.settings.embed_dim)
                    if literal is not None:
                        seen = {fact.id for fact in facts}
                        for fact, similarity in store.vector_search(conn, scope, literal, moment):
                            vector_similarity[fact.id] = similarity
                            if fact.id not in seen:
                                facts.append(fact)
                                seen.add(fact.id)
            else:
                facts = store.list_facts(
                    conn,
                    scope,
                    include_inactive=False,
                    as_of=moment,
                    visible=True,
                    limit=50,
                )
            hits = rank_facts(query, facts, as_of=moment, vector_similarity=vector_similarity)
            if explicit_time:
                historical = store.list_facts(
                    conn,
                    scope,
                    include_inactive=False,
                    as_of=moment,
                    visible=True,
                    limit=500,
                )
                profile = profile_view(scope, historical, moment)
            else:
                profile = store.list_profile(conn, scope, visible=True)
            episodes = store.recent_episodes(conn, scope, moment)
        return assemble_prompt(
            query=query,
            as_of=moment,
            budget=budget,
            profile=profile,
            hits=hits,
            episodes=episodes,
        )

    def forget(
        self,
        scope: Scope,
        *,
        fact_id: UUID | None = None,
        query: str | None = None,
        hard: bool = False,
    ) -> ForgetResult:
        self._check_scope(scope)
        if fact_id is None and not (query and query.strip()):
            raise MemoryError("需要 fact_id 或 query", 400)
        if fact_id is not None:
            with self.connect() as conn:
                fact = store.get_fact(conn, fact_id)
            if fact is None or fact.user_id != scope.user_id or not agent_can_see(fact.agent_id, scope.agent_id):
                raise MemoryError("事实不存在", 404)
            targets = [fact]
        else:
            recalled = self.recall(scope, query or "", budget=800)
            targets = [hit.fact for hit in recalled.hits if hit.relevance >= 0.35][:5]
            if not targets:
                return ForgetResult(forgotten=[], hard=hard)
        moment = ensure_utc()
        forgotten: list[Fact] = []
        with self.connect() as conn:
            for fact in targets:
                if hard:
                    store.hard_delete_fact(conn, fact.id, scope.user_id)
                    forgotten.append(fact)
                else:
                    store.soft_forget(conn, fact.id, scope.user_id, moment)
                    forgotten.append(fact.model_copy(update={"valid_to": moment}))
                store.audit(
                    conn,
                    scope.user_id,
                    "forget",
                    "fact",
                    fact.id,
                    {"hard": hard, "content": fact.content},
                )
            self.refresh_profile(conn, scope, moment)
        return ForgetResult(forgotten=forgotten, hard=hard)

    def process_pending(self, limit: int = 50, user_id: str | None = None) -> ExtractReport:
        limit = max(1, min(500, int(limit)))
        with self.connect() as conn:
            pending = store.list_pending_ids(conn, limit, user_id=user_id)
        report = ExtractReport()
        for episode_id in pending:
            try:
                with self.connect() as conn:
                    self._process_episode(conn, episode_id, report)
            except Exception:
                log.exception("抽取 episode %s 失败，保留为未抽取", episode_id)
        return report

    def _process_episode(self, conn, episode_id: UUID, report: ExtractReport) -> None:
        episode = store.lock_episode(conn, episode_id)
        if episode is None or episode.extracted_at is not None:
            return
        if episode.role == "user" and episode.content.strip():
            drafts = self.extractor.extract(episode.content)
            if drafts:
                scope = Scope(
                    user_id=episode.user_id,
                    agent_id=episode.agent_id,
                    session_id=episode.session_id,
                )
                applied = self.apply_fact_drafts(
                    conn,
                    scope,
                    drafts,
                    origin="extracted",
                    source_episode_ids=[episode.id],
                    refresh=True,
                )
                report.facts_added += len(applied.added)
                report.facts_superseded += len(applied.superseded)
                report.facts_skipped += len(applied.skipped)
            for draft in extract_intent_drafts(episode.content, ensure_utc(episode.occurred_at)):
                saved = self._save_intent(
                    conn,
                    Scope(
                        user_id=episode.user_id,
                        agent_id=episode.agent_id,
                        session_id=episode.session_id,
                    ),
                    draft,
                    [episode.id],
                    dedupe=True,
                )
                if saved is not None:
                    report.intents_added += 1
        store.mark_extracted(conn, episode.id)
        report.episodes_seen += 1

    def summarize_idle_sessions(
        self,
        *,
        days: int = 3,
        now: datetime | None = None,
        limit: int = 2,
        user_id: str | None = None,
    ) -> dict:
        """Compress conversations that have been quiet for `days` into one digest fact.

        There is no "session end" event in DSH, so staleness is the trigger: a
        session whose newest episode is older than the cutoff is treated as over.
        The digest is stored as a fact so it stays recallable from other sessions
        (L0 is session-scoped and could never surface it).
        """
        moment = ensure_utc(now)
        cutoff = moment - timedelta(days=max(1, int(days)))
        done: list[dict] = []
        with self.connect() as conn:
            candidates = store.idle_sessions(conn, cutoff, limit=limit, user_id=user_id)
        for row in candidates:
            session_id = row["session_id"]
            scope = Scope(user_id=row["user_id"], agent_id=row["agent_id"], session_id=session_id)
            with self.connect() as conn:
                if store.has_session_summary(conn, session_id):
                    continue
                episodes = store.list_episodes(conn, scope, limit=200)
                if not episodes:
                    continue
                # One-line sessions ("1", "hello") are not worth a digest; they would
                # only add noise to L1 forever.
                if len(episodes) < 2 or sum(len(item.content) for item in episodes) < 60:
                    continue
                transcript = "\n".join(f"{item.role}: {item.content[:400]}" for item in episodes)[:12000]
                summary = (self.extractor.summarize(transcript) or "").strip()
                if not summary:
                    continue
                # L1 renders each fact as "- {content}"; keep the digest single-line
                # so bullets do not double up and every point stays visible.
                points = [line.lstrip("-• ").strip() for line in summary.splitlines() if line.strip()]
                content = "；".join(points)[:900]
                if not content:
                    continue
                draft = FactDraft(
                    subject="会话",
                    predicate="session_summary",
                    # object must be unique per session: (subject, predicate, object)
                    # is the dedupe key, so a bare date would collapse every digest.
                    object=f"{str(row['last_at'])[:10]}#{session_id[-8:]}",
                    content=content,
                    importance=0.45,
                    profile_label="notes",
                )
                self.apply_fact_drafts(
                    conn,
                    scope,
                    [draft],
                    # origin is constrained to explicit/extracted by the schema;
                    # the digest is identified by metadata.kind instead.
                    origin="extracted",
                    source_episode_ids=[item.id for item in episodes[-5:]],
                    # valid_from is the session's last activity, not "now": using a
                    # shifted clock would hide the digest from present-day recall.
                    at=row["last_at"],
                    refresh=False,
                    metadata={"kind": "session_summary", "session_id": session_id},
                )
            done.append({"session_id": session_id, "episodes": len(episodes), "summary": summary})
            log.info("会话纪要：%s (%s 条)", session_id, len(episodes))
        return {"summarized": done, "days": int(days), "cutoff": cutoff.isoformat()}

    def list_facts(self, scope: Scope, *, include_inactive: bool = False) -> list[Fact]:
        self._check_scope(scope)
        with self.connect() as conn:
            return store.list_facts(conn, scope, include_inactive=include_inactive, visible=False, limit=500)

    def list_episodes(self, scope: Scope) -> list[Episode]:
        self._check_scope(scope, require_session=False)
        with self.connect() as conn:
            return store.list_episodes(conn, scope)

    def list_entities(self, user_id: str):
        if not user_id.strip():
            raise MemoryError("user_id 不能为空", 400)
        with self.connect() as conn:
            return store.list_entities(conn, user_id)

    def create_intent(
        self,
        scope: Scope,
        title: str,
        *,
        description: str = "",
        due_at: datetime | None = None,
        priority: int = 0,
        metadata: dict | None = None,
        source_episode_ids: list[UUID] | None = None,
        cron: str | None = None,
    ) -> Intent:
        self._check_scope(scope)
        self._check_text(title, "title")
        if cron is not None and not _valid_cron(cron):
            raise MemoryError("cron 需为 5 段（分 时 日 月 周），每段是 * 或整数", 400)
        draft = IntentDraft(
            title=" ".join(title.split())[:120],
            description=(description or "")[:2000],
            due_at=None if cron else (ensure_utc(due_at) if due_at else None),
            priority=int(priority),
            explicit=True,
        )
        extra = dict(metadata or {})
        with self.connect() as conn:
            saved = self._save_intent(
                conn,
                scope,
                draft,
                list(source_episode_ids or []),
                dedupe=False,
                metadata=extra,
                cron=cron,
            )
        if saved is None:
            raise MemoryError("待办没有写入", 500)
        return saved

    def list_intents(self, scope: Scope, status: str | None = None) -> list[Intent]:
        self._check_scope(scope)
        if status and status not in {"open", "in_progress", "done", "cancelled"}:
            raise MemoryError("status 无效", 400)
        with self.connect() as conn:
            return store.list_intents(conn, scope, status=status)

    def complete_intent(self, scope: Scope, intent_id: UUID) -> Intent:
        return self._close_intent(scope, intent_id, "done")

    def cancel_intent(self, scope: Scope, intent_id: UUID) -> Intent:
        return self._close_intent(scope, intent_id, "cancelled")

    def reschedule_intent(self, scope: Scope, intent_id: UUID, due_at: datetime) -> Intent:
        self._check_scope(scope)
        moment = ensure_utc(due_at)
        with self.connect() as conn:
            intent = self._owned_intent(conn, scope, intent_id)
            if intent.status in {"done", "cancelled"}:
                raise MemoryError("已结束的待办不能改期", 400)
            metadata = dict(intent.metadata)
            metadata.pop("acted_at", None)
            metadata.pop("last_action", None)
            updated = store.reschedule_once_trigger(conn, intent.id, moment, metadata)
            store.audit(
                conn,
                scope.user_id,
                "reschedule_intent",
                "intent",
                intent.id,
                {"due_at": moment.isoformat()},
            )
            return updated

    def tick(self, *, now: datetime | None = None, user_id: str | None = None, limit: int = 50) -> TickReport:
        moment = ensure_utc(now)
        limit = max(1, min(200, int(limit)))
        with self.connect() as conn:
            due = store.due_once_triggers(conn, moment, user_id, limit)
            cron_rows = store.due_cron_triggers(conn, moment, user_id, limit)
            bare = store.due_intents_without_trigger(conn, moment, user_id, limit)
            day_start = moment.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
            stale = store.stale_cron_triggers(conn, day_start, user_id, limit)
        queued: list[tuple[Intent, Trigger | None]] = list(due)
        seen = {intent.id for intent, _trigger in queued}
        for intent, trigger in cron_rows:
            if intent.id in seen or not trigger.expression or not cron_matches(trigger.expression, moment):
                continue
            queued.append((intent, trigger))
            seen.add(intent.id)
        # Catch-up: a daily job whose minute passed while the machine slept would
        # otherwise be skipped for the whole day. Fire at most once per day.
        for intent, trigger in stale:
            if intent.id in seen or not trigger.expression:
                continue
            if last_occurrence_today(trigger.expression, moment) is None:
                continue
            queued.append((intent, trigger))
            seen.add(intent.id)
        for intent in bare:
            if intent.id in seen:
                continue
            queued.append((intent, None))
            seen.add(intent.id)
        fired: list[TickFire] = []
        for intent, trigger in queued[:limit]:
            fired.append(self._fire_intent(intent, trigger, moment))
        # Ride along on the heartbeat: compress conversations that went quiet.
        # One per tick keeps the LLM cost bounded; failures must not break tick.
        try:
            self.summarize_idle_sessions(now=moment, limit=1)
        except Exception:
            log.warning("会话纪要扫描失败", exc_info=True)
        return TickReport(fired=fired)

    def _close_intent(self, scope: Scope, intent_id: UUID, status: str) -> Intent:
        self._check_scope(scope)
        with self.connect() as conn:
            intent = self._owned_intent(conn, scope, intent_id)
            if intent.status == status:
                return intent
            if intent.status in {"done", "cancelled"}:
                raise MemoryError("待办已经结束", 400)
            updated = store.set_intent_status(conn, intent.id, status)
            store.disable_intent_triggers(conn, intent.id)
            store.audit(
                conn,
                scope.user_id,
                "complete_intent" if status == "done" else "cancel_intent",
                "intent",
                intent.id,
                {"status": status},
            )
            return store.get_intent(conn, updated.id) or updated

    def _owned_intent(self, conn, scope: Scope, intent_id: UUID) -> Intent:
        intent = store.get_intent(conn, intent_id)
        if intent is None or intent.user_id != scope.user_id:
            raise MemoryError("待办不存在", 404)
        if scope.agent_id and intent.agent_id not in {"", scope.agent_id}:
            raise MemoryError("待办不存在", 404)
        return intent

    def _save_intent(
        self,
        conn,
        scope: Scope,
        draft: IntentDraft,
        source_episode_ids: list[UUID],
        *,
        dedupe: bool,
        metadata: dict | None = None,
        cron: str | None = None,
    ) -> Intent | None:
        title = " ".join(draft.title.split())[:120]
        if not title:
            return None
        if dedupe and store.find_open_intent_by_title(conn, scope, title):
            return None
        merged = dict(metadata or {})
        description = (draft.description or title)[:2000]
        merged["policy"] = policy_for(title, description, merged.get("mode"))
        merged.setdefault("origin", "explicit" if draft.explicit else "extracted")
        if merged["policy"] == "propose":
            merged["proposed_action"] = proposed_action_text(title)
        intent = store.insert_intent(
            conn,
            scope=scope,
            title=title,
            description=description,
            due_at=draft.due_at,
            priority=int(draft.priority),
            source_episode_ids=source_episode_ids,
            metadata=merged,
        )
        if cron:
            store.insert_trigger(conn, intent.id, "cron", cron, None)
        elif draft.due_at is not None:
            store.insert_trigger(conn, intent.id, "once_at", None, draft.due_at)
        store.audit(
            conn,
            scope.user_id,
            "create_intent",
            "intent",
            intent.id,
            {"title": title, "policy": merged["policy"], "due_at": draft.due_at.isoformat() if draft.due_at else None},
        )
        return store.get_intent(conn, intent.id)

    def _act_halted(self) -> bool:
        """Emergency stop for autonomous execution.

        Any of these turns every `act` intent back into a plain notification:
        env MEMORY_ACT_HALT=1, or a file at $DSH_HOME/ACT_HALT (default ~/.dsh).
        """
        if (os.getenv("MEMORY_ACT_HALT") or "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
        home = os.getenv("DSH_HOME") or str(Path.home() / ".dsh")
        return (Path(home) / "ACT_HALT").exists()

    def _fire_intent(self, intent: Intent, trigger: Trigger | None, moment: datetime) -> TickFire:
        session_id = intent.session_id or f"agenda-{intent.id}"
        scope = Scope(user_id=intent.user_id, agent_id=intent.agent_id, session_id=session_id)
        recalled = self.recall(scope, intent.title, budget=1200)
        mode = str(intent.metadata.get("mode") or "").strip().lower() or None
        if mode == "act" and self._act_halted():
            log.warning("ACT_HALT 生效：待办 %s 降级为提醒", intent.id)
            mode = "notify"
        policy = policy_for(intent.title, intent.description, mode)
        proposed = proposed_action_text(intent.title) if policy == "propose" else None
        content = render_action_episode(
            title=intent.title,
            due_at=intent.due_at,
            policy=policy,
            context=recalled.prompt,
            proposed=proposed,
        )
        episode = self.append_episode(
            scope,
            "assistant",
            content,
            metadata={
                "kind": "intent_result",
                "intent_id": str(intent.id),
                "action": policy,
                "proposed_action": proposed,
            },
            occurred_at=moment,
            mark_extracted=True,
        )
        metadata = dict(intent.metadata)
        metadata["acted_at"] = moment.isoformat()
        metadata["last_action"] = policy
        metadata["policy"] = policy
        if proposed:
            metadata["proposed_action"] = proposed
        status = intent.status
        if policy == "notify" and status == "open":
            status = "in_progress"
        elif policy == "act" and (trigger is None or trigger.type != "cron"):
            # One-shot autonomous runs retire themselves. A recurring (cron) task
            # must stay alive, otherwise the second run could never happen.
            status = "done"
        with self.connect() as conn:
            if trigger is not None:
                store.mark_trigger_fired(conn, trigger.id, moment, disable=trigger.type != "cron")
            store.update_intent_progress(conn, intent.id, status, metadata)
            store.audit(
                conn,
                intent.user_id,
                "tick_notify" if policy == "notify" else "tick_propose",
                "intent",
                intent.id,
                {"episode_id": str(episode.id), "action": policy, "proposed_action": proposed},
            )
        return TickFire(
            intent_id=intent.id,
            user_id=intent.user_id,
            title=intent.title,
            action=policy,
            episode_id=episode.id,
            proposed_action=proposed,
            content=content,
            session_id=intent.session_id,
        )

    def purge_user(self, user_id: str) -> None:
        with self.connect() as conn:
            store.purge_user(conn, user_id)

    def _query_vector(self, query: str) -> list[float] | None:
        if not self.embedder.available:
            return None
        vectors = self.embedder.embed([query])
        if not vectors:
            return None
        return vectors[0]

    def _check_scope(self, scope: Scope, *, require_session: bool = False) -> None:
        if not scope.user_id or not scope.user_id.strip():
            raise MemoryError("user_id 不能为空", 400)
        if len(scope.user_id) > 200 or len(scope.agent_id or "") > 200:
            raise MemoryError("user_id 或 agent_id 过长", 400)
        if require_session and not (scope.session_id and scope.session_id.strip()):
            raise MemoryError("session_id 不能为空", 400)
        if scope.session_id and len(scope.session_id) > 200:
            raise MemoryError("session_id 过长", 400)

    def _check_text(self, text: str, field: str) -> None:
        if text is None or not str(text).strip():
            raise MemoryError(f"{field} 不能为空", 400)
        if len(text) > 20000:
            raise MemoryError(f"{field} 过长", 400)


def agent_can_see(fact_agent: str, scope_agent: str) -> bool:
    fact_agent = fact_agent or ""
    scope_agent = scope_agent or ""
    if scope_agent == "":
        return fact_agent == ""
    return fact_agent in ("", scope_agent)


def entity_pairs(draft) -> list[tuple[str, str | None]]:
    pairs: list[tuple[str, str | None]] = []
    for entity in draft.entities:
        name = clean_object(entity.name)
        if name:
            kind = (entity.kind or "").strip() or None
            pairs.append((name[:80], kind[:40] if kind else None))
    if norm(draft.subject) not in _GENERIC_SUBJECTS:
        pairs.append((draft.subject[:80], "person"))
    kind = _ENTITY_KINDS.get(draft.predicate)
    if kind and norm(draft.object) not in _GENERIC_SUBJECTS:
        pairs.append((draft.object[:80], kind))
    deduped: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for name, kind in pairs:
        key = norm(name)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append((name, kind))
    return deduped


def profile_view(scope: Scope, facts: list[Fact], moment: datetime) -> list[ProfileBlock]:
    rendered = render_profile_from_facts(facts)
    blocks: list[ProfileBlock] = []
    for label in MANAGED_LABELS:
        content = rendered.get(label)
        if not content:
            continue
        blocks.append(
            ProfileBlock(
                id=uuid5(NAMESPACE_URL, f"memory:{scope.user_id}:{scope.agent_id}:{label}"),
                user_id=scope.user_id,
                agent_id=scope.agent_id or "",
                label=label,
                content=content,
                token_limit=200,
                version=0,
                updated_at=moment,
            )
        )
    return blocks
