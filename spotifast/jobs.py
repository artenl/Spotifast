"""Download jobs: a bounded queue of `sunnify download` subprocesses.

Each job gets its own folder under the work dir. Files only live there
until the user has fetched them (or the TTL runs out); nothing is kept.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .db import Database, User
from .sunnify import AUDIO_EXTENSIONS, Preview, SpotifyRef, download_command
from .zipstream import collect_audio_files

log = logging.getLogger("spotifast.jobs")

JOB_ID = re.compile(r"^[A-Za-z0-9_-]{22}$")
ACTIVE = {"queued", "running"}
# Environment the CLI may inherit, beyond what we set ourselves. Proxy
# variables are passed through so an outbound proxy can be configured for
# YouTube without touching the code.
_PASSTHROUGH_ENV = (
    "PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)
_CLI_ERRORS = {
    "invalid_url": "Lien Spotify refusé par Sunnify.",
    "metadata_fetch_failed": "Impossible de lire cette playlist chez Spotify (est-elle publique ?).",
    "ffmpeg_missing": "FFmpeg est introuvable sur le serveur.",
    "out_dir_unusable": "Le dossier de travail du serveur est inutilisable.",
    "folder_locked": "Ce téléchargement est déjà en cours.",
    "run_failed": "Sunnify a rencontré une erreur.",
}
YOUTUBE_BLOCKED = (
    "YouTube demande au serveur de prouver qu'il n'est pas un robot et refuse les "
    "téléchargements. C'est l'IP du VPS qui est visée : réessaie dans quelques heures."
)


class JobRejected(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class Job:
    id: str
    user_id: int
    ref: SpotifyRef
    title: str
    cover: str
    audio_format: str
    quality: str
    track_total: int
    dir: Path
    status: str = "queued"
    landed: int = 0
    failed: int = 0
    failed_titles: list[str] = field(default_factory=list)
    recent: deque = field(default_factory=lambda: deque(maxlen=30))
    message: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    expires_at: float | None = None
    active_downloads: int = 0
    task: asyncio.Task | None = None
    proc: asyncio.subprocess.Process | None = None
    cancel_requested: bool = False
    version: int = 0
    _changed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def out_dir(self) -> Path:
        return self.dir / "out"

    def notify(self) -> None:
        self.version += 1
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    async def wait_change(self, seen: int, timeout: float) -> None:
        if self.version != seen:
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._changed.wait(), timeout)

    def result_files(self) -> list[tuple[Path, str]]:
        if self.status in ACTIVE or self.status == "expired":
            return []
        return collect_audio_files(self.out_dir, AUDIO_EXTENSIONS)

    def download_name(self) -> str:
        base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", self.title).strip(" .") or "spotifast"
        return base[:120]

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "kind": self.ref.kind,
            "url": self.ref.url,
            "title": self.title,
            "cover": self.cover,
            "format": self.audio_format,
            "quality": self.quality,
            "status": self.status,
            "trackTotal": self.track_total,
            "landed": self.landed,
            "failed": self.failed,
            "failedTitles": self.failed_titles[:50],
            "recent": list(self.recent),
            "message": self.message,
            "createdAt": int(self.created_at),
            "finishedAt": int(self.finished_at) if self.finished_at else None,
            "expiresAt": int(self.expires_at) if self.expires_at else None,
            "downloadable": self.status not in ACTIVE and self.status != "expired"
            and self.landed > 0,
            "version": self.version,
        }


class JobManager:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.jobs: dict[str, Job] = {}
        self._slots: asyncio.Semaphore | None = None
        self._janitor: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()

    # lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        self._slots = asyncio.Semaphore(self.settings.max_concurrent_jobs)
        work = self.settings.work_dir
        work.mkdir(parents=True, exist_ok=True)
        # Anything left in the work dir is from a previous run that didn't
        # shut down cleanly: nobody can download it any more, so drop it.
        for child in work.iterdir():
            if child.name != ".cache":
                _remove(child)
        self.db.mark_interrupted_jobs()
        self._janitor = asyncio.create_task(self._janitor_loop())

    async def stop(self) -> None:
        if self._janitor:
            self._janitor.cancel()
        for job in list(self.jobs.values()):
            if job.status in ACTIVE:
                job.cancel_requested = True
                if job.proc:
                    await _terminate(job.proc, grace=5)
                if job.task:
                    job.task.cancel()
        for job in list(self.jobs.values()):
            _remove(job.dir)

    # queries ---------------------------------------------------------------

    def get(self, job_id: str, user: User) -> Job | None:
        job = self.jobs.get(job_id) if JOB_ID.match(job_id) else None
        return job if job and job.user_id == user.id else None

    def for_user(self, user: User) -> list[Job]:
        jobs = [j for j in self.jobs.values() if j.user_id == user.id]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def running_count(self) -> int:
        return sum(1 for j in self.jobs.values() if j.status == "running")

    def queued_count(self) -> int:
        return sum(1 for j in self.jobs.values() if j.status == "queued")

    # submit / cancel -------------------------------------------------------

    def submit(self, user: User, preview: Preview, *, audio_format: str, quality: str) -> Job:
        s = self.settings
        if preview.truncated or preview.track_count > s.max_tracks_per_job:
            raise JobRejected(
                f"Trop de titres : la limite est de {s.max_tracks_per_job} par téléchargement."
            )
        if preview.track_count == 0:
            raise JobRejected("Aucun titre à télécharger.")
        active = [j for j in self.for_user(user) if j.status in ACTIVE]
        if len(active) >= s.max_active_jobs_per_user:
            raise JobRejected(
                "Tu as déjà un téléchargement en cours. Attends qu'il se termine.", 429
            )
        used = self.db.tracks_requested_since(user.id, int(time.time()) - 86400)
        if used + preview.track_count > s.daily_track_quota:
            left = max(0, s.daily_track_quota - used)
            raise JobRejected(
                f"Quota de {s.daily_track_quota} titres par 24 h atteint "
                f"(il t'en reste {left}).",
                429,
            )
        free_mb = shutil.disk_usage(s.work_dir).free // (1024 * 1024)
        if free_mb < s.min_free_disk_mb:
            raise JobRejected("Le serveur manque d'espace disque, réessaie plus tard.", 503)

        job_id = secrets.token_urlsafe(16)
        job = Job(
            id=job_id,
            user_id=user.id,
            ref=preview.ref,
            title=preview.name,
            cover=preview.cover,
            audio_format=audio_format,
            quality=quality,
            track_total=preview.track_count,
            dir=s.work_dir / job_id,
        )
        self.db.record_job(
            job_id=job_id,
            user_id=user.id,
            url=preview.ref.url,
            kind=preview.ref.kind,
            title=preview.name,
            audio_format=audio_format,
            track_total=preview.track_count,
            status="queued",
        )
        self.jobs[job_id] = job
        job.task = asyncio.create_task(self._run(job))
        log.info("job %s queued by user %s: %s (%d tracks)", job_id, user.username,
                 preview.ref.url, preview.track_count)
        return job

    async def cancel(self, job: Job) -> None:
        if job.status not in ACTIVE:
            return
        job.cancel_requested = True
        job.message = "Annulation…"
        job.notify()
        if job.proc and job.proc.returncode is None:
            # SIGINT to the whole group stops ffmpeg/deno children too and
            # lets Sunnify write its summary; SIGKILL follows if it lingers.
            # Done in the background so the HTTP request returns at once.
            task = asyncio.create_task(_terminate(job.proc, grace=10))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        elif job.task and job.status == "queued":
            job.task.cancel()

    # running ---------------------------------------------------------------

    async def _run(self, job: Job) -> None:
        assert self._slots is not None
        try:
            async with self._slots:
                if job.cancel_requested:
                    job.status = "cancelled"
                    return
                job.status = "running"
                self.db.mark_job_running(job.id)
                job.notify()
                await self._execute(job)
        except asyncio.CancelledError:
            job.status = "cancelled"
        except Exception:
            log.exception("job %s crashed", job.id)
            job.status = "failed"
            job.message = job.message or "Erreur interne du serveur."
        finally:
            job.proc = None
            job.finished_at = time.time()
            job.expires_at = job.finished_at + self.settings.result_ttl_s
            if job.landed == 0:
                _remove(job.dir)
            self.db.finish_job(job.id, status=job.status, landed=job.landed, failed=job.failed)
            log.info("job %s finished: %s (%d ok, %d failed)", job.id, job.status,
                     job.landed, job.failed)
            job.notify()

    async def _execute(self, job: Job) -> None:
        s = self.settings
        home = job.dir / "home"
        tmp = job.dir / "tmp"
        for path in (job.out_dir, home, tmp):
            path.mkdir(parents=True, exist_ok=True)
        cache = s.work_dir / ".cache"
        env = {k: os.environ[k] for k in _PASSTHROUGH_ENV if k in os.environ}
        env.setdefault("LANG", "C.UTF-8")
        env.update(
            HOME=str(home),
            # every scratch file of the run stays in the job folder, so
            # removing that folder is a complete cleanup
            TMPDIR=str(tmp),
            XDG_CONFIG_HOME=str(home / ".config"),
            XDG_STATE_HOME=str(home / ".state"),
            # yt-dlp's player cache is worth sharing between jobs
            XDG_CACHE_HOME=str(cache),
            DENO_DIR=str(cache / "deno"),
            QT_QPA_PLATFORM="offscreen",
            PYTHONUNBUFFERED="1",
        )
        cmd = download_command(
            s.sunnify_bin, job.ref, job.out_dir, audio_format=job.audio_format, quality=job.quality
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=job.dir,
            start_new_session=True,
            limit=1024 * 1024,
        )
        job.proc = proc
        stderr_tail: deque[str] = deque(maxlen=40)
        stderr_task = asyncio.create_task(_drain(proc.stderr, stderr_tail))
        summary: dict | None = None
        try:
            summary = await asyncio.wait_for(self._consume(job, proc), timeout=s.job_timeout_s)
        except asyncio.TimeoutError:
            job.message = "Délai maximum dépassé, téléchargement arrêté."
            await _terminate(proc, grace=10)
        except asyncio.CancelledError:
            await _terminate(proc, grace=2)
            raise
        returncode = await proc.wait()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(stderr_task, 5)
        if returncode not in (0, 1) and not job.cancel_requested:
            log.warning("job %s: sunnify exited %s; stderr tail:\n%s", job.id, returncode,
                        "\n".join(stderr_tail))

        if summary:
            job.failed = int(summary.get("failed", job.failed))
            titles = summary.get("failed_titles") or []
            job.failed_titles = [str(t)[:200] for t in titles if isinstance(t, str)]
        if job.cancel_requested:
            job.status = "cancelled"
            job.message = "Annulé." + (" Les titres déjà prêts restent disponibles."
                                       if job.landed else "")
        elif job.landed == 0:
            job.status = "failed"
            job.message = job.message or "Aucun titre n'a pu être téléchargé."
        elif job.failed or returncode != 0:
            job.status = "partial"
        else:
            job.status = "done"
            job.message = ""

    async def _consume(self, job: Job, proc: asyncio.subprocess.Process) -> dict | None:
        assert proc.stdout is not None
        summary = None
        while True:
            try:
                line = await proc.stdout.readline()
            except ValueError:  # a single line over the reader limit
                continue
            if not line:
                return summary
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("event")
            if kind == "track_done":
                job.landed += 1
                job.recent.append({"title": _text(event.get("title")),
                                   "artists": _text(event.get("artists")), "ok": True})
            elif kind == "warning":
                self._on_warning(job, _text(event.get("message"), 300))
            elif kind == "error":
                code = event.get("code")
                if code in _CLI_ERRORS:
                    job.message = _CLI_ERRORS[code]
            elif kind == "run_summary":
                summary = event
            else:
                continue
            job.notify()

    def _on_warning(self, job: Job, message: str) -> None:
        low = message.lower()
        if "not a bot" in low or "blocked this network" in low:
            job.message = YOUTUBE_BLOCKED
        elif "rate limit" in low:
            job.message = "Limite de débit atteinte, Sunnify patiente…"
        elif low.endswith("not found on youtube") or "download failed" in low or \
                "may be unavailable" in low:
            job.failed += 1
            title = re.search(r"'(.+)'", message)
            job.recent.append({"title": title.group(1) if title else message,
                               "artists": "", "ok": False})

    # downloads & cleanup ---------------------------------------------------

    def mark_downloaded(self, job: Job) -> None:
        soon = time.time() + self.settings.after_download_ttl_s
        if job.expires_at is None or soon < job.expires_at:
            job.expires_at = soon
            job.notify()

    async def _janitor_loop(self) -> None:
        last_session_purge = 0.0
        while True:
            await asyncio.sleep(15)
            try:
                self.sweep()
                if time.time() - last_session_purge > 600:
                    self.db.purge_sessions(idle_s=self.settings.session_idle_s,
                                           max_s=self.settings.session_max_s)
                    last_session_purge = time.time()
            except Exception:
                log.exception("janitor pass failed")

    def sweep(self) -> None:
        now = time.time()
        for job in list(self.jobs.values()):
            if job.status in ACTIVE or job.active_downloads:
                continue
            if job.status != "expired" and job.expires_at and job.expires_at <= now:
                _remove(job.dir)
                job.status = "expired"
                job.notify()
            # Forget finished jobs after a day; the history stays in SQLite.
            if job.finished_at and now - job.finished_at > 86400:
                self.jobs.pop(job.id, None)


def _text(value: object, limit: int = 200) -> str:
    return str(value)[:limit] if isinstance(value, str) else ""


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists() or path.is_symlink():
        with contextlib.suppress(OSError):
            path.unlink()


async def _drain(stream: asyncio.StreamReader | None, tail: deque[str]) -> None:
    if stream is None:
        return
    while True:
        try:
            line = await stream.readline()
        except ValueError:
            continue
        if not line:
            return
        tail.append(line.decode(errors="replace").rstrip())


async def _terminate(proc: asyncio.subprocess.Process, *, grace: float) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGINT)
    try:
        await asyncio.wait_for(proc.wait(), grace)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
