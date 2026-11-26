"""Accounts for the web UI: a username, a scrypt password hash and a role.

- admin: everything, including users, job boards, the install's API keys and updates.
- user: their own jobs, filter, stories, profile, resumes, situation, alerts and keys.

User 1 is the install's owner (the first admin, made at setup, or the single password of versions
before accounts). Their files stay at the top of the data folder; other users' are in
data/users/<id>/. User 1 can't be deleted, demoted or disabled, and there's always an enabled admin.
A password an admin sets (a new account, a reset) is shown once and must be changed at the next login.
"""
from __future__ import annotations

import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from .models import utcnow
from .web.auth import check_password, hash_password

OWNER = 1
ROLES = ("admin", "user")
USERNAME = re.compile(r"^[a-z0-9._-]{2,32}$")
# Checked when the username doesn't exist, so a wrong name takes as long as a wrong password.
_DUMMY = hash_password(secrets.token_urlsafe(16))


@dataclass(frozen=True)
class User:
    id: int
    username: str
    role: str
    ver: int
    must_change: bool
    disabled: bool
    created_at: str
    last_login_at: str | None

    @property
    def admin(self) -> bool:
        return self.role == "admin"

    @classmethod
    def from_row(cls, r) -> User:
        return cls(r["id"], r["username"], r["role"], r["ver"], bool(r["must_change"]), bool(r["disabled"]),
                   r["created_at"], r["last_login_at"])


class UserError(ValueError):
    pass


def check_username(name: str) -> str:
    name = (name or "").strip().lower()
    if not USERNAME.match(name):
        raise UserError("A username has 2 to 32 characters: a-z, 0-9, dot, dash, underscore.")
    return name


def one_time_password() -> str:
    """For a new account or a reset: four groups of four, easy to read out."""
    raw = secrets.token_hex(8)
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def user_folder(data_dir: Path, uid: int) -> Path:
    """Where a user's profile, stories and resumes live (user 1: the data folder itself)."""
    return data_dir if uid == OWNER else data_dir / "users" / str(int(uid))


class Users:
    def __init__(self, store):
        self.store, self.db = store, store.db

    def _now(self) -> str:
        return utcnow().isoformat(timespec="seconds")

    # ---- reading
    def all(self) -> list[User]:
        return [User.from_row(r) for r in self.db.execute("SELECT * FROM users ORDER BY id")]

    def get(self, uid) -> User | None:
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            return None
        r = self.db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return User.from_row(r) if r else None

    def by_name(self, name: str) -> User | None:
        r = self.db.execute("SELECT * FROM users WHERE username=?", ((name or "").strip().lower(),)).fetchone()
        return User.from_row(r) if r else None

    def any(self) -> bool:
        return self.db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    # ---- sessions ended by logging out
    def end_session(self, sid: str, expires_at: int) -> None:
        now = int(utcnow().timestamp())
        self.db.execute("DELETE FROM ended_sessions WHERE expires_at<?", (now,))
        self.db.execute("INSERT OR IGNORE INTO ended_sessions (sid, expires_at) VALUES (?, ?)", (sid, expires_at))

    def session_ended(self, sid: str) -> bool:
        return self.db.execute("SELECT 1 FROM ended_sessions WHERE sid=?", (sid,)).fetchone() is not None

    def enabled_admins(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0").fetchone()[0]

    def authenticate(self, name: str, password: str) -> User | None:
        """The user, if the name and password match and the account is enabled. The same work is done
        for an unknown name, so the answer doesn't tell which names exist."""
        r = self.db.execute("SELECT * FROM users WHERE username=?", ((name or "").strip().lower(),)).fetchone()
        ok = check_password(password, r["password_hash"] if r else _DUMMY)
        if not r or not ok or r["disabled"]:
            return None
        self.db.execute("UPDATE users SET last_login_at=? WHERE id=?", (self._now(), r["id"]))
        return User.from_row(r)

    def password_ok(self, uid: int, password: str) -> bool:
        r = self.db.execute("SELECT password_hash FROM users WHERE id=?", (uid,)).fetchone()
        return bool(r) and check_password(password, r["password_hash"])

    # ---- writing
    def create(self, name: str, password: str, role: str = "user", must_change: bool = True) -> User:
        name = check_username(name)
        if role not in ROLES:
            raise UserError("Unknown role.")
        if self.by_name(name):
            raise UserError(f"There is already a user called {name}.")
        first = not self.any()
        cur = self.db.execute(
            "INSERT INTO users (id, username, password_hash, role, must_change, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (OWNER if first else None, name, hash_password(password), "admin" if first else role,
             int(must_change and not first), self._now()))
        user = self.get(cur.lastrowid)
        if user.id == OWNER:
            self._mirror(user.id)
        return user

    def set_password(self, uid: int, password: str, must_change: bool = False) -> None:
        """New password; every session of this user is logged out (the caller logs the current one in again)."""
        self.db.execute("UPDATE users SET password_hash=?, must_change=?, ver=ver+1 WHERE id=?",
                        (hash_password(password), int(must_change), uid))
        self._mirror(uid)

    def _mirror(self, uid: int) -> None:
        """User 1's password also where versions before accounts look for it, so going back still works."""
        if uid == OWNER:
            r = self.db.execute("SELECT password_hash FROM users WHERE id=?", (uid,)).fetchone()
            self.store.set_meta("web.password_hash", r["password_hash"])

    def set_role(self, uid: int, role: str) -> None:
        user = self.get(uid)
        if user is None or role not in ROLES:
            raise UserError("Unknown user or role.")
        if role != "admin" and user.admin:
            if uid == OWNER:
                raise UserError("The first admin stays an admin.")
            if not user.disabled and self.enabled_admins() <= 1:
                raise UserError("There must be another admin first.")
        self.db.execute("UPDATE users SET role=?, ver=ver+1 WHERE id=?", (role, uid))

    def set_disabled(self, uid: int, disabled: bool) -> None:
        user = self.get(uid)
        if user is None:
            raise UserError("Unknown user.")
        if disabled and uid == OWNER:
            raise UserError("The first admin can't be disabled.")
        if disabled and user.admin and not user.disabled and self.enabled_admins() <= 1:
            raise UserError("There must be another admin first.")
        self.db.execute("UPDATE users SET disabled=?, ver=ver+1 WHERE id=?", (int(disabled), uid))

    def delete(self, uid: int, data_dir: Path | None = None) -> None:
        """The account, its job decisions, settings and keys. Its folder moves to data/users/.trash/."""
        user = self.get(uid)
        if user is None:
            raise UserError("Unknown user.")
        if uid == OWNER:
            raise UserError("The first admin can't be deleted.")
        if user.admin and not user.disabled and self.enabled_admins() <= 1:
            raise UserError("There must be another admin first.")
        self.db.execute("DELETE FROM users WHERE id=?", (uid,))
        for table in ("settings_by_user", "secrets_by_user"):
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                self.db.execute(f"DELETE FROM {table} WHERE user_id=?", (uid,))
        self.store.forget_user(uid)
        if data_dir is not None:
            folder = user_folder(data_dir, uid)
            if folder.is_dir():
                trash = data_dir / "users" / ".trash"
                trash.mkdir(parents=True, exist_ok=True)
                shutil.move(str(folder), trash / f"{uid}-{user.username}-{utcnow():%Y%m%d-%H%M%S}")
