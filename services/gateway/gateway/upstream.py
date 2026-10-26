"""MCP *client* for one upstream server (Streamable HTTP transport).

Flow for a fresh connection (spec 2025-06-18):
    POST initialize                       -> result + optional Mcp-Session-Id header
    POST notifications/initialized        -> 202 Accepted
    POST tools/list | tools/call          -> JSON body or SSE stream with the response
A server that returns 404 for a known session id has expired it; we start a new
session once and retry.

Hardening, because upstreams are untrusted:
  * hard wall-clock deadline per operation (connect + read + SSE streaming);
  * response size cap (a hostile server can't make us buffer gigabytes);
  * redirects are never followed (classic SSRF pivot);
  * outgoing headers are built from scratch: the only credential ever sent is the
    one the admin configured for this upstream. Client API keys are never forwarded.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

import requests

log = logging.getLogger("gateway.upstream")

CLIENT_INFO = {"name": "mcp-gateway", "version": "1.0.0"}
PROTOCOL_VERSION = "2025-06-18"


class UpstreamError(Exception):
    """kind: unreachable | timeout | auth | http | protocol | upstream_error"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


class _Deadline:
    def __init__(self, seconds: float):
        self.end = time.monotonic() + seconds

    def remaining(self) -> float:
        left = self.end - time.monotonic()
        if left <= 0:
            raise UpstreamError("timeout", "upstream did not respond in time")
        return left


class UpstreamClient:
    def __init__(self, url: str, headers: dict[str, str], connect_timeout: float = 3.0,
                 max_bytes: int = 2_000_000, pre_connect_check: Callable[[], None] | None = None,
                 session: requests.Session | None = None):
        self.url = url
        self._auth_headers = dict(headers)
        self.connect_timeout = connect_timeout
        self.max_bytes = max_bytes
        self._pre_connect_check = pre_connect_check
        self._http = session or requests.Session()
        self._lock = threading.Lock()
        self._session_id: str | None = None
        self._protocol_version: str | None = None
        self._ids = iter(range(1, 2 ** 31))

    # ---- public API ----------------------------------------------------
    def list_tools(self, timeout: float) -> list[dict]:
        deadline = _Deadline(timeout)
        tools: list[dict] = []
        cursor = None
        for _ in range(20):  # bounded pagination
            params = {"cursor": cursor} if cursor else {}
            result = self._rpc("tools/list", params, deadline)
            page = result.get("tools")
            if not isinstance(page, list):
                raise UpstreamError("protocol", "tools/list returned no tool array")
            tools.extend(t for t in page if isinstance(t, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict, timeout: float) -> dict:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments}, _Deadline(timeout))
        if not isinstance(result.get("content"), list):
            raise UpstreamError("protocol", "tools/call result has no content array")
        return result

    def reset(self) -> None:
        """Forget the session; the next operation performs a fresh handshake."""
        self._reset_session()

    def close(self) -> None:
        self._http.close()

    # ---- JSON-RPC plumbing ---------------------------------------------
    def _rpc(self, method: str, params: dict, deadline: _Deadline) -> dict:
        self._ensure_session(deadline)
        try:
            return self._request(method, params, deadline)
        except _SessionExpired:
            self._reset_session()
            self._ensure_session(deadline)
            try:
                return self._request(method, params, deadline)
            except _SessionExpired:
                raise UpstreamError("protocol", "upstream keeps rejecting our session") from None

    def _ensure_session(self, deadline: _Deadline) -> None:
        with self._lock:
            if self._protocol_version is not None:
                return
            if self._pre_connect_check:
                try:
                    self._pre_connect_check()
                except ValueError as exc:  # SsrfError subclasses ValueError
                    raise UpstreamError("unreachable", f"blocked by gateway network policy: {exc}") from exc
            result = self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO,
            }, deadline, handshake=True)
            self._protocol_version = str(result.get("protocolVersion") or PROTOCOL_VERSION)
            try:
                self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, deadline, expect_reply=False)
            except _SessionExpired:
                self._session_id = self._protocol_version = None
                raise UpstreamError("protocol", "upstream rejected the new session during handshake") from None

    def _reset_session(self) -> None:
        with self._lock:
            self._session_id = None
            self._protocol_version = None

    def _request(self, method: str, params: dict, deadline: _Deadline, handshake: bool = False) -> dict:
        msg_id = next(self._ids)
        reply = self._post({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params},
                           deadline, expect_reply=True, msg_id=msg_id, handshake=handshake)
        if "error" in reply:
            err = reply["error"] if isinstance(reply["error"], dict) else {}
            raise UpstreamError("upstream_error", f"{err.get('message', 'upstream error')} (code {err.get('code')})")
        result = reply.get("result")
        if not isinstance(result, dict):
            raise UpstreamError("protocol", "response has no result object")
        return result

    def _post(self, payload: dict, deadline: _Deadline, expect_reply: bool,
              msg_id: int | None = None, handshake: bool = False) -> dict:
        headers = {
            **self._auth_headers,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._protocol_version and not handshake:
            headers["MCP-Protocol-Version"] = self._protocol_version
        try:
            resp = self._http.post(self.url, data=json.dumps(payload), headers=headers,
                                   timeout=(min(self.connect_timeout, deadline.remaining()), deadline.remaining()),
                                   stream=True, allow_redirects=False)
        except requests.Timeout as exc:
            raise UpstreamError("timeout", "upstream did not respond in time") from exc
        except requests.RequestException as exc:
            raise UpstreamError("unreachable", f"cannot connect to upstream ({type(exc).__name__})") from exc

        try:
            if resp.status_code == 404 and self._session_id and not handshake:
                raise _SessionExpired()
            if resp.status_code in (401, 403):
                raise UpstreamError("auth", "upstream rejected the gateway's credentials")
            if 300 <= resp.status_code < 400:
                raise UpstreamError("protocol", "upstream attempted a redirect (not followed)")
            if resp.status_code >= 400:
                raise UpstreamError("http", f"upstream returned HTTP {resp.status_code}")
            if handshake and resp.headers.get("Mcp-Session-Id"):
                self._session_id = resp.headers["Mcp-Session-Id"]
            if not expect_reply:
                return {}
            ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ctype == "text/event-stream":
                return self._read_sse(resp, msg_id, deadline)
            return self._read_json(resp, msg_id, deadline)
        finally:
            resp.close()

    def _read_body(self, resp: requests.Response, deadline: _Deadline) -> bytes:
        chunks, total = [], 0
        try:
            for chunk in resp.iter_content(chunk_size=8192):
                deadline.remaining()
                total += len(chunk)
                if total > self.max_bytes:
                    raise UpstreamError("protocol", "upstream response too large")
                chunks.append(chunk)
        except requests.Timeout as exc:
            raise UpstreamError("timeout", "upstream stalled while sending the response") from exc
        except requests.RequestException as exc:
            raise UpstreamError("unreachable", "connection to upstream was lost") from exc
        return b"".join(chunks)

    def _read_json(self, resp: requests.Response, msg_id: int | None, deadline: _Deadline) -> dict:
        body = self._read_body(resp, deadline)
        try:
            data: Any = json.loads(body)
        except ValueError as exc:
            raise UpstreamError("protocol", "upstream returned invalid JSON") from exc
        return _pick_reply(data, msg_id)

    def _read_sse(self, resp: requests.Response, msg_id: int | None, deadline: _Deadline) -> dict:
        """Parse a text/event-stream until the JSON-RPC response for msg_id arrives."""
        data_lines: list[str] = []
        total = 0
        try:
            for raw in resp.iter_lines(decode_unicode=True):
                deadline.remaining()
                total += len(raw or "") + 1
                if total > self.max_bytes:
                    raise UpstreamError("protocol", "upstream response too large")
                if raw:
                    if raw.startswith("data:"):
                        data_lines.append(raw[5:].lstrip())
                    continue
                if not data_lines:  # blank line with nothing buffered
                    continue
                event, data_lines = "\n".join(data_lines), []
                try:
                    message = json.loads(event)
                except ValueError:
                    continue  # ignore non-JSON events (comments, pings)
                if isinstance(message, dict) and message.get("id") == msg_id and ("result" in message or "error" in message):
                    return message
                # else: a server-initiated notification/request; we advertise no capabilities, so ignore
        except requests.Timeout as exc:
            raise UpstreamError("timeout", "upstream stalled while streaming the response") from exc
        except requests.RequestException as exc:
            raise UpstreamError("unreachable", "connection to upstream was lost") from exc
        raise UpstreamError("protocol", "stream ended without a response")


class _SessionExpired(Exception):
    pass


def _pick_reply(data: Any, msg_id: int | None) -> dict:
    candidates = data if isinstance(data, list) else [data]
    for item in candidates:
        if isinstance(item, dict) and item.get("id") == msg_id and ("result" in item or "error" in item):
            return item
    raise UpstreamError("protocol", "response did not contain the expected JSON-RPC reply")
