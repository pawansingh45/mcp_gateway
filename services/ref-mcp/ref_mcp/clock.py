"""'clock' MCP server: the current time and timezone conversion.

Models don't know what day it is. Pairing this with the WooCommerce server is the
canonical "two upstream servers in one prompt" demo: 'what time is it in the
store's timezone, and which orders came in since midnight?'
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcpkit import McpApp, ToolError


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ToolError(f"Unknown timezone '{name}'. Use an IANA name like 'Asia/Kolkata'.") from exc


def build_app(auth_token: str | None = None, now_fn=None) -> McpApp:
    now_fn = now_fn or (lambda tz: datetime.now(tz))
    app = McpApp("clock-mcp", auth_token=auth_token, instructions="Current time and timezone conversion.")

    @app.tool("current_time", "Get the current date and time in a timezone (default UTC).",
              {"properties": {"timezone": {"type": "string", "description": "IANA timezone, e.g. 'Asia/Kolkata'."}}},
              read_only=True)
    def current_time(a):
        now = now_fn(_zone(a.get("timezone", "UTC")))
        return {"iso": now.isoformat(), "date": now.date().isoformat(), "time": now.strftime("%H:%M:%S"),
                "weekday": now.strftime("%A"), "timezone": str(now.tzinfo)}

    @app.tool("convert_time", "Convert a timestamp (ISO 8601, naive means from_timezone) between timezones.",
              {"properties": {"timestamp": {"type": "string"}, "from_timezone": {"type": "string"},
                              "to_timezone": {"type": "string"}},
               "required": ["timestamp", "to_timezone"]}, read_only=True)
    def convert_time(a):
        try:
            ts = datetime.fromisoformat(a["timestamp"])
        except ValueError as exc:
            raise ToolError("timestamp must be ISO 8601, e.g. 2026-10-24T09:30:00") from exc
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_zone(a.get("from_timezone", "UTC")))
        out = ts.astimezone(_zone(a["to_timezone"]))
        return {"iso": out.isoformat(), "weekday": out.strftime("%A")}

    return app
