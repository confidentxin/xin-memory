from datetime import datetime, timezone

from memory_kernel.models import Scope
from memory_kernel.service import MemoryService


def test_recall_isolates_users(svc: MemoryService, users):
    left = users()
    right = users()
    svc.remember(left, "我喜欢手冲咖啡")
    svc.remember(right, "我喜欢红茶")

    recalled = svc.recall(left, "手冲咖啡", budget=800)
    text = "\n".join(hit.fact.content for hit in recalled.hits)
    assert "手冲" in text
    assert "红茶" not in recalled.prompt
    assert all(hit.fact.user_id == left.user_id for hit in recalled.hits)

    outsider = svc.recall(Scope(user_id=right.user_id, session_id=left.session_id), "手冲咖啡")
    assert all("手冲" not in hit.fact.content for hit in outsider.hits)


def test_recall_isolates_agents(svc: MemoryService, users):
    alpha = users("alpha")
    beta = users("beta", user_id=alpha.user_id)
    svc.remember(alpha, "我住在杭州")
    svc.remember(beta, "我住在成都")

    beta_view = svc.recall(beta, "住在哪里")
    contents = [hit.fact.content for hit in beta_view.hits]
    assert any("成都" in content for content in contents)
    assert all("杭州" not in content for content in contents)

    global_view = svc.recall(Scope(user_id=alpha.user_id), "住在哪里")
    assert global_view.hits == []


def test_recall_as_of_and_recent_session(svc: MemoryService, users):
    scope = users()
    january = datetime(2026, 1, 1, tzinfo=timezone.utc)
    june = datetime(2026, 6, 1, tzinfo=timezone.utc)
    svc.remember(scope, "我住在杭州", at=january)
    svc.remember(scope, "我住在成都", at=june)
    svc.append_episode(scope, "user", "我下个月搬到成都。", occurred_at=june)

    early = svc.recall(scope, "住在哪里", as_of=datetime(2026, 3, 1, tzinfo=timezone.utc))
    early_text = "\n".join(hit.fact.content for hit in early.hits)
    assert "杭州" in early_text
    assert "成都" not in early_text
    location = next(block.content for block in early.profile if block.label == "location")
    assert "杭州" in location and "成都" not in location

    late = svc.recall(scope, "住在哪里", as_of=datetime(2026, 7, 1, tzinfo=timezone.utc))
    late_text = "\n".join(hit.fact.content for hit in late.hits)
    assert "成都" in late_text
    assert "杭州" not in late_text
    assert any("搬到成都" in episode.content for episode in late.episodes)
    assert late.tokens["total"] <= late.budget


def test_schema_has_core_tables(svc: MemoryService):
    with svc.connect() as conn:
        extensions = {row["extname"] for row in conn.execute("SELECT extname FROM pg_extension").fetchall()}
        tables = {
            row["tablename"]
            for row in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()
        }
        indexes = {
            row["indexname"]
            for row in conn.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'").fetchall()
        }
    assert {"vector", "pg_trgm"} <= extensions
    assert {"episodes", "facts", "entities", "profile_blocks", "memory_audit"} <= tables
    assert "relations" not in tables
    assert {"facts_tsv_idx", "facts_embedding_hnsw_idx", "facts_content_trgm_idx"} <= indexes
