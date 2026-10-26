"""Application factory: wires config, storage, registry, service, auth, chat and HTTP."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import requests
from flask import Flask, Response, jsonify, request

from . import api, mcp_endpoint
from .audit import AuditLog
from .auth import Auth
from .chat import ChatAgent, LlmClient
from .config import Settings
from .db import Database
from .metrics import Metrics
from .ratelimit import LoginThrottle, RateLimiter
from .registry import Registry
from .security import Crypto
from .service import GatewayService

log = logging.getLogger("gateway")


def create_app(settings: Settings, start_background: bool = True,
               llm_session: requests.Session | None = None) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 1_000_000

    db = Database(settings.db_path)
    crypto = Crypto(settings.encryption_keys)
    metrics, audit = Metrics(), AuditLog(db)
    holder = SimpleNamespace(service=None)
    registry = Registry(db, crypto, settings,
                        event_hook=lambda slug, outcome, detail: holder.service.system_event(slug, outcome, detail))
    service = GatewayService(settings, registry, audit, metrics, RateLimiter(settings.rate_limit_per_minute))
    holder.service = service
    auth = Auth(db, settings)
    chat = ChatAgent(service, LlmClient(settings.llm_base_url, settings.llm_api_key, settings.llm_model,
                                        settings.llm_timeout, llm_session), settings)

    app.extensions["ctx"] = SimpleNamespace(settings=settings, db=db, registry=registry, service=service,
                                            auth=auth, audit=audit, metrics=metrics, chat=chat,
                                            throttle=LoginThrottle())
    auth.bootstrap_admin()
    registry.seed(settings.seed_servers)
    app.register_blueprint(api.bp)
    app.register_blueprint(mcp_endpoint.bp)

    @app.get("/healthz")
    def healthz():  # liveness: the process is up and serving
        return jsonify(status="ok")

    @app.get("/readyz")
    def readyz():  # readiness: can we reach our database?
        try:
            db.one("SELECT 1")
        except Exception:
            return jsonify(status="db unavailable"), 503
        return jsonify(status="ready")

    @app.get("/metrics")
    def metrics_endpoint():  # not routed by the Ingress: reachable only inside the cluster
        status = {r["slug"]: registry.status_of(r) for r in registry.list_servers()}
        return Response(metrics.render(status), mimetype="text/plain; version=0.0.4")

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        if request.path.startswith(("/api", "/mcp")):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    if start_background:
        registry.start()
    return app
