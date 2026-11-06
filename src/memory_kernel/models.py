from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

Origin = Literal["explicit", "extracted"]
Role = Literal["user", "assistant", "system", "tool"]


class Scope(BaseModel):
    user_id: str
    agent_id: str = ""
    session_id: str | None = None


class EntityDraft(BaseModel):
    name: str
    kind: str | None = None


class FactDraft(BaseModel):
    subject: str
    predicate: str
    object: str
    content: str
    importance: float = 0.5
    profile_label: str = "notes"
    entities: list[EntityDraft] = Field(default_factory=list)


class Episode(BaseModel):
    id: UUID
    user_id: str
    agent_id: str
    session_id: str
    role: str
    content: str
    metadata: dict = Field(default_factory=dict)
    occurred_at: datetime
    ingested_at: datetime
    extracted_at: datetime | None = None


class Fact(BaseModel):
    id: UUID
    user_id: str
    agent_id: str
    subject: str
    predicate: str
    object: str
    content: str
    valid_from: datetime
    valid_to: datetime | None = None
    superseded_by: UUID | None = None
    source_episode_ids: list[UUID] = Field(default_factory=list)
    origin: str
    importance: float
    metadata: dict = Field(default_factory=dict)
    created_at: datetime
    has_embedding: bool = False


class ProfileBlock(BaseModel):
    id: UUID
    user_id: str
    agent_id: str
    label: str
    content: str
    token_limit: int
    version: int
    updated_at: datetime


class Entity(BaseModel):
    id: UUID
    user_id: str
    name: str
    kind: str | None = None
    aliases: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class ApplyResult(BaseModel):
    added: list[Fact] = Field(default_factory=list)
    superseded: list[Fact] = Field(default_factory=list)
    skipped: list[Fact] = Field(default_factory=list)


class RememberResult(BaseModel):
    added: list[Fact]
    superseded: list[Fact]
    skipped: list[Fact]
    profile: list[ProfileBlock]


class RecallHit(BaseModel):
    score: float
    relevance: float
    fact: Fact


class RecallResult(BaseModel):
    query: str
    as_of: datetime
    budget: int
    tokens: dict[str, int]
    profile: list[ProfileBlock]
    hits: list[RecallHit]
    episodes: list[Episode]
    prompt: str


class ForgetResult(BaseModel):
    forgotten: list[Fact]
    hard: bool


class ExtractReport(BaseModel):
    episodes_seen: int = 0
    facts_added: int = 0
    facts_superseded: int = 0
    facts_skipped: int = 0
    intents_added: int = 0


IntentStatus = Literal["open", "in_progress", "done", "cancelled"]
TriggerType = Literal["cron", "once_at", "on_event"]


class Trigger(BaseModel):
    id: UUID
    intent_id: UUID
    type: str
    expression: str | None = None
    at: datetime | None = None
    enabled: bool = True
    last_fired_at: datetime | None = None
    created_at: datetime


class Intent(BaseModel):
    id: UUID
    user_id: str
    agent_id: str = ""
    session_id: str | None = None
    title: str
    description: str = ""
    status: str
    due_at: datetime | None = None
    priority: int = 0
    source_episode_ids: list[UUID] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    triggers: list[Trigger] = Field(default_factory=list)


class IntentDraft(BaseModel):
    title: str
    description: str = ""
    due_at: datetime | None = None
    priority: int = 0
    explicit: bool = False


class IntentIn(BaseModel):
    user_id: str
    agent_id: str = ""
    session_id: str | None = None
    title: str
    description: str = ""
    due_at: datetime | None = None
    priority: int = 0
    metadata: dict = Field(default_factory=dict)


class TickFire(BaseModel):
    intent_id: UUID
    user_id: str
    title: str
    action: str
    episode_id: UUID
    proposed_action: str | None = None
    content: str
    # The conversation that owns the reminder. Wake delivery needs it: the desktop
    # plugin can only route to a live agent by session id.
    session_id: str | None = None


class TickReport(BaseModel):
    fired: list[TickFire] = Field(default_factory=list)


class EpisodeIn(BaseModel):
    user_id: str
    agent_id: str = ""
    session_id: str
    role: Role
    content: str
    metadata: dict = Field(default_factory=dict)
    occurred_at: datetime | None = None


class RememberIn(BaseModel):
    user_id: str
    agent_id: str = ""
    text: str
    importance: float | None = None
    session_id: str | None = None


class RecallIn(BaseModel):
    user_id: str
    agent_id: str = ""
    session_id: str | None = None
    query: str = ""
    budget: int = 2500
    as_of: datetime | None = None


class ForgetIn(BaseModel):
    user_id: str
    agent_id: str = ""
    fact_id: UUID | None = None
    query: str | None = None
    hard: bool = False


class ProfileUpdateIn(BaseModel):
    user_id: str
    agent_id: str = ""
    label: str
    content: str
    token_limit: int = 200
