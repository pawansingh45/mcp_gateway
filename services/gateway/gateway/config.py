"""Runtime configuration, read once from environment variables.

Secrets (encryption keys, admin password, LLM key) arrive as env vars injected
from Kubernetes Secrets; nothing sensitive has a default value, and the process
refuses to start without an encryption key rather than silently running insecure.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    encryption_keys: tuple[str, ...]                 # Fernet keys; first one encrypts, all decrypt (rotation)
    db_path: str = "/data/gateway.db"
    admin_email: str | None = None
    admin_password: str | None = None
    public_url: str = "http://localhost:8080"
    cookie_secure: bool = False
    session_ttl_hours: int = 12
    invite_ttl_hours: int = 72
    connect_timeout: float = 3.0
    list_timeout: float = 5.0
    call_timeout: float = 20.0
    health_interval: float = 10.0
    max_result_chars: int = 50_000
    max_upstream_bytes: int = 2_000_000
    tool_description_max: int = 1024
    rate_limit_per_minute: int = 120                 # per user; 0 disables
    ssrf_allowed_hosts: tuple[str, ...] = ()
    ssrf_allow_loopback: bool = False
    allowed_origins: tuple[str, ...] = ()
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "openai/gpt-oss-120b"
    llm_api_key: str | None = None
    llm_timeout: float = 60.0
    chat_max_steps: int = 8
    chat_max_direct_tools: int = 30
    store_timezone: str = "UTC"
    seed_servers: tuple[dict, ...] = field(default_factory=tuple)

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        e = os.environ if env is None else env
        keys = tuple(k.strip() for k in e.get("GATEWAY_ENCRYPTION_KEYS", "").split(",") if k.strip())
        if not keys:
            raise RuntimeError("GATEWAY_ENCRYPTION_KEYS is required (comma-separated Fernet keys)")
        seeds = json.loads(e.get("SEED_SERVERS", "[]") or "[]")
        return cls(
            encryption_keys=keys,
            db_path=e.get("GATEWAY_DB_PATH", "/data/gateway.db"),
            admin_email=e.get("ADMIN_EMAIL") or None,
            admin_password=e.get("ADMIN_PASSWORD") or None,
            public_url=e.get("PUBLIC_URL", "http://localhost:8080").rstrip("/"),
            cookie_secure=_bool(e.get("COOKIE_SECURE")),
            session_ttl_hours=int(e.get("SESSION_TTL_HOURS", "12")),
            connect_timeout=float(e.get("CONNECT_TIMEOUT_SECONDS", "3")),
            list_timeout=float(e.get("LIST_TIMEOUT_SECONDS", "5")),
            call_timeout=float(e.get("CALL_TIMEOUT_SECONDS", "20")),
            health_interval=float(e.get("HEALTH_INTERVAL_SECONDS", "10")),
            max_result_chars=int(e.get("MAX_RESULT_CHARS", "50000")),
            rate_limit_per_minute=int(e.get("RATE_LIMIT_PER_MINUTE", "120")),
            ssrf_allowed_hosts=tuple(h.strip().lower() for h in e.get("SSRF_ALLOWED_HOSTS", "").split(",") if h.strip()),
            ssrf_allow_loopback=_bool(e.get("SSRF_ALLOW_LOOPBACK")),
            allowed_origins=tuple(o.strip().rstrip("/") for o in e.get("ALLOWED_ORIGINS", "").split(",") if o.strip()),
            llm_base_url=e.get("LLM_BASE_URL", cls.llm_base_url).rstrip("/"),
            llm_model=e.get("LLM_MODEL", cls.llm_model),
            llm_api_key=e.get("LLM_API_KEY") or None,
            chat_max_steps=int(e.get("CHAT_MAX_STEPS", "8")),
            chat_max_direct_tools=int(e.get("CHAT_MAX_DIRECT_TOOLS", "30")),
            store_timezone=e.get("STORE_TIMEZONE", "UTC"),
            seed_servers=tuple(seeds),
        )
