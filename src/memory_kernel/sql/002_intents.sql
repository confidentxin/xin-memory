-- 可执行记忆：待办 intents 与触发器 triggers。
-- 幂等，可在已有一期库上重复执行。

CREATE TABLE IF NOT EXISTS intents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL DEFAULT '',
    session_id TEXT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'in_progress', 'done', 'cancelled')),
    due_at TIMESTAMPTZ,
    priority INTEGER NOT NULL DEFAULT 0,
    source_episode_ids UUID[] NOT NULL DEFAULT '{}',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS intents_user_status_idx
    ON intents (user_id, agent_id, status, due_at);

CREATE TABLE IF NOT EXISTS triggers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    intent_id UUID NOT NULL REFERENCES intents (id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK (type IN ('cron', 'once_at', 'on_event')),
    expression TEXT,
    at TIMESTAMPTZ,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    last_fired_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (type = 'once_at' AND at IS NOT NULL)
        OR (type = 'cron' AND expression IS NOT NULL)
        OR (type = 'on_event' AND expression IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS triggers_once_due_idx
    ON triggers (at)
    WHERE enabled = TRUE AND type = 'once_at';
