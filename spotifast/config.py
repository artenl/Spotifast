"""Runtime settings, read once from SPOTIFAST_* environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(f"SPOTIFAST_{name}", default).strip()


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Public origin the browser uses, e.g. https://music.example.fr. Unsafe
    # requests whose Origin doesn't match are refused (CSRF defence).
    public_origin: str = field(default_factory=lambda: _env("PUBLIC_ORIGIN", "http://127.0.0.1:8000"))
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "./.data")).resolve())
    work_dir: Path = field(default_factory=lambda: Path(_env("WORK_DIR", "./.work")).resolve())
    sunnify_bin: str = field(default_factory=lambda: _env("SUNNIFY_BIN", "sunnify"))

    # Cookies are Secure (and use the __Host- prefix) unless explicitly
    # disabled for plain-http local development.
    cookie_secure: bool = field(default_factory=lambda: _bool("COOKIE_SECURE", True))
    session_idle_s: int = field(default_factory=lambda: _int("SESSION_IDLE_DAYS", 7) * 86400)
    session_max_s: int = field(default_factory=lambda: _int("SESSION_MAX_DAYS", 30) * 86400)

    # Load limits: they protect the VPS and, as much, its IP's standing
    # with YouTube (bursts get the whole server bot-checked).
    max_concurrent_jobs: int = field(default_factory=lambda: _int("MAX_CONCURRENT_JOBS", 2))
    max_active_jobs_per_user: int = field(default_factory=lambda: _int("MAX_ACTIVE_JOBS_PER_USER", 1))
    max_tracks_per_job: int = field(default_factory=lambda: _int("MAX_TRACKS_PER_JOB", 150))
    daily_track_quota: int = field(default_factory=lambda: _int("DAILY_TRACK_QUOTA", 400))
    job_timeout_s: int = field(default_factory=lambda: _int("JOB_TIMEOUT_MIN", 45) * 60)
    # Finished files are deleted from the server after this long, or shortly
    # after the user has downloaded them, whichever comes first.
    result_ttl_s: int = field(default_factory=lambda: _int("RESULT_TTL_MIN", 20) * 60)
    after_download_ttl_s: int = field(default_factory=lambda: _int("AFTER_DOWNLOAD_TTL_S", 120))
    min_free_disk_mb: int = field(default_factory=lambda: _int("MIN_FREE_DISK_MB", 1500))

    login_max_failures: int = field(default_factory=lambda: _int("LOGIN_MAX_FAILURES", 5))
    login_window_s: int = field(default_factory=lambda: _int("LOGIN_WINDOW_MIN", 15) * 60)

    @property
    def cookie_name(self) -> str:
        # __Host- forbids Domain and requires Secure + Path=/, so the cookie
        # can't be set or shadowed by a sibling subdomain.
        return "__Host-spotifast" if self.cookie_secure else "spotifast"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "spotifast.sqlite3"


settings = Settings()
