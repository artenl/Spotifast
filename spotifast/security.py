"""Password hashing, session tokens and login throttling."""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()  # argon2id, RFC 9106 low-memory profile
# Verified against when the username doesn't exist, so a wrong username
# costs the same time as a wrong password (no account enumeration).
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))

MIN_PASSWORD_LEN = 12
MAX_PASSWORD_LEN = 128
# No 0/O, 1/l/I: temporary passwords get read aloud or retyped from a phone.
_TEMP_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def generate_temp_password() -> str:
    groups = ("".join(secrets.choice(_TEMP_ALPHABET) for _ in range(4)) for _ in range(4))
    return "-".join(groups)


def password_problem(password: str, username: str) -> str | None:
    """Return a French error message, or None when the password is acceptable."""
    if len(password) < MIN_PASSWORD_LEN:
        return f"Le mot de passe doit faire au moins {MIN_PASSWORD_LEN} caractères."
    if len(password) > MAX_PASSWORD_LEN:
        return f"Le mot de passe doit faire au plus {MAX_PASSWORD_LEN} caractères."
    if username.lower() in password.lower():
        return "Le mot de passe ne doit pas contenir le nom d'utilisateur."
    if len(set(password)) < 5:
        return "Le mot de passe est trop répétitif."
    return None


def new_session_token() -> tuple[str, str]:
    """Return (token for the cookie, hash stored server-side).

    Only the hash is persisted, so a leaked database can't be replayed as
    live sessions.
    """
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


class LoginThrottle:
    """Sliding-window failure counters.

    - per (account, IP): the normal lockout, after `max_failures`;
    - per IP: an IP guessing across many accounts (4x, since friends may
      share a home connection);
    - per account, all IPs: distributed guessing (10x). Kept well above the
      per-pair limit so a stranger who knows a username can't lock its
      owner out with a handful of tries.

    In-memory on purpose: the app runs as a single process, and a restart
    clearing the counters is acceptable.
    """

    def __init__(self, max_failures: int, window_s: int):
        self.max_failures = max_failures
        self.window_s = window_s
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _limits(self, ip: str, username: str) -> list[tuple[str, int]]:
        name = username.lower()
        return [
            (f"pair:{name}|{ip}", self.max_failures),
            (f"ip:{ip}", self.max_failures * 4),
            (f"user:{name}", self.max_failures * 10),
        ]

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._failures[key]
        while q and q[0] <= now - self.window_s:
            q.popleft()
        return q

    def retry_after(self, ip: str, username: str) -> int:
        """Seconds until a new attempt is allowed, 0 when allowed now."""
        now = time.monotonic()
        wait = 0.0
        with self._lock:
            for key, limit in self._limits(ip, username):
                q = self._prune(key, now)
                if len(q) >= limit:
                    wait = max(wait, q[0] + self.window_s - now)
        return int(wait) + 1 if wait > 0 else 0

    def record_failure(self, ip: str, username: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key, _limit in self._limits(ip, username):
                self._failures[key].append(now)

    def reset(self, ip: str, username: str) -> None:
        with self._lock:
            # Success clears this account+IP pair only; an IP that has been
            # guessing at other accounts stays throttled.
            self._failures.pop(self._limits(ip, username)[0][0], None)


class RateLimiter:
    """Simple fixed-budget limiter for cheap-but-abusable endpoints."""

    def __init__(self, limit: int, window_s: int):
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - self.window_s:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True
