"""REST API consumed by the dashboard (cookie-session auth, JSON only).

Security notes
  * Session cookie: HttpOnly, SameSite=Strict, Secure when served over TLS.
  * CSRF: SameSite=Strict + JSON-only bodies + Origin check on unsafe methods.
  * Authorisation is checked server-side on every route; the UI merely hides things.
  * Upstream credentials are write-only: no endpoint ever returns them.
"""
from __future__ import annotations

import json
import logging
from functools import wraps
from urllib.parse import urlsplit

from flask import Blueprint, current_app, g, jsonify, request

from .auth import AuthError
from .chat import ChatError
from .registry import NotFound, ValidationError
from .service import User

log = logging.getLogger("gateway.api")
bp = Blueprint("api", __name__, url_prefix="/api")
COOKIE = "gw_session"


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def ctx():
    return current_app.extensions["ctx"]


# ---------------------------------------------------------------- plumbing
@bp.errorhandler(ApiError)
def _api_error(e: ApiError):
    return jsonify(error=e.message), e.status


@bp.errorhandler(AuthError)
@bp.errorhandler(ValidationError)
def _bad_request(e):
    return jsonify(error=str(e)), 400


@bp.errorhandler(NotFound)
def _not_found(e):
    return jsonify(error=str(e)), 404


@bp.errorhandler(ChatError)
def _chat_error(e: ChatError):
    return jsonify(error=e.message), e.status


@bp.before_request
def _csrf_guard():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            raise ApiError(403, "cross-origin request rejected")


def body() -> dict:
    if request.content_length and not request.is_json:
        raise ApiError(415, "Content-Type must be application/json")
    data = request.get_json(silent=True) if request.content_length else {}
    if not isinstance(data, dict):
        raise ApiError(400, "request body must be a JSON object")
    return data


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        token = request.cookies.get(COOKIE)
        user = ctx().auth.user_for_session(token) if token else None
        if user is None:
            raise ApiError(401, "not signed in")
        g.user = user
        return fn(*a, **kw)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*a, **kw):
        if not g.user.is_admin:
            raise ApiError(403, "admin only")
        return fn(*a, **kw)
    return wrapper


def user_json(u: User) -> dict:
    return {"id": u.id, "email": u.email, "name": u.name, "role": u.role}


def _set_cookie(resp, token: str):
    s = ctx().settings
    resp.set_cookie(COOKIE, token, httponly=True, samesite="Strict", secure=s.cookie_secure,
                    max_age=s.session_ttl_hours * 3600, path="/")
    return resp


# -------------------------------------------------------------------- auth
@bp.post("/auth/login")
def login():
    data = body()
    email = str(data.get("email", "")).strip().lower()
    key = f"{request.remote_addr}|{email}"
    c = ctx()
    if c.throttle.blocked(key):
        raise ApiError(429, "too many failed attempts; try again in a few minutes")
    user = c.auth.authenticate_password(email, str(data.get("password", "")))
    if user is None:
        c.throttle.fail(key)
        raise ApiError(401, "invalid email or password")
    c.throttle.reset(key)
    return _set_cookie(jsonify(user=user_json(user)), c.auth.create_session(user.id))


@bp.post("/auth/logout")
def logout():
    token = request.cookies.get(COOKIE)
    if token:
        ctx().auth.end_session(token)
    resp = jsonify(ok=True)
    resp.delete_cookie(COOKIE, path="/")
    return resp


@bp.get("/me")
@login_required
def me():
    return jsonify(user=user_json(g.user))


@bp.get("/config")
@login_required
def config():
    s = ctx().settings
    return jsonify(gateway_url=f"{s.public_url}/mcp", chat_enabled=bool(s.llm_api_key), chat_model=s.llm_model)


@bp.post("/invites/accept")
def accept_invite():
    data = body()
    c = ctx()
    user = c.auth.accept_invite(str(data.get("token", "")), str(data.get("name", "")), str(data.get("password", "")))
    return _set_cookie(jsonify(user=user_json(user)), c.auth.create_session(user.id)), 201


# --------------------------------------------------------- team management
@bp.get("/users")
@admin_required
def users():
    return jsonify(users=ctx().auth.list_users(), invites=ctx().auth.list_invites())


@bp.patch("/users/<int:user_id>")
@admin_required
def patch_user(user_id: int):
    data = body()
    ctx().auth.update_user(user_id, data.get("role"), data.get("disabled") if "disabled" in data else None)
    return jsonify(ok=True)


@bp.post("/invites")
@admin_required
def invite():
    data = body()
    token = ctx().auth.create_invite(g.user.id, str(data.get("email", "")), str(data.get("role", "member")))
    # The token is shown once; only its hash is stored. There is no mailer: the admin shares the link.
    return jsonify(token=token, url=f"{ctx().settings.public_url}/#/invite/{token}"), 201


@bp.get("/keys")
@login_required
def list_keys():
    show_all = request.args.get("all") == "1" and g.user.is_admin
    return jsonify(keys=ctx().auth.list_keys(None if show_all else g.user.id))


@bp.post("/keys")
@login_required
def create_key():
    data = body()
    target = g.user.id
    if data.get("user_id") not in (None, g.user.id):
        if not g.user.is_admin:
            raise ApiError(403, "only admins can issue keys for other users")
        target = int(data["user_id"])
    key, secret = ctx().auth.create_key(target, str(data.get("name", "")))
    return jsonify(key=key, secret=secret), 201  # the only time the secret is ever returned


@bp.delete("/keys/<int:key_id>")
@login_required
def revoke_key(key_id: int):
    key = ctx().auth.get_key(key_id)
    if not key or (key["user_id"] != g.user.id and not g.user.is_admin):
        raise ApiError(404, "key not found")
    ctx().auth.revoke_key(key_id)
    return jsonify(ok=True)


# ----------------------------------------------------------------- servers
def server_view(row: dict, admin: bool, user: User) -> dict:
    c = ctx()
    st = c.registry.state(row["id"])
    tools = []
    for e in (st.entries if st else []):
        if not c.service.allowed(row, e.tool_name, user):
            continue
        tools.append({"name": e.public_name, "tool": e.tool_name, "description": e.definition["description"]})
    view = {"id": row["id"], "slug": row["slug"], "name": row["name"], "enabled": bool(row["enabled"]),
            "status": c.registry.status_of(row), "tools": tools, "checked_at": st.checked_at if st else None,
            "latency_ms": st.latency_ms if st else None, "managed": bool(row["managed"])}
    if admin:
        view.update(url=row["url"], auth_type=row["auth_type"], has_credential=bool(row["credential_enc"]),
                    tool_policy=json.loads(row["tool_policy"] or "{}"), last_error=st.last_error if st else None)
    return view


@bp.get("/servers")
@login_required
def servers():
    rows = ctx().registry.list_servers()
    if not g.user.is_admin:
        rows = [r for r in rows if r["enabled"]]
    return jsonify(servers=[server_view(r, g.user.is_admin, g.user) for r in rows])


@bp.post("/servers")
@admin_required
def add_server():
    row = ctx().registry.create(body())
    return jsonify(server=server_view(row, True, g.user)), 201


@bp.patch("/servers/<int:server_id>")
@admin_required
def edit_server(server_id: int):
    row = ctx().registry.update(server_id, body())
    return jsonify(server=server_view(row, True, g.user))


@bp.delete("/servers/<int:server_id>")
@admin_required
def remove_server(server_id: int):
    ctx().registry.delete(server_id)
    return jsonify(ok=True)


@bp.post("/servers/<int:server_id>/refresh")
@admin_required
def refresh_server(server_id: int):
    ctx().registry.refresh(server_id)
    return jsonify(server=server_view(ctx().registry.get(server_id), True, g.user))


# ------------------------------------------------------------- audit & chat
@bp.get("/audit")
@login_required
def audit():
    a = request.args
    uid = None if g.user.is_admin else g.user.id  # members only ever see their own calls
    if g.user.is_admin and a.get("user_id"):
        uid = int(a["user_id"])
    result = ctx().audit.query(user_id=uid, server=a.get("server") or None, outcome=a.get("outcome") or None,
                               limit=int(a.get("limit", 50)), offset=int(a.get("offset", 0)))
    return jsonify(**result)


@bp.post("/chat")
@login_required
def chat():
    data = body()
    return jsonify(ctx().chat.run(g.user, data.get("messages")))
