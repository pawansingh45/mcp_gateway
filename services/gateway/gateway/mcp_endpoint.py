"""The gateway's own MCP endpoint: POST /mcp (Streamable HTTP, stateless JSON replies).

Auth: `Authorization: Bearer gw_...` - a per-user API key. The key identifies the
caller for authorisation and audit; it is NEVER forwarded to upstream servers
(upstreams only ever see the admin-configured credential for that upstream).

Stateless by design: no Mcp-Session-Id is issued, so any replica can serve any
request. Tool lists come from the cached catalog and tool calls are routed by
public name, so no per-client state is needed.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from flask import Blueprint, Response, current_app, jsonify, request

from .service import User

log = logging.getLogger("gateway.mcp")

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = ("Team MCP gateway. Tools from several servers are exposed under the name <server>__<tool>. "
                "Use '?mode=lazy' on the URL to get search_tools/execute_tool instead of the full tool list.")

bp = Blueprint("mcp", __name__)


def _ctx():
    return current_app.extensions["ctx"]


def _unauthorized() -> Response:
    return Response('{"error":"invalid_token"}', 401, mimetype="application/json",
                    headers={"WWW-Authenticate": 'Bearer realm="mcp-gateway"'})


def _origin_ok() -> bool:
    """Spec: servers MUST validate Origin to stop DNS-rebinding from a browser.
    Non-browser MCP clients send no Origin header at all."""
    origin = request.headers.get("Origin")
    if not origin:
        return True
    ctx = _ctx()
    allowed = {ctx.settings.public_url.rstrip("/"), *ctx.settings.allowed_origins}
    return origin.rstrip("/") in allowed or urlsplit(origin).netloc == request.host


def _bearer_user() -> User | None:
    scheme, _, secret = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not secret.strip():
        return None
    return _ctx().auth.user_for_key(secret.strip())


def _rpc_ok(msg_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _rpc_err(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle_rpc(user: User, msg: Any, mode: str) -> dict | None:
    service = _ctx().service
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc_err(None, -32600, "Invalid JSON-RPC request")
    if "method" not in msg or "id" not in msg:  # notification or client response
        return None
    msg_id, method = msg["id"], msg["method"]
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

    if method == "initialize":
        asked = params.get("protocolVersion")
        return _rpc_ok(msg_id, {
            "protocolVersion": asked if asked in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "mcp-gateway", "version": "1.0.0"},
            "instructions": INSTRUCTIONS})
    if method == "ping":
        return _rpc_ok(msg_id, {})
    if method == "tools/list":
        return _rpc_ok(msg_id, {"tools": service.list_tools(user, mode)})
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str):
            return _rpc_err(msg_id, -32602, "params.name is required")
        outcome = service.call_tool(user, name, params.get("arguments"), source="mcp")
        if outcome.unknown_tool:
            return _rpc_err(msg_id, -32602, f"Unknown tool: {name}")
        return _rpc_ok(msg_id, outcome.result)
    return _rpc_err(msg_id, -32601, f"Method not found: {method}")


@bp.post("/mcp")
def mcp_post():
    if not _origin_ok():
        return Response('{"error":"origin not allowed"}', 403, mimetype="application/json")
    user = _bearer_user()
    if user is None:
        log.info("rejected /mcp request: missing or invalid API key (ip=%s)", request.remote_addr)
        return _unauthorized()
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify(_rpc_err(None, -32700, "Parse error")), 400
    mode = "lazy" if request.args.get("mode") == "lazy" else "full"
    if isinstance(payload, list):
        replies = [r for r in (handle_rpc(user, m, mode) for m in payload[:50]) if r is not None]
        return (jsonify(replies), 200) if replies else ("", 202)
    reply = handle_rpc(user, payload, mode)
    return ("", 202) if reply is None else jsonify(reply)


@bp.route("/mcp", methods=["GET", "DELETE"])
def mcp_unsupported():
    # No server-initiated SSE stream and no sessions to terminate; 405 is allowed by the spec.
    if _bearer_user() is None:
        return _unauthorized()
    return Response(status=405, headers={"Allow": "POST"})
