"""MCP server over the memory-kernel HTTP API.

stdio is the DSH default. Streamable HTTP is a stateless POST /mcp that
answers with JSON or a single SSE event. Tool names stay unprefixed; DSH
publishes them as mcp__memory_kernel__<tool>.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from memory_kernel import __version__
from memory_kernel.kernel_client import KernelApiError, KernelClient

PROTOCOL_VERSION = "2025-03-26"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "recall",
        "description": "Search memory and return a token-budgeted prompt (L2 profile, relevant L1 facts, recent L0).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "budget": {"type": "integer", "minimum": 1, "maximum": 20000},
                "user_id": {"type": "string"},
                "session_id": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "remember",
        "description": "Write an explicit memory. The kernel stores an L1 fact and refreshes L2.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "importance": {"type": "number", "minimum": 0, "maximum": 1},
                "user_id": {"type": "string"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "append_episode",
        "description": "Append one raw L0 episode. Prefer the Cordis plugin for automatic turn persistence so this is not called for every utterance.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "role": {"type": "string", "enum": ["user", "assistant", "system", "tool"]},
                "session_id": {"type": "string"},
                "user_id": {"type": "string"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "get_profile",
        "description": "Read L2 profile blocks for the user.",
        "inputSchema": {
            "type": "object",
            "properties": {"user_id": {"type": "string"}},
        },
    },
    {
        "name": "list_intents",
        "description": "List intents. This does not fire triggers; the kernel tick scheduler does that.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["open", "in_progress", "done", "cancelled"]},
                "user_id": {"type": "string"},
            },
        },
    },
    {
        "name": "create_intent",
        "description": "Create an intent. A due_at value also creates a once_at trigger on the kernel.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "due_at": {"type": "string", "description": "ISO-8601 timestamp"},
                "priority": {"type": "integer"},
                "session_id": {"type": "string"},
                "user_id": {"type": "string"},
            },
            "required": ["title"],
        },
    },
]

ToolHandler = Callable[[KernelClient, dict[str, Any]], Any]


def _args(arguments: dict[str, Any] | None) -> dict[str, Any]:
    if arguments is None:
        return {}
    if not isinstance(arguments, dict):
        raise KernelApiError(400, "arguments 必须是对象")
    return arguments


def tool_recall(client: KernelClient, arguments: dict[str, Any]) -> Any:
    args = _args(arguments)
    query = str(args.get("query") or "").strip()
    if not query:
        raise KernelApiError(400, "query 不能为空")
    return client.recall(
        query,
        budget=int(args.get("budget") or 1200),
        user_id=args.get("user_id"),
        session_id=args.get("session_id"),
    )


def tool_remember(client: KernelClient, arguments: dict[str, Any]) -> Any:
    args = _args(arguments)
    text = str(args.get("text") or "").strip()
    if not text:
        raise KernelApiError(400, "text 不能为空")
    importance = args.get("importance")
    return client.remember(
        text,
        importance=float(importance) if importance is not None else None,
        user_id=args.get("user_id"),
    )


def tool_append_episode(client: KernelClient, arguments: dict[str, Any]) -> Any:
    args = _args(arguments)
    content = str(args.get("content") or "").strip()
    if not content:
        raise KernelApiError(400, "content 不能为空")
    return client.append_episode(
        content,
        role=str(args.get("role") or "user"),
        session_id=args.get("session_id"),
        user_id=args.get("user_id"),
    )


def tool_get_profile(client: KernelClient, arguments: dict[str, Any]) -> Any:
    args = _args(arguments)
    return client.get_profile(user_id=args.get("user_id"))


def tool_list_intents(client: KernelClient, arguments: dict[str, Any]) -> Any:
    args = _args(arguments)
    return client.list_intents(status=args.get("status"), user_id=args.get("user_id"))


def tool_create_intent(client: KernelClient, arguments: dict[str, Any]) -> Any:
    args = _args(arguments)
    title = str(args.get("title") or "").strip()
    if not title:
        raise KernelApiError(400, "title 不能为空")
    return client.create_intent(
        title,
        description=str(args.get("description") or ""),
        due_at=args.get("due_at"),
        priority=int(args.get("priority") or 0),
        session_id=args.get("session_id"),
        user_id=args.get("user_id"),
    )


HANDLERS: dict[str, ToolHandler] = {
    "recall": tool_recall,
    "remember": tool_remember,
    "append_episode": tool_append_episode,
    "get_profile": tool_get_profile,
    "list_intents": tool_list_intents,
    "create_intent": tool_create_intent,
}


def call_tool(client: KernelClient, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    handler = HANDLERS.get(name)
    if handler is None:
        return _tool_result(f"未知工具: {name}", is_error=True)
    try:
        payload = handler(client, arguments or {})
    except KernelApiError as exc:
        return _tool_result(exc.detail, is_error=True)
    except Exception as exc:
        return _tool_result(str(exc), is_error=True)
    return _tool_result(json.dumps(payload, ensure_ascii=False, default=str))


def _tool_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def handle_rpc(message: dict[str, Any], client: KernelClient) -> dict[str, Any] | None:
    method = message.get("method")
    msg_id = message.get("id")
    if isinstance(method, str) and method.startswith("notifications/"):
        return None
    if msg_id is None and method != "initialize":
        return None

    def ok(result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def fail(code: int, text: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": text}}

    if method == "initialize":
        return ok(
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "memory-kernel", "version": __version__},
            }
        )
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        params = message.get("params") or {}
        if not isinstance(params, dict) or not params.get("name"):
            return fail(-32602, "tools/call 需要 name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return fail(-32602, "arguments 必须是对象")
        return ok(call_tool(client, str(params["name"]), arguments))
    return fail(-32601, f"方法不存在: {method}")


def handle_payload(payload: Any, client: KernelClient) -> Any:
    if isinstance(payload, list):
        return [item for item in (handle_rpc(message, client) for message in payload) if item is not None]
    if isinstance(payload, dict):
        return handle_rpc(payload, client)
    raise KernelApiError(400, "JSON-RPC 消息必须是对象或数组")


def encode_message(payload: dict[str, Any] | list[Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


def decode_message(buffer: bytes) -> tuple[Any | None, bytes]:
    """Pop one framed JSON-RPC message. Returns (None, buffer) when incomplete."""
    separator = b"\r\n\r\n"
    index = buffer.find(separator)
    if index < 0:
        return None, buffer
    header = buffer[:index].decode("ascii", errors="replace")
    length = 0
    for line in header.split("\r\n"):
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip())
    start = index + len(separator)
    if len(buffer) < start + length:
        return None, buffer
    body = buffer[start : start + length]
    return json.loads(body.decode("utf-8")), buffer[start + length :]


def serve_stdio(client: KernelClient, stdin: Any | None = None, stdout: Any | None = None) -> None:
    reader = stdin if stdin is not None else sys.stdin.buffer
    writer = stdout if stdout is not None else sys.stdout.buffer
    pending = b""
    while True:
        chunk = reader.read(1) if not hasattr(reader, "read1") else reader.read(4096)
        if not chunk:
            if not pending:
                break
            # A short read at EOF still tries to finish a buffered frame.
            chunk = b""
        pending += chunk
        while True:
            message, pending = decode_message(pending)
            if message is None:
                break
            response = handle_payload(message, client)
            if response is not None:
                writer.write(encode_message(response))
                flush = getattr(writer, "flush", None)
                if flush:
                    flush()
        if not chunk:
            break


def create_mcp_app(client: KernelClient):
    app = FastAPI(title="memory-kernel MCP", version=__version__)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "kernel": client.base_url}

    @app.get("/mcp")
    def mcp_get() -> JSONResponse:
        return JSONResponse(
            status_code=405,
            content={"jsonrpc": "2.0", "error": {"code": -32000, "message": "server does not offer a standalone SSE stream"}},
        )

    @app.post("/mcp")
    async def mcp_post(request: Request):
        try:
            payload = await request.json()
            result = handle_payload(payload, client)
        except Exception as exc:
            return JSONResponse(
                status_code=400,
                content={"jsonrpc": "2.0", "error": {"code": -32700, "message": str(exc)}},
            )
        if result is None:
            return Response(status_code=202)
        accept = request.headers.get("accept", "")
        if "text/event-stream" in accept:
            data = json.dumps(result, ensure_ascii=False)
            body = f"event: message\ndata: {data}\n\n"
            return Response(content=body, media_type="text/event-stream")
        return JSONResponse(result)

    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="memory-kernel-mcp", description="MCP adapter for the memory kernel")
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8742)
    parser.add_argument("--kernel-url", default=None)
    parser.add_argument("--user-id", default=None)
    args = parser.parse_args(argv)
    client = KernelClient(args.kernel_url, user_id=args.user_id)
    if args.transport == "stdio":
        serve_stdio(client)
        return
    import uvicorn

    uvicorn.run(create_mcp_app(client), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
