from fastapi.testclient import TestClient

from memory_kernel.api import app


def test_http_roundtrip(svc, users):
    scope = users()
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["extractor"] == "mock"

        created = client.post(
            "/episodes",
            json={
                "user_id": scope.user_id,
                "session_id": scope.session_id,
                "role": "user",
                "content": "我住在杭州。",
            },
        )
        assert created.status_code == 200
        assert created.json()["extracted_at"] is None

        remembered = client.post(
            "/remember",
            json={"user_id": scope.user_id, "text": "我对芒果过敏", "importance": 0.9},
        )
        assert remembered.status_code == 200
        assert remembered.json()["added"][0]["origin"] == "explicit"

        extracted = client.post("/extract", params={"user_id": scope.user_id})
        assert extracted.status_code == 200
        assert extracted.json()["facts_added"] >= 1

        recalled = client.post(
            "/recall",
            json={"user_id": scope.user_id, "session_id": scope.session_id, "query": "住在哪里", "budget": 1000},
        )
        assert recalled.status_code == 200
        body = recalled.json()
        assert body["tokens"]["total"] <= 1000
        assert "杭州" in body["prompt"]
        assert "芒果" in body["prompt"]

        missing = client.post("/forget", json={"user_id": scope.user_id})
        assert missing.status_code == 400


def test_episodes_limit_is_tunable(svc, users):
    """/episodes used to be stuck at 50, which hid older rows while debugging."""
    scope = users()
    for index in range(5):
        svc.append_episode(scope, "user", f"第{index}条")

    assert len(svc.list_episodes(scope)) == 5
    assert len(svc.list_episodes(scope, limit=2)) == 2