from memory_kernel.service import MemoryService


def test_remember_writes_fact_profile_and_tsv(svc: MemoryService, users):
    scope = users()
    result = svc.remember(scope, "我叫林夏", importance=0.9)
    assert len(result.added) == 1
    fact = result.added[0]
    assert fact.origin == "explicit"
    assert fact.predicate == "name"
    assert fact.object == "林夏"
    assert fact.valid_to is None
    assert any(block.label == "identity" and "林夏" in block.content for block in result.profile)

    again = svc.remember(scope, "我叫林夏")
    assert again.added == []
    assert len(again.skipped) == 1

    with svc.connect() as conn:
        row = conn.execute(
            "SELECT tsv::text AS tsv, embedding IS NULL AS no_embedding FROM facts WHERE id = %s",
            (fact.id,),
        ).fetchone()
    assert row["tsv"]
    assert row["no_embedding"] is True

    entities = svc.list_entities(scope.user_id)
    assert any(entity.name == "林夏" and entity.kind == "person" for entity in entities)


def test_soft_forget_hides_fact_and_refreshes_profile(svc: MemoryService, users):
    scope = users()
    created = svc.remember(scope, "我喜欢红茶")
    fact_id = created.added[0].id
    forgotten = svc.forget(scope, fact_id=fact_id)
    assert forgotten.hard is False
    assert forgotten.forgotten[0].valid_to is not None

    stored = svc.list_facts(scope, include_inactive=True)
    assert stored[0].valid_to is not None
    assert stored[0].superseded_by is None
    recalled = svc.recall(scope, "红茶")
    assert all(hit.fact.id != fact_id for hit in recalled.hits)
    assert all("红茶" not in block.content for block in svc.get_profile(scope))


def test_hard_forget_removes_row(svc: MemoryService, users):
    scope = users()
    created = svc.remember(scope, "我喜欢绿茶")
    fact_id = created.added[0].id
    svc.forget(scope, fact_id=fact_id, hard=True)
    assert svc.list_facts(scope, include_inactive=True) == []
