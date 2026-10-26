"""Users, dashboard sessions, invitations and gateway API keys."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from .config import Settings
from .db import Database, now_iso
from .security import hash_password, hash_token, new_token, verify_password
from .service import User

log = logging.getLogger("gateway.auth")

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
MIN_PASSWORD = 10
KEY_PREFIX = "gw_"
# Verifying against this when the email is unknown keeps login timing uniform.
_DUMMY_HASH = hash_password("dummy-password-for-timing")


class AuthError(ValueError):
    """Bad input that should become a 4xx with this message."""


def _iso_in(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")


def _row_to_user(row: dict) -> User:
    return User(id=row["id"], email=row["email"], name=row["name"], role=row["role"])


class Auth:
    def __init__(self, db: Database, settings: Settings):
        self.db, self.settings = db, settings

    # --------------------------------------------------------------- users
    def create_user(self, email: str, name: str, role: str, password: str) -> User:
        email = (email or "").strip().lower()
        if not EMAIL_RE.match(email):
            raise AuthError("invalid email address")
        if role not in ("admin", "member"):
            raise AuthError("role must be admin or member")
        if len(password or "") < MIN_PASSWORD:
            raise AuthError(f"password must be at least {MIN_PASSWORD} characters")
        name = (name or "").strip()[:80] or email.split("@")[0]
        if self.db.one("SELECT id FROM users WHERE email = ?", (email,)):
            raise AuthError("a user with this email already exists")
        user_id = self.db.run(
            "INSERT INTO users (email,name,role,password_hash,created_at) VALUES (?,?,?,?,?)",
            (email, name, role, hash_password(password), now_iso()))
        return _row_to_user(self.db.one("SELECT * FROM users WHERE id = ?", (user_id,)))

    def bootstrap_admin(self) -> None:
        """Create the first admin from the configured Secret, only if no admin exists."""
        s = self.settings
        if self.db.one("SELECT id FROM users WHERE role = 'admin'"):
            return
        if not (s.admin_email and s.admin_password):
            log.warning("no admin exists and ADMIN_EMAIL/ADMIN_PASSWORD are not set; nobody can log in")
            return
        self.create_user(s.admin_email, "Admin", "admin", s.admin_password)
        log.info("bootstrapped admin %s", s.admin_email)

    def list_users(self) -> list[dict]:
        return self.db.all(
            "SELECT u.id, u.email, u.name, u.role, u.disabled, u.created_at,"
            " (SELECT COUNT(*) FROM api_keys k WHERE k.user_id = u.id AND k.revoked_at IS NULL) AS active_keys"
            " FROM users u ORDER BY u.id")

    def update_user(self, user_id: int, role: str | None, disabled: bool | None) -> None:
        row = self.db.one("SELECT * FROM users WHERE id = ?", (user_id,))
        if not row:
            raise AuthError("user not found")
        new_role = role if role is not None else row["role"]
        new_disabled = int(disabled) if disabled is not None else row["disabled"]
        if new_role not in ("admin", "member"):
            raise AuthError("role must be admin or member")
        losing_admin = row["role"] == "admin" and not row["disabled"] and (new_role != "admin" or new_disabled)
        if losing_admin and self.db.one(
                "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND disabled=0")["n"] <= 1:
            raise AuthError("cannot remove or disable the last active admin")
        self.db.run("UPDATE users SET role = ?, disabled = ? WHERE id = ?", (new_role, new_disabled, user_id))
        if new_disabled:
            self.db.run("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def authenticate_password(self, email: str, password: str) -> User | None:
        row = self.db.one("SELECT * FROM users WHERE email = ?", ((email or "").strip().lower(),))
        ok = verify_password(password or "", row["password_hash"] if row else _DUMMY_HASH)
        if not row or not ok or row["disabled"]:
            return None
        return _row_to_user(row)

    # ------------------------------------------------------------ sessions
    def create_session(self, user_id: int) -> str:
        token = new_token("s_")
        self.db.run("INSERT INTO sessions (token_hash,user_id,created_at,expires_at) VALUES (?,?,?,?)",
                    (hash_token(token), user_id, now_iso(), _iso_in(self.settings.session_ttl_hours)))
        return token

    def user_for_session(self, token: str) -> User | None:
        row = self.db.one(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_at > ? AND u.disabled = 0", (hash_token(token), now_iso()))
        return _row_to_user(row) if row else None

    def end_session(self, token: str) -> None:
        self.db.run("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))
        self.db.run("DELETE FROM sessions WHERE expires_at <= ?", (now_iso(),))  # opportunistic cleanup

    # ------------------------------------------------------------- invites
    def create_invite(self, created_by: int, email: str, role: str) -> str:
        email = (email or "").strip().lower()
        if not EMAIL_RE.match(email):
            raise AuthError("invalid email address")
        if role not in ("admin", "member"):
            raise AuthError("role must be admin or member")
        if self.db.one("SELECT id FROM users WHERE email = ?", (email,)):
            raise AuthError("a user with this email already exists")
        token = new_token("inv_")
        self.db.run("INSERT INTO invites (token_hash,email,role,created_by,created_at,expires_at) VALUES (?,?,?,?,?,?)",
                    (hash_token(token), email, role, created_by, now_iso(), _iso_in(self.settings.invite_ttl_hours)))
        return token

    def list_invites(self) -> list[dict]:
        return self.db.all("SELECT id, email, role, created_at, expires_at, used_at FROM invites ORDER BY id DESC LIMIT 100")

    def accept_invite(self, token: str, name: str, password: str) -> User:
        row = self.db.one("SELECT * FROM invites WHERE token_hash = ?", (hash_token(token or ""),))
        if not row or row["used_at"] or row["expires_at"] <= now_iso():
            raise AuthError("this invitation is invalid or has expired")
        user = self.create_user(row["email"], name, row["role"], password)
        self.db.run("UPDATE invites SET used_at = ? WHERE id = ?", (now_iso(), row["id"]))
        return user

    # ------------------------------------------------------------- API keys
    def create_key(self, user_id: int, name: str) -> tuple[dict, str]:
        """Returns (key metadata, plaintext secret). The secret exists only in this return value."""
        name = (name or "").strip()[:80] or "default"
        if not self.db.one("SELECT id FROM users WHERE id = ? AND disabled = 0", (user_id,)):
            raise AuthError("user not found or disabled")
        secret = new_token(KEY_PREFIX)
        key_id = self.db.run(
            "INSERT INTO api_keys (user_id,name,prefix,key_hash,created_at) VALUES (?,?,?,?,?)",
            (user_id, name, secret[:9], hash_token(secret), now_iso()))
        return self.db.one("SELECT id,user_id,name,prefix,created_at,last_used_at,revoked_at FROM api_keys WHERE id = ?",
                           (key_id,)), secret

    def list_keys(self, user_id: int | None) -> list[dict]:
        sql = ("SELECT k.id,k.user_id,u.email AS user_email,k.name,k.prefix,k.created_at,k.last_used_at,k.revoked_at "
               "FROM api_keys k JOIN users u ON u.id = k.user_id")
        if user_id is None:
            return self.db.all(sql + " ORDER BY k.id DESC")
        return self.db.all(sql + " WHERE k.user_id = ? ORDER BY k.id DESC", (user_id,))

    def get_key(self, key_id: int) -> dict | None:
        return self.db.one("SELECT * FROM api_keys WHERE id = ?", (key_id,))

    def revoke_key(self, key_id: int) -> None:
        self.db.run("UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (now_iso(), key_id))

    def user_for_key(self, secret: str) -> User | None:
        """Hash lookup -> user. Revoked keys and disabled users are rejected on every request."""
        if not secret.startswith(KEY_PREFIX):
            return None
        row = self.db.one(
            "SELECT u.*, k.id AS key_id, k.last_used_at AS key_last_used FROM api_keys k "
            "JOIN users u ON u.id = k.user_id WHERE k.key_hash = ? AND k.revoked_at IS NULL AND u.disabled = 0",
            (hash_token(secret),))
        if not row:
            return None
        last = row["key_last_used"]
        if not last or (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() > 60:
            self.db.run("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now_iso(), row["key_id"]))
        return _row_to_user(row)
