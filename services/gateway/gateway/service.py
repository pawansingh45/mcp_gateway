"""GatewayService: the one code path every tool call takes.

External MCP clients (/mcp) and the dashboard chat agent both call into this
class, so authorisation, rate limiting, timeouts, result sanitising, audit and
metrics are enforced identically for both and cannot be bypassed by picking a
different entry point.

Call pipeline:
    rate limit -> resolve public name -> role policy -> upstream health
    -> upstream call (hard timeout) -> sanitise result -> audit + metrics
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from .audit import AuditLog
from .config import Settings
from .metrics import Metrics
from .ratelimit import RateLimiter
from .registry import Registry, ToolEntry, matches_any
from .upstream import UpstreamError

log = logging.getLogger("gateway.service")

META_SEARCH = "search_tools"
META_EXECUTE = "execute_tool"
META_TOOLS = [
    {"name": META_SEARCH,
     "description": "Search the catalog of available tools by keyword. Returns matching tool names, "
                    "descriptions and input schemas. Use this first, then call execute_tool.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Keywords, e.g. 'orders' or 'create product'."},
         "limit": {"type": "integer", "description": "Max results (default 8)."}}, "required": ["query"]}},
    {"name": META_EXECUTE,
     "description": "Execute a tool found with search_tools, by its exact name.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "Exact tool name from search_tools."},
         "arguments": {"type": "object", "description": "Arguments matching the tool's inputSchema."}},
         "required": ["name"]}},
]


@dataclass(frozen=True)
class User:
    id: int
    email: str
    name: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


@dataclass
class CallOutcome:
    result: dict            # an MCP CallToolResult
    outcome: str            # see audit.OUTCOMES
    unknown_tool: bool = False
    duration_ms: int = 0


def text_result(text: str, is_error: bool = False) -> dict:
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["isError"] = True
    return out


class GatewayService:
    def __init__(self, settings: Settings, registry: Registry, audit: AuditLog, metrics: Metrics,
                 limiter: RateLimiter):
        self.settings, self.registry, self.audit, self.metrics, self.limiter = settings, registry, audit, metrics, limiter

    # ----------------------------------------------------------- listing
    def allowed(self, server_row: dict, tool_name: str, user: User) -> bool:
        policy = json.loads(server_row["tool_policy"] or "{}")
        if matches_any(tool_name, policy.get("deny", [])):
            return False
        if matches_any(tool_name, policy.get("admin_only", [])) and not user.is_admin:
            return False
        return True

    def visible(self, user: User) -> list[tuple[dict, ToolEntry]]:
        return [(row, e) for row, e in self.registry.visible_entries() if self.allowed(row, e.tool_name, user)]

    def list_tools(self, user: User, mode: str = "full") -> list[dict]:
        if mode == "lazy":
            return META_TOOLS
        return [e.definition for _, e in self.visible(user)]

    def search_tools(self, user: User, query: str, limit: int = 8) -> list[dict]:
        words = [w for w in query.lower().replace("_", " ").replace("-", " ").split() if w]
        scored = []
        for _, entry in self.visible(user):
            name = entry.public_name.lower().replace("_", " ").replace("-", " ")
            desc = entry.definition["description"].lower()
            score = sum(3 for w in words if w in name) + sum(1 for w in words if w in desc)
            if score:
                scored.append((score, entry))
        scored.sort(key=lambda s: (-s[0], s[1].public_name))
        return [e.definition for _, e in scored[: max(1, min(limit, 20))]]

    # ----------------------------------------------------------- calling
    def call_tool(self, user: User, public_name: str, arguments: Any, source: str) -> CallOutcome:
        started = time.monotonic()
        ctx = {"server": None, "tool": public_name}
        if not isinstance(arguments, dict):
            arguments = {}
        shown_args = arguments

        if public_name == META_SEARCH:
            query = str(arguments.get("query", ""))
            limit = arguments.get("limit") if isinstance(arguments.get("limit"), int) else 8
            found = self.search_tools(user, query, limit)
            ctx.update(server="gateway")
            return self._finish(user, source, ctx, started, "success", text_result(json.dumps(found, indent=1)),
                                shown_args)
        if public_name == META_EXECUTE:
            public_name = str(arguments.get("name", ""))
            inner = arguments.get("arguments")
            arguments = inner if isinstance(inner, dict) else {}
            shown_args, ctx["tool"] = arguments, public_name

        allowed, retry_in = self.limiter.allow(user.id)
        if not allowed:
            return self._finish(user, source, ctx, started, "rate_limited",
                                text_result(f"Rate limit exceeded. Retry in {retry_in:.0f}s.", True), shown_args)

        found = self.registry.locate(public_name)
        if found is None:
            return self._finish(user, source, ctx, started, "not_found",
                                text_result(f"Unknown tool: {public_name}", True), shown_args, unknown=True)
        row, entry = found
        ctx.update(server=row["slug"], tool=entry.tool_name)

        if not self.allowed(row, entry.tool_name, user):
            # Same response as "unknown": do not confirm that a hidden tool exists.
            return self._finish(user, source, ctx, started, "denied",
                                text_result(f"Unknown tool: {public_name}", True), shown_args, unknown=True,
                                detail=f"role '{user.role}' may not call this tool")

        state = self.registry.state(row["id"])
        if state is None or state.status != "connected":
            reason = (state.last_error if state else None) or "not connected yet"
            return self._finish(user, source, ctx, started, "unreachable",
                                text_result(f"The '{row['slug']}' server is currently unreachable ({reason}). "
                                            "Other servers are unaffected; try again shortly.", True),
                                shown_args, detail=reason)

        try:
            client = self.registry.client_for(row)
            raw = client.call_tool(entry.tool_name, arguments, timeout=self.settings.call_timeout)
        except UpstreamError as exc:
            if exc.kind == "unreachable":
                self.registry.mark_unreachable(row, f"{exc.kind}: {exc.message}")
            outcome = exc.kind if exc.kind in ("timeout", "unreachable") else "upstream_error"
            msg = {"timeout": f"The '{row['slug']}' server did not answer within {self.settings.call_timeout:.0f}s.",
                   "unreachable": f"The '{row['slug']}' server is unreachable."}.get(
                exc.kind, f"The '{row['slug']}' server returned an error: {exc.message}")
            return self._finish(user, source, ctx, started, outcome, text_result(msg, True), shown_args,
                                detail=f"{exc.kind}: {exc.message}")

        result = self._sanitize_result(raw)
        outcome = "tool_error" if result.get("isError") else "success"
        return self._finish(user, source, ctx, started, outcome, result, shown_args)

    # ----------------------------------------------------------- helpers
    def _sanitize_result(self, raw: dict) -> dict:
        """Only text survives; other content types are replaced by a placeholder.
        A hostile upstream thus cannot push huge blobs or odd payloads through us."""
        budget = self.settings.max_result_chars
        blocks = []
        for block in raw.get("content", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                if len(text) > budget:
                    text = text[:budget] + "\n[truncated by gateway]"
                budget = max(0, budget - len(text))
                blocks.append({"type": "text", "text": text})
            else:
                blocks.append({"type": "text", "text": f"[{block.get('type', 'unknown')} content omitted by gateway]"})
        out: dict[str, Any] = {"content": blocks or [{"type": "text", "text": ""}]}
        if raw.get("isError") is True:
            out["isError"] = True
        return out

    def _finish(self, user: User, source: str, ctx: dict, started: float, outcome: str, result: dict,
                arguments: Any, unknown: bool = False, detail: str | None = None) -> CallOutcome:
        duration_ms = int((time.monotonic() - started) * 1000)
        if detail is None and outcome in ("tool_error", "rate_limited", "not_found", "upstream_error", "timeout"):
            detail = result["content"][0]["text"][:300] if result.get("content") else None
        try:
            self.audit.record(user_id=user.id, user_email=user.email, source=source, server_slug=ctx["server"],
                              tool=ctx["tool"], duration_ms=duration_ms, outcome=outcome, detail=detail,
                              arguments=arguments)
        except Exception:
            log.exception("FAILED TO WRITE AUDIT RECORD for %s %s", user.email, ctx)
        self.metrics.observe_call(ctx["server"] or "unknown", outcome, duration_ms)
        return CallOutcome(result, outcome, unknown, duration_ms)

    def system_event(self, server_slug: str, outcome: str, detail: str) -> None:
        self.audit.record(user_id=None, user_email="system", source="system", server_slug=server_slug,
                          tool=None, duration_ms=0, outcome=outcome, detail=detail)
