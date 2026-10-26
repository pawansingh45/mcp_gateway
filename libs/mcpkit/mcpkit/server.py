"""MCP server over Streamable HTTP (JSON responses, stateless).

Protocol notes (spec revision 2025-06-18, "Streamable HTTP" transport):
  * Client sends every JSON-RPC message as an HTTP POST to the single MCP endpoint.
  * Requests (have an `id`) get a JSON-RPC response; the server may answer with
    `application/json` or an SSE stream. We always answer with JSON - simplest
    and fully allowed by the spec.
  * Notifications / responses from the client get `202 Accepted` and no body.
  * We do not assign an `Mcp-Session-Id` (sessions are optional), which keeps the
    server stateless and trivially horizontally scalable.
  * GET on the endpoint is for server-initiated SSE streams. We have none, so we
    answer 405 which the spec explicitly permits.
"""
from __future__ import annotations

import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from flask import Flask, Response, jsonify, request

from .validate import validate

log = logging.getLogger("mcpkit")

# Newest first. We echo the client's version when we support it, else our newest.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

# JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ToolError(Exception):
    """Raise from a tool to report a failure the *model* should see.

    Becomes a normal result with `isError: true` (MCP convention: tool-execution
    errors are results, protocol errors are JSON-RPC errors).
    """


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], Any]
    read_only: bool = False


@dataclass
class McpApp:
    name: str
    version: str = "1.0.0"
    instructions: str | None = None
    auth_token: str | None = None  # if set, require `Authorization: Bearer <token>`
    tools: dict[str, Tool] = field(default_factory=dict)

    # ---- registration -------------------------------------------------
    def tool(self, name: str, description: str, input_schema: dict, read_only: bool = False):
        def deco(fn: Callable[[dict], Any]):
            if name in self.tools:
                raise ValueError(f"duplicate tool {name}")
            schema = {"type": "object", **input_schema}
            self.tools[name] = Tool(name, description, schema, fn, read_only)
            return fn
        return deco

    # ---- JSON-RPC dispatch (pure; unit-testable without HTTP) ---------
    def handle_message(self, msg: Any) -> dict | None:
        """Handle one JSON-RPC message. Returns a response dict or None."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "Invalid JSON-RPC request")
        method = msg.get("method")
        msg_id = msg.get("id")
        if method is None:  # a response from the client; nothing for us to do
            return None
        if "id" not in msg:  # notification: notifications/initialized, /cancelled, ...
            return None
        params = msg.get("params") or {}

        try:
            if method == "initialize":
                return _ok(msg_id, self._initialize(params))
            if method == "ping":
                return _ok(msg_id, {})
            if method == "tools/list":
                return _ok(msg_id, {"tools": [self._describe(t) for t in self.tools.values()]})
            if method == "tools/call":
                return self._call(msg_id, params)
            return _error(msg_id, METHOD_NOT_FOUND, f"Method not found: {method}")
        except Exception:  # never leak a stack trace to the client
            log.exception("unhandled error in %s", method)
            return _error(msg_id, INTERNAL_ERROR, "Internal error")

    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
        result: dict[str, Any] = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.name, "version": self.version},
        }
        if self.instructions:
            result["instructions"] = self.instructions
        return result

    @staticmethod
    def _describe(tool: Tool) -> dict:
        d = {"name": tool.name, "description": tool.description, "inputSchema": tool.input_schema}
        if tool.read_only:
            d["annotations"] = {"readOnlyHint": True}
        return d

    def _call(self, msg_id: Any, params: dict) -> dict:
        name = params.get("name")
        tool = self.tools.get(name) if isinstance(name, str) else None
        if tool is None:
            return _error(msg_id, INVALID_PARAMS, f"Unknown tool: {name}")
        args = params.get("arguments") or {}
        problems = validate(args, tool.input_schema)
        if problems:
            # Argument problems are tool-level errors so the model can self-correct.
            return _ok(msg_id, _text_result("Invalid arguments: " + "; ".join(problems), is_error=True))
        started = time.monotonic()
        try:
            out = tool.handler(args)
            result = _text_result(out if isinstance(out, str) else json.dumps(out, default=str, indent=2))
        except ToolError as exc:
            result = _text_result(str(exc), is_error=True)
        except Exception:
            log.exception("tool %s crashed", name)
            result = _text_result("Tool failed unexpectedly.", is_error=True)
        log.info("tool=%s error=%s ms=%d", name, result.get("isError", False), (time.monotonic() - started) * 1000)
        return _ok(msg_id, result)

    # ---- HTTP wiring ---------------------------------------------------
    def flask_app(self, path: str = "/mcp") -> Flask:
        app = Flask(self.name)

        @app.get("/healthz")
        def healthz():  # liveness/readiness: no auth, no dependencies
            return jsonify(status="ok", server=self.name)

        @app.route(path, methods=["POST"])
        def mcp_post():
            if not self._authorized():
                return Response(json.dumps({"error": "unauthorized"}), 401, mimetype="application/json",
                                headers={"WWW-Authenticate": "Bearer"})
            payload = request.get_json(silent=True)
            if payload is None:
                return jsonify(_error(None, PARSE_ERROR, "Parse error")), 400
            if isinstance(payload, list):  # JSON-RPC batch (allowed by 2025-03-26)
                replies = [r for r in (self.handle_message(m) for m in payload) if r is not None]
                return (jsonify(replies), 200) if replies else ("", 202)
            reply = self.handle_message(payload)
            if reply is None:
                return "", 202
            return jsonify(reply)

        @app.route(path, methods=["GET", "DELETE"])
        def mcp_other():
            return Response(status=405, headers={"Allow": "POST"})

        return app

    def _authorized(self) -> bool:
        if not self.auth_token:
            return True
        header = request.headers.get("Authorization", "")
        scheme, _, supplied = header.partition(" ")
        # constant-time comparison: don't leak the token through timing
        return scheme.lower() == "bearer" and hmac.compare_digest(supplied.strip(), self.auth_token)


def _ok(msg_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _text_result(text: str, is_error: bool = False) -> dict:
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["isError"] = True
    return out
