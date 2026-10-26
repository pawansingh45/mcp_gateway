"""Append-only audit log of tool calls (who, which server/tool, when, how long, outcome)."""
from __future__ import annotations

import json
import re

from .db import Database, now_iso

_SENSITIVE = re.compile(r"(pass|secret|token|key|auth|credential)", re.I)
OUTCOMES = ("success", "tool_error", "timeout", "unreachable", "upstream_error", "denied",
            "not_found", "rate_limited", "tools_changed")


def _redact(value, depth=0):
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        return {k: ("***" if _SENSITIVE.search(str(k)) else _redact(v, depth + 1)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, depth + 1) for v in value[:20]]
    return value


def args_preview(arguments, limit: int = 300) -> str:
    try:
        text = json.dumps(_redact(arguments), default=str)
    except (TypeError, ValueError):
        text = "<unserialisable>"
    return text if len(text) <= limit else text[:limit] + "…"


class AuditLog:
    def __init__(self, db: Database):
        self.db = db

    def record(self, *, user_id: int | None, user_email: str, source: str, server_slug: str | None,
               tool: str | None, duration_ms: int, outcome: str, detail: str | None = None,
               arguments=None) -> None:
        self.db.run(
            "INSERT INTO audit (ts,user_id,user_email,source,server_slug,tool,duration_ms,outcome,detail,args)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), user_id, user_email, source, server_slug, tool, duration_ms, outcome,
             (detail or "")[:500] or None, args_preview(arguments) if arguments else None))

    def query(self, *, user_id: int | None = None, server: str | None = None, outcome: str | None = None,
              limit: int = 50, offset: int = 0) -> dict:
        where, params = [], []
        if user_id is not None:
            where.append("user_id = ?"); params.append(user_id)
        if server:
            where.append("server_slug = ?"); params.append(server)
        if outcome:
            where.append("outcome = ?"); params.append(outcome)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        total = self.db.one(f"SELECT COUNT(*) AS n FROM audit {clause}", params)["n"]
        rows = self.db.all(f"SELECT * FROM audit {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                           (*params, max(1, min(limit, 200)), max(0, offset)))
        return {"total": total, "rows": rows}
