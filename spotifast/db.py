"""SQLite storage: users, sessions and a job history.

One short-lived connection per operation keeps this safe to call from both
the event loop and Starlette's threadpool; the traffic is a handful of
friends, far below where that would matter.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    last_login_at INTEGER
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    audio_format TEXT NOT NULL,
    track_total INTEGER NOT NULL,
    status TEXT NOT NULL,
    landed INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    finished_at INTEGER
);
CREATE INDEX IF NOT EXISTS jobs_user_created ON jobs(user_id, created_at);
"""


@dataclass(frozen=True)
class User:
    id: int
    username: str
    password_hash: str
    is_admin: bool
    is_active: bool
    must_change_password: bool
    created_at: int
    last_login_at: int | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> User:
        return cls(
            id=row["id"],
            username=row["username"],
            password_hash=row["password_hash"],
            is_admin=bool(row["is_admin"]),
            is_active=bool(row["is_active"]),
            must_change_password=bool(row["must_change_password"]),
            created_at=row["created_at"],
            last_login_at=row["last_login_at"],
        )


@dataclass(frozen=True)
class Session:
    token_hash: str
    user: User
    csrf_token: str
    created_at: int
    last_seen_at: int


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # users -----------------------------------------------------------------

    def create_user(self, username: str, password_hash: str, *, is_admin: bool) -> User:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, is_admin, created_at) "
                "VALUES (?, ?, ?, ?)",
                (username, password_hash, int(is_admin), int(time.time())),
            )
            row = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
        return User.from_row(row)

    def get_user(self, user_id: int) -> User | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return User.from_row(row) if row else None

    def get_user_by_name(self, username: str) -> User | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return User.from_row(row) if row else None

    def list_users(self) -> list[User]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
        return [User.from_row(r) for r in rows]

    def count_admins(self) -> int:
        with self.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM users WHERE is_admin = 1 AND is_active = 1"
            ).fetchone()[0]

    def set_password(self, user_id: int, password_hash: str, *, must_change: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, must_change_password = ? WHERE id = ?",
                (password_hash, int(must_change), user_id),
            )

    def set_active(self, user_id: int, active: bool) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (int(active), user_id))
            if not active:
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def delete_user(self, user_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def touch_login(self, user_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET last_login_at = ? WHERE id = ?", (int(time.time()), user_id)
            )

    # sessions --------------------------------------------------------------

    def create_session(self, token_hash: str, user_id: int, csrf_token: str) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, csrf_token, created_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (token_hash, user_id, csrf_token, now, now),
            )

    def get_session(self, token_hash: str) -> Session | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT s.token_hash, s.csrf_token, s.created_at AS s_created, s.last_seen_at, u.* "
                "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
                (token_hash,),
            ).fetchone()
        if not row:
            return None
        return Session(
            token_hash=row["token_hash"],
            user=User.from_row(row),
            csrf_token=row["csrf_token"],
            created_at=row["s_created"],
            last_seen_at=row["last_seen_at"],
        )

    def touch_session(self, token_hash: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
                (int(time.time()), token_hash),
            )

    def delete_session(self, token_hash: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_user_sessions(self, user_id: int, *, keep: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM sessions WHERE user_id = ? AND token_hash IS NOT ?", (user_id, keep)
            )

    def purge_sessions(self, *, idle_s: int, max_s: int) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM sessions WHERE last_seen_at < ? OR created_at < ?",
                (now - idle_s, now - max_s),
            )

    # jobs ------------------------------------------------------------------

    def record_job(
        self,
        *,
        job_id: str,
        user_id: int,
        url: str,
        kind: str,
        title: str,
        audio_format: str,
        track_total: int,
        status: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO jobs (id, user_id, url, kind, title, audio_format, track_total, "
                "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, user_id, url, kind, title, audio_format, track_total, status,
                 int(time.time())),
            )

    def mark_job_running(self, job_id: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))

    def finish_job(self, job_id: str, *, status: str, landed: int, failed: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, landed = ?, failed = ?, finished_at = ? WHERE id = ?",
                (status, landed, failed, int(time.time()), job_id),
            )

    def mark_interrupted_jobs(self) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = 'interrupted', finished_at = ? "
                "WHERE status IN ('queued', 'running')",
                (int(time.time()),),
            )

    def tracks_requested_since(self, user_id: int, since: int) -> int:
        """Quota usage: tracks actually obtained by finished jobs, plus the
        full size of jobs still queued or running (reserved up front)."""
        with self.connect() as conn:
            return conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN status IN ('queued', 'running') "
                "THEN track_total ELSE landed END), 0) FROM jobs "
                "WHERE user_id = ? AND created_at >= ?",
                (user_id, since),
            ).fetchone()[0]

    def recent_jobs(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT j.*, u.username FROM jobs j JOIN users u ON u.id = j.user_id "
                "ORDER BY j.created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
