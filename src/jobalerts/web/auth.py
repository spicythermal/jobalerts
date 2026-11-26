"""Login for the web UI: one admin password, signed session cookies, CSRF tokens, a login rate limit,
the first-run setup code and the read-only share token for the Home Assistant map card.

Standard library only:
- Password: scrypt (n=2**14, r=8, p=1) with a random salt, stored in the `meta` table.
- Session: a cookie holding JSON {csrf, user, ver, exp}, signed with HMAC-SHA256 under a key kept in
  the data folder (`secret.key`, 0600). Changing the password bumps `ver`, which logs out every
  other session. HttpOnly, SameSite=Lax, and Secure when `web.secure_cookies` is set.
- CSRF: every visitor's session carries a random token; every POST must send it back as the `csrf`
  form field or the `X-CSRF-Token` header (htmx sends the header, see base.html).
- First run: with no password yet, a one-time setup code is printed to the log; /setup asks for it,
  so the first device on the network can't claim the admin account.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field

from ..vault import load_or_create_key  # noqa: F401 (re-exported for the web app)

SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
COOKIE = "jobalerts_session"
MIN_PASSWORD = 10


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **SCRYPT)
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${_b64(salt)}${_b64(digest)}"


def check_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=_unb64(salt), dklen=32, n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, _unb64(digest))


def password_problem(password: str, again: str) -> str:
    if password != again:
        return "The two passwords don't match."
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    return ""


class Signer:
    """Sign and check small JSON payloads (the session cookie)."""

    def __init__(self, key: bytes):
        self._key = hashlib.sha256(b"jobalerts session v1" + key).digest()

    def dumps(self, payload: dict) -> str:
        body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        return body + "." + _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())

    def loads(self, token: str) -> dict | None:
        body, _, sig = (token or "").partition(".")
        if not body or not sig:
            return None
        want = hmac.new(self._key, body.encode(), hashlib.sha256).digest()
        try:
            if not hmac.compare_digest(want, _unb64(sig)):
                return None
            payload = json.loads(_unb64(body))
        except (ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None


@dataclass
class Session:
    csrf: str
    user: str = ""        # "admin" once logged in
    ver: int = 0          # password version it was issued under
    exp: int = 0          # unix time
    changed: bool = False  # needs a new cookie on the response

    @classmethod
    def new(cls) -> Session:
        return cls(csrf=secrets.token_urlsafe(24), changed=True)

    def payload(self) -> dict:
        return {"csrf": self.csrf, "user": self.user, "ver": self.ver, "exp": self.exp}


@dataclass
class LoginLimiter:
    """After `free` failures from one address, each further try waits 30 s, doubling up to 15 min."""
    free: int = 5
    failures: dict[str, tuple[int, float]] = field(default_factory=dict)  # ip -> (count, locked until)

    def wait(self, ip: str, now: float | None = None) -> int:
        count, until = self.failures.get(ip, (0, 0.0))
        return max(0, int(until - (now or time.time()) + 0.999))

    def failed(self, ip: str, now: float | None = None) -> None:
        now = now or time.time()
        count = self.failures.get(ip, (0, 0.0))[0] + 1
        lock = 0 if count < self.free else min(30 * 2 ** (count - self.free), 900)
        self.failures[ip] = (count, now + lock)

    def succeeded(self, ip: str) -> None:
        self.failures.pop(ip, None)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def tokens_match(given: str | None, expected: str | None) -> bool:
    return bool(given and expected) and hmac.compare_digest(given.encode(), expected.encode())
