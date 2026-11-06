import io
import json

import httpx
from fastapi.testclient import TestClient

from memory_kernel.kernel_client import KernelClient
from memory_kernel.mcp_server import (
    call_tool,
    create_mcp_app,
    decode_message,
    encode_message,
    handle_rpc,
    serve_stdio,
)


def kernel(handler) -> KernelClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="http://kernel.test")
    return KernelClient("http://kernel.test", user_id="lin", http=http)


def test_recall_and_remember_hit_kernel_http():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, json.loads(request.content)))
        if request.url.path == "/recall":
            return httpx.Response(200, json={"prompt": "【用户画像 L2】\n住在成都", "tokens": {"total": 12}})
        return httpx.Response(200, json={"added": [{"content": "用户住在成都"}], "skipped": []})

    client = kernel(handler)
    recalled = call_tool(client, "recall", {"query": "住在哪", "budget": 400})
    assert "住在成都" in recalled["content"][0]["text"]
    remembered = call_tool(client, "remember", {"text": "我住在成都"})
    assert "added" in remembered["content"][0]["text"]
    assert seen[0][1] == "/recall"
    assert seen[0][2]["user_id"] == "lin"
    assert seen[0][2]["budget"] == 400
    assert seen[1][1] == "/remember"


def test_tools_list_and_unknown_tool():
    client = kernel(lambda request: httpx.Response(200, json={}))
    listed = handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, client)
    names = [tool["name"] for tool in listed["result"]["tools"]]
    assert names == ["recall", "remember", "append_episode", "get_profile", "list_intents", "create_intent"]
    missing = call_tool(client, "tick", {})
    assert missing["isError"] is True
    failed = call_tool(client, "recall", {})
    assert failed["isError"] is True


def test_kernel_error_is_a_tool_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "无法连接 Postgres"})

    result = call_tool(kernel(handler), "get_profile", {})
    assert result["isError"] is True
    assert "Postgres" in result["content"][0]["text"]


def test_stdio_frame_roundtrip_lists_tools():
    client = kernel(lambda request: httpx.Response(200, json={"prompt": "ok", "tokens": {"total": 1}}))
    request = encode_message({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "recall", "arguments": {"query": "hi"}}})
    stdin = io.BytesIO(request)
    stdout = io.BytesIO()
    serve_stdio(client, stdin, stdout)
    message, rest = decode_message(stdout.getvalue())
    assert rest == b""
    assert message["id"] == 7
    assert "ok" in message["result"]["content"][0]["text"]


def test_http_transport_returns_json_and_sse():
    client = kernel(lambda request: httpx.Response(200, json=[]))
    app = create_mcp_app(client)
    with TestClient(app) as http:
        health = http.get("/health")
        assert health.status_code == 200
        listed = http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"accept": "application/json"},
        )
        assert listed.status_code == 200
        assert listed.json()["result"]["tools"][0]["name"] == "recall"
        sse = http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
            headers={"accept": "application/json, text/event-stream"},
        )
        assert sse.status_code == 200
        assert "event: message" in sse.text
        assert '"id":2' in sse.text.replace(" ", "")
        assert http.get("/mcp").status_code == 405
        note = http.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert note.status_code == 202
