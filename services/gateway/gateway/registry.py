"""Registry of upstream MCP servers: persistence, connection state, tool catalog.

Key design decisions
--------------------
* The hot path (tools/list) NEVER talks to upstreams. It reads a cached catalog
  that a background health loop refreshes. A slow or dead upstream therefore
  cannot slow down or break tool listing for anyone else.
* Tool names are namespaced as ``<server-slug>__<tool>``. Always prefixing (rather
  than only on collision) means a name never changes when another server is added
  or removed, and two servers exposing ``search`` can coexist. Slugs are immutable
  for the same reason.
* Everything an upstream tells us about its tools is untrusted input (it ends up in
  a model's context): descriptions are length-capped and stripped of control
  characters, schemas are size-capped, and only whitelisted annotations survive.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from . import ssrf
from .config import Settings
from .db import Database, now_iso
from .security import Crypto
from .upstream import UpstreamClient, UpstreamError

log = logging.getLogger("gateway.registry")

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
# Headers an admin must not override: they would break framing or the MCP session.
FORBIDDEN_HEADERS = {"host", "content-length", "content-type", "transfer-encoding", "connection", "accept",
                     "mcp-session-id", "mcp-protocol-version", "upgrade", "te", "trailer", "cookie"}
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ANNOTATION_KEYS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
MAX_SCHEMA_BYTES = 16_000


class ValidationError(ValueError):
    pass


class NotFound(LookupError):
    pass


@dataclass(frozen=True)
class ToolEntry:
    public_name: str
    server_slug: str
    tool_name: str          # the upstream's own name for the tool
    definition: dict        # sanitised MCP tool definition with name == public_name


@dataclass
class ServerState:
    status: str = "pending"            # pending | connected | unreachable
    entries: list[ToolEntry] = field(default_factory=list)
    last_error: str | None = None
    checked_at: str | None = None
    latency_ms: int | None = None
    tools_hash: str | None = None

    @property
    def by_name(self) -> dict[str, ToolEntry]:
        return {e.public_name: e for e in self.entries}


# ---------------------------------------------------------------- tool naming
def public_name(slug: str, tool: str, taken: set[str]) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", tool)
    base = f"{slug}__{clean}"
    if len(base) > 64:  # most MCP clients / LLM APIs cap tool names at 64 chars
        digest = hashlib.sha1(tool.encode()).hexdigest()[:6]
        base = f"{slug}__{clean[: 64 - len(slug) - 2 - 7]}_{digest}"
    candidate, n = base, 2
    while candidate in taken:
        candidate = f"{base[:60]}_{n}"
        n += 1
    return candidate


def _clean_text(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else ""
    return _CONTROL_CHARS.sub("", text)[:limit]


def _clean_schema(node: Any, desc_limit: int, depth: int = 0, names: bool = False) -> Any:
    """Copy a JSON schema, truncating every free-text description (injection surface)."""
    if depth > 8:
        return {}
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in ("description", "title") and not names:
                out[k] = _clean_text(v, desc_limit)
            else:
                out[k] = _clean_schema(v, desc_limit, depth + 1,
                                       names=k in ("properties", "patternProperties", "$defs", "definitions"))
        return out
    if isinstance(node, list):
        return [_clean_schema(v, desc_limit, depth + 1) for v in node[:200]]
    if isinstance(node, str):
        return node[:500]
    return node


def sanitize_definition(public: str, raw: dict, desc_max: int) -> dict:
    schema = raw.get("inputSchema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        schema = {"type": "object", "properties": {}}
    schema = _clean_schema(schema, min(desc_max, 500))
    if len(json.dumps(schema)) > MAX_SCHEMA_BYTES:
        schema = {"type": "object", "properties": {}, "additionalProperties": True}
    out = {"name": public, "description": _clean_text(raw.get("description"), desc_max), "inputSchema": schema}
    ann = raw.get("annotations")
    if isinstance(ann, dict):
        kept = {k: ann[k] for k in _ANNOTATION_KEYS if isinstance(ann.get(k), bool)}
        if kept:
            out["annotations"] = kept
    return out


def build_entries(slug: str, raw_tools: list[dict], desc_max: int) -> list[ToolEntry]:
    taken: set[str] = set()
    entries = []
    for raw in raw_tools:
        name = raw.get("name")
        if not isinstance(name, str) or not name or len(name) > 200:
            continue
        pub = public_name(slug, name, taken)
        taken.add(pub)
        entries.append(ToolEntry(pub, slug, name, sanitize_definition(pub, raw, desc_max)))
    return entries


def _hash_entries(entries: list[ToolEntry]) -> str:
    blob = json.dumps([(e.tool_name, e.definition) for e in entries], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ------------------------------------------------------------------- registry
class Registry:
    def __init__(self, db: Database, crypto: Crypto, settings: Settings,
                 event_hook: Callable[[str, str, str], None] | None = None):
        self.db, self.crypto, self.settings = db, crypto, settings
        self._event_hook = event_hook  # (server_slug, outcome, detail) -> None
        self._states: dict[int, ServerState] = {}
        self._clients: dict[int, UpstreamClient] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="upstream")

    # ------------------------------------------------------------------ CRUD
    def list_servers(self) -> list[dict]:
        return self.db.all("SELECT * FROM servers ORDER BY id")

    def enabled_servers(self) -> list[dict]:
        return self.db.all("SELECT * FROM servers WHERE enabled = 1 ORDER BY id")

    def get(self, server_id: int) -> dict:
        row = self.db.one("SELECT * FROM servers WHERE id = ?", (server_id,))
        if not row:
            raise NotFound("server not found")
        return row

    def create(self, data: dict, managed: bool = False, refresh: bool = True) -> dict:
        slug = str(data.get("slug", "")).strip().lower()
        if not SLUG_RE.match(slug):
            raise ValidationError("slug must be 1-24 chars: lowercase letters, digits, '-', starting with a letter")
        if self.db.one("SELECT id FROM servers WHERE slug = ?", (slug,)):
            raise ValidationError(f"a server with slug '{slug}' already exists")
        name = self._name(data.get("name") or slug)
        url = self._url(data.get("url"))
        auth_type, cred = self._credential(data)
        policy = self._policy(data.get("tool_policy", {}))
        now = now_iso()
        server_id = self.db.run(
            "INSERT INTO servers (slug,name,url,auth_type,credential_enc,enabled,tool_policy,managed,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (slug, name, url, auth_type, self.crypto.encrypt_json(cred) if cred else None,
             1 if data.get("enabled", True) else 0, json.dumps(policy), 1 if managed else 0, now, now))
        row = self.get(server_id)
        if row["enabled"] and refresh:
            self.refresh(server_id)
        return row

    def update(self, server_id: int, data: dict) -> dict:
        row = self.get(server_id)
        fields: dict[str, Any] = {}
        if "name" in data:
            fields["name"] = self._name(data["name"])
        if "url" in data:
            fields["url"] = self._url(data["url"])
        if "enabled" in data:
            fields["enabled"] = 1 if data["enabled"] else 0
        if "tool_policy" in data:
            fields["tool_policy"] = json.dumps(self._policy(data["tool_policy"]))
        if data.get("clear_credential"):
            fields.update(auth_type="none", credential_enc=None)
        elif any(k in data for k in ("auth_type", "auth_token", "auth_header_name", "auth_header_value")):
            auth_type, cred = self._credential({"auth_type": data.get("auth_type", row["auth_type"]), **data})
            fields.update(auth_type=auth_type, credential_enc=self.crypto.encrypt_json(cred) if cred else None)
        if not fields:
            return row
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.run(f"UPDATE servers SET {cols} WHERE id = ?", (*fields.values(), server_id))
        self._drop_client(server_id)
        with self._lock:
            self._states.pop(server_id, None)  # url/credential may have changed: forget old status
        updated = self.get(server_id)
        if updated["enabled"]:
            self.refresh(server_id)
        return updated

    def delete(self, server_id: int) -> None:
        self.get(server_id)
        self.db.run("DELETE FROM servers WHERE id = ?", (server_id,))
        self._drop_client(server_id)
        with self._lock:
            self._states.pop(server_id, None)

    # ------------------------------------------------------------ validation
    @staticmethod
    def _name(value: Any) -> str:
        name = _clean_text(value, 80).strip()
        if not name:
            raise ValidationError("name must not be empty")
        return name

    def _url(self, value: Any) -> str:
        url = str(value or "").strip()
        try:
            ssrf.check_url(url, self.settings.ssrf_allowed_hosts, self.settings.ssrf_allow_loopback)
        except ssrf.SsrfError as exc:
            raise ValidationError(str(exc)) from exc
        return url

    @staticmethod
    def _credential(data: dict) -> tuple[str, dict | None]:
        auth_type = data.get("auth_type") or "none"
        if auth_type == "none":
            return "none", None
        if auth_type == "bearer":
            token = str(data.get("auth_token") or "")
            if not token:
                raise ValidationError("auth_token is required for bearer auth")
            return "bearer", {"token": token}
        if auth_type == "header":
            name, value = str(data.get("auth_header_name") or ""), str(data.get("auth_header_value") or "")
            if not HEADER_NAME_RE.match(name) or name.lower() in FORBIDDEN_HEADERS:
                raise ValidationError("auth_header_name is not a permitted header name")
            if not value or re.search(r"[\r\n]", value):
                raise ValidationError("auth_header_value is required and must be a single line")
            return "header", {"name": name, "value": value}
        raise ValidationError("auth_type must be none, bearer or header")

    @staticmethod
    def _policy(value: Any) -> dict:
        if not isinstance(value, dict):
            raise ValidationError("tool_policy must be an object")
        out = {}
        for key in ("admin_only", "deny"):
            patterns = value.get(key, [])
            if not isinstance(patterns, list) or len(patterns) > 100 or not all(
                    isinstance(p, str) and 0 < len(p) <= 100 for p in patterns):
                raise ValidationError(f"tool_policy.{key} must be a list of glob patterns")
            if patterns:
                out[key] = patterns
        return out

    # ------------------------------------------------------------ connections
    def _headers_for(self, row: dict) -> dict[str, str]:
        if row["auth_type"] == "none" or not row["credential_enc"]:
            return {}
        cred = self.crypto.decrypt_json(row["credential_enc"])
        if row["auth_type"] == "bearer":
            return {"Authorization": f"Bearer {cred['token']}"}
        return {cred["name"]: cred["value"]}

    def client_for(self, row: dict) -> UpstreamClient:
        with self._lock:
            client = self._clients.get(row["id"])
            if client is None:
                url, s = row["url"], self.settings
                client = UpstreamClient(
                    url, self._headers_for(row), connect_timeout=s.connect_timeout, max_bytes=s.max_upstream_bytes,
                    pre_connect_check=lambda: ssrf.check_url(url, s.ssrf_allowed_hosts, s.ssrf_allow_loopback))
                self._clients[row["id"]] = client
            return client

    def _drop_client(self, server_id: int) -> None:
        with self._lock:
            client = self._clients.pop(server_id, None)
        if client:
            client.close()

    # ------------------------------------------------------------------ state
    def state(self, server_id: int) -> ServerState | None:
        with self._lock:
            return self._states.get(server_id)

    def status_of(self, row: dict) -> str:
        if not row["enabled"]:
            return "disabled"
        st = self.state(row["id"])
        return st.status if st else "pending"

    def refresh(self, server_id: int) -> ServerState | None:
        """Fetch the tool list from one upstream and update its state. Never raises."""
        try:
            row = self.get(server_id)
        except NotFound:
            return None
        started = time.monotonic()
        try:
            client = self.client_for(row)
            raw = client.list_tools(timeout=self.settings.list_timeout)
            entries = build_entries(row["slug"], raw, self.settings.tool_description_max)
            new = ServerState("connected", entries, None, now_iso(), int((time.monotonic() - started) * 1000),
                              _hash_entries(entries))
            with self._lock:
                old = self._states.get(server_id)
                self._states[server_id] = new
            if old and old.tools_hash and old.tools_hash != new.tools_hash and self._event_hook:
                # Possible "rug pull": an upstream changed tool definitions after being trusted.
                self._event_hook(row["slug"], "tools_changed", f"{len(old.entries)} -> {len(entries)} tools")
            return new
        except UpstreamError as exc:
            self._mark_failed(row, f"{exc.kind}: {exc.message}")
        except Exception as exc:  # e.g. undecryptable credential; keep the loop alive
            log.exception("refresh failed for %s", row["slug"])
            self._mark_failed(row, f"internal: {type(exc).__name__}")
        return self.state(server_id)

    def mark_unreachable(self, row: dict, reason: str) -> None:
        """Called by the call path when a connection error proves the upstream is down."""
        self._mark_failed(row, reason)

    def _mark_failed(self, row: dict, reason: str) -> None:
        with self._lock:
            old = self._states.get(row["id"])
            self._states[row["id"]] = ServerState(
                "unreachable", old.entries if old else [], reason, now_iso(), None, old.tools_hash if old else None)
            client = self._clients.get(row["id"])
        if client:
            client.reset()  # next attempt does a fresh handshake (the pod may have restarted)
        log.warning("upstream %s unreachable: %s", row["slug"], reason)

    def refresh_all(self) -> None:
        rows = self.enabled_servers()
        futures = [self._pool.submit(self.refresh, r["id"]) for r in rows]
        for f in futures:
            try:
                f.result(timeout=self.settings.list_timeout + self.settings.connect_timeout + 5)
            except Exception:  # one wedged refresh must not stall the loop
                log.exception("refresh task failed")

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="health-loop", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh_all()
            except Exception:
                log.exception("health loop iteration failed")
            self._stop.wait(self.settings.health_interval)

    def stop(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ---------------------------------------------------------------- catalog
    def visible_entries(self) -> list[tuple[dict, ToolEntry]]:
        """(server row, tool) for every tool of every enabled AND connected server."""
        out = []
        for row in self.enabled_servers():
            st = self.state(row["id"])
            if st and st.status == "connected":
                out.extend((row, e) for e in st.entries)
        return out

    def resolve(self, public: str) -> tuple[dict, ToolEntry] | None:
        for row, entry in self.visible_entries():
            if entry.public_name == public:
                return row, entry
        return None

    def locate(self, public: str) -> tuple[dict, ToolEntry] | None:
        """Like resolve() but also finds tools of enabled servers that are currently
        unreachable, so the caller can report *why* a call failed."""
        for row in self.enabled_servers():
            st = self.state(row["id"])
            if st:
                entry = st.by_name.get(public)
                if entry:
                    return row, entry
        return None

    # ----------------------------------------------------------- seeding
    def seed(self, servers: tuple[dict, ...], env: dict | None = None) -> None:
        """Register servers declared in configuration (Helm values). Idempotent:
        existing slugs are left alone so admin edits are never overwritten."""
        env = os.environ if env is None else env
        for spec in servers:
            if self.db.one("SELECT id FROM servers WHERE slug = ?", (spec["slug"],)):
                continue
            data = dict(spec)
            cred_env = data.pop("credential_env", None)
            if cred_env:
                token = env.get(cred_env)
                if not token:
                    log.error("seed server %s: env var %s is empty, registering without credential", spec["slug"], cred_env)
                else:
                    data.update(auth_type="bearer", auth_token=token)
            try:
                self.create(data, managed=True, refresh=False)  # health loop connects
                log.info("seeded server %s", spec["slug"])
            except ValidationError as exc:
                log.error("cannot seed server %s: %s", spec.get("slug"), exc)


def matches_any(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)
