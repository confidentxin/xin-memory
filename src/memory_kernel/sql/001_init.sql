-- 记忆内核一期 schema：L0 episodes / L1 facts / L2 profile_blocks
-- 另含 entities 与 memory_audit。relations 一期不做。
-- embedding 维度固定为 1536（OpenAI text-embedding-3-small）。无 key 时该列留空。

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS episodes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    content TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    extracted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS episodes_user_session_time_idx
    ON episodes (user_id, session_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS episodes_user_time_idx
    ON episodes (user_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS episodes_pending_idx
    ON episodes (ingested_at)
    WHERE extracted_at IS NULL;

CREATE TABLE IF NOT EXISTS entities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT,
    aliases TEXT[] NOT NULL DEFAULT '{}',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS facts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    "object" TEXT NOT NULL,
    content TEXT NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to TIMESTAMPTZ,
    superseded_by UUID REFERENCES facts (id) ON DELETE SET NULL,
    source_episode_ids UUID[] NOT NULL DEFAULT '{}',
    origin TEXT NOT NULL CHECK (origin IN ('explicit', 'extracted')),
    importance REAL NOT NULL DEFAULT 0.5 CHECK (importance >= 0 AND importance <= 1),
    embedding vector(1536),
    tsv tsvector GENERATED ALWAYS AS (
        to_tsvector(
            'simple',
            coalesce(subject, '') || ' ' || coalesce(predicate, '') || ' ' ||
            coalesce("object", '') || ' ' || coalesce(content, '')
        )
    ) STORED,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS facts_active_user_idx
    ON facts (user_id, agent_id, valid_from DESC)
    WHERE valid_to IS NULL;

CREATE INDEX IF NOT EXISTS facts_predicate_idx
    ON facts (user_id, agent_id, predicate)
    WHERE valid_to IS NULL;

CREATE INDEX IF NOT EXISTS facts_tsv_idx
    ON facts USING GIN (tsv);

CREATE INDEX IF NOT EXISTS facts_content_trgm_idx
    ON facts USING GIN (content gin_trgm_ops);

CREATE INDEX IF NOT EXISTS facts_embedding_hnsw_idx
    ON facts USING hnsw (embedding vector_cosine_ops)
    WHERE embedding IS NOT NULL;

CREATE TABLE IF NOT EXISTS profile_blocks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL,
    content TEXT NOT NULL,
    token_limit INTEGER NOT NULL DEFAULT 200 CHECK (token_limit > 0),
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, agent_id, label)
);

CREATE TABLE IF NOT EXISTS memory_audit (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id UUID,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS memory_audit_user_time_idx
    ON memory_audit (user_id, created_at DESC);
