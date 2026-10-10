"""FastAPI application: pages, JSON API and admin."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.concurrency import iterate_in_threadpool, run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .config import Settings
from .db import Database, Session
from .jobs import ACTIVE, JobManager, JobRejected
from .security import (
    LoginThrottle,
    RateLimiter,
    generate_temp_password,
    hash_password,
    needs_rehash,
    new_csrf_token,
    new_session_token,
    password_problem,
    verify_password,
)
from .sunnify import (
    AUDIO_FORMATS,
    QUALITIES,
    InvalidSpotifyURL,
    MetadataError,
    PreviewCache,
    fetch_preview,
    parse_spotify_url,
)
from .web import (
    CSRFError,
    Forbidden,
    LoginRequired,
    PasswordChangeRequired,
    SecurityMiddleware,
    check_csrf,
    clear_session_cookie,
    content_disposition,
    is_api,
    load_session,
    login_redirect,
    safe_next,
    set_session_cookie,
)
from .zipstream import stream_file, stream_zip

log = logging.getLogger("spotifast")
HERE = Path(__file__).parent
USERNAME = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MEDIA_TYPES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".opus": "audio/ogg",
               ".ogg": "audio/ogg", ".flac": "audio/flac", ".wav": "audio/wav"}
STATUS_FR = {"queued": "en attente", "running": "en cours", "done": "terminé",
             "partial": "incomplet", "failed": "échec", "cancelled": "annulé",
             "expired": "effacé", "interrupted": "interrompu", "rejected": "refusé"}


def _format_time(ts: int | None) -> str:
    return time.strftime("%d/%m %H:%M", time.localtime(ts)) if ts else "—"


def _asset_version() -> str:
    """Content hash of the static files, used as a cache-busting query
    string so browsers pick up new CSS/JS right after an update."""
    digest = hashlib.sha256()
    for path in sorted((HERE / "static").rglob("*")):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def _setup_logging() -> None:
    # uvicorn only configures its own loggers; without this our INFO lines
    # (logins, jobs, admin actions) would never reach `docker compose logs`.
    logger = logging.getLogger("spotifast")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False


def create_app(settings: Settings | None = None) -> FastAPI:
    _setup_logging()
    settings = settings or Settings()
    db = Database(settings.db_path)
    manager = JobManager(settings, db)
    throttle = LoginThrottle(settings.login_max_failures, settings.login_window_s)
    preview_limiter = RateLimiter(limit=20, window_s=60)
    previews = PreviewCache()
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(version=__version__, asset_v=_asset_version(),
                                 formats=AUDIO_FORMATS, qualities=QUALITIES)
    templates.env.filters["dt"] = _format_time
    templates.env.filters["status_fr"] = lambda s: STATUS_FR.get(s, s)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await manager.start()
        yield
        await manager.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.db = db
    app.state.manager = manager

    host = settings.public_origin.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=[host, "localhost", "127.0.0.1", "testserver"]
    )
    app.add_middleware(SecurityMiddleware, settings=settings)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    # dependencies ---------------------------------------------------------

    def optional_session(request: Request) -> Session | None:
        return load_session(request, db, settings)

    def any_session(request: Request) -> Session:
        session = load_session(request, db, settings)
        if session is None:
            raise LoginRequired()
        return session

    def user_session(session: Session = Depends(any_session)) -> Session:
        if session.user.must_change_password:
            raise PasswordChangeRequired()
        return session

    def admin_session(session: Session = Depends(user_session)) -> Session:
        if not session.user.is_admin:
            raise Forbidden()
        return session

    async def api_csrf(request: Request, session: Session = Depends(user_session)) -> Session:
        check_csrf(session, request.headers.get("x-csrf-token"))
        return session

    # error handlers -------------------------------------------------------

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _exc):
        if is_api(request):
            return JSONResponse({"error": "Session expirée, reconnecte-toi."}, 401)
        return login_redirect(request)

    @app.exception_handler(PasswordChangeRequired)
    async def _password_change(request: Request, _exc):
        if is_api(request):
            return JSONResponse({"error": "Change d'abord ton mot de passe."}, 403)
        return RedirectResponse("/account", 303)

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, _exc):
        if is_api(request):
            return JSONResponse({"error": "Accès refusé."}, 403)
        return HTMLResponse("Accès refusé.", 403)

    @app.exception_handler(CSRFError)
    async def _csrf(request: Request, _exc):
        if is_api(request):
            return JSONResponse({"error": "Jeton de sécurité invalide, recharge la page."}, 403)
        return HTMLResponse("Jeton de sécurité invalide : recharge la page et réessaie.", 403)

    def page(request: Request, name: str, session: Session | None, status: int = 200, **ctx):
        return templates.TemplateResponse(
            request,
            name,
            {"user": session.user if session else None,
             "csrf": session.csrf_token if session else "", **ctx},
            status_code=status,
        )

    # pages ------------------------------------------------------------------

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return RedirectResponse("/static/favicon.svg", 301)

    @app.get("/login")
    async def login_form(request: Request, session: Session | None = Depends(optional_session)):
        if session:
            return RedirectResponse("/", 303)
        return page(request, "login.html", None, next=safe_next(request.query_params.get("next")))

    @app.post("/login")
    async def login(
        request: Request,
        username: str = Form("", max_length=64),
        password: str = Form("", max_length=256),
        next: str = Form("/", max_length=512),
    ):
        ip = request.client.host if request.client else "unknown"
        username = username.strip()
        wait = throttle.retry_after(ip, username)
        if wait:
            minutes = max(1, round(wait / 60))
            return page(request, "login.html", None, 429, next=safe_next(next), username=username,
                        error=f"Trop de tentatives. Réessaie dans {minutes} min.")
        user = await run_in_threadpool(db.get_user_by_name, username) if username else None
        ok = await run_in_threadpool(verify_password, user.password_hash if user else None,
                                     password)
        if not ok or user is None or not user.is_active:
            throttle.record_failure(ip, username)
            log.warning("failed login for %r from %s", username, ip)
            return page(request, "login.html", None, 401, next=safe_next(next), username=username,
                        error="Identifiant ou mot de passe incorrect.")
        throttle.reset(ip, username)
        if needs_rehash(user.password_hash):
            db.set_password(user.id, hash_password(password), must_change=user.must_change_password)
        # Always a fresh session id at login (no fixation), and drop the
        # one the browser may still be holding.
        old = load_session(request, db, settings)
        if old:
            db.delete_session(old.token_hash)
        token, token_hash = new_session_token()
        db.create_session(token_hash, user.id, new_csrf_token())
        db.touch_login(user.id)
        log.info("login %s from %s", user.username, ip)
        target = "/account" if user.must_change_password else safe_next(next)
        response = RedirectResponse(target, 303)
        set_session_cookie(response, settings, token)
        return response

    @app.post("/logout")
    async def logout(session: Session = Depends(any_session), csrf: str = Form("")):
        check_csrf(session, csrf)
        db.delete_session(session.token_hash)
        response = RedirectResponse("/login", 303)
        clear_session_cookie(response, settings)
        return response

    @app.get("/")
    async def index(request: Request, session: Session = Depends(user_session)):
        return page(request, "index.html", session, limits=_limits(session))

    @app.get("/account")
    async def account(request: Request, session: Session = Depends(any_session)):
        success = None
        if request.query_params.get("changed") == "1":
            success = "Mot de passe changé. Tes autres appareils ont été déconnectés."
        return page(request, "account.html", session, success=success)

    @app.post("/account/password")
    async def change_password(
        request: Request,
        session: Session = Depends(any_session),
        csrf: str = Form(""),
        current: str = Form("", max_length=256),
        new: str = Form("", max_length=256),
        confirm: str = Form("", max_length=256),
    ):
        check_csrf(session, csrf)
        user = session.user
        if not await run_in_threadpool(verify_password, user.password_hash, current):
            return page(request, "account.html", session, 400,
                        error="Le mot de passe actuel est incorrect.")
        problem = password_problem(new, user.username)
        if problem is None and new != confirm:
            problem = "Les deux nouveaux mots de passe ne correspondent pas."
        if problem is None and new == current:
            problem = "Le nouveau mot de passe doit être différent de l'actuel."
        if problem:
            return page(request, "account.html", session, 400, error=problem)
        db.set_password(user.id, await run_in_threadpool(hash_password, new), must_change=False)
        db.delete_user_sessions(user.id, keep=session.token_hash)
        log.info("password changed for %s", user.username)
        return RedirectResponse("/account?changed=1", 303)

    # API --------------------------------------------------------------------

    def _limits(session: Session) -> dict:
        used = db.tracks_requested_since(session.user.id, int(time.time()) - 86400)
        return {
            "maxTracks": settings.max_tracks_per_job,
            "dailyQuota": settings.daily_track_quota,
            "usedToday": used,
            "resultTtlMin": settings.result_ttl_s // 60,
        }

    async def _preview_for(raw_url: object, session: Session):
        ref = parse_spotify_url(raw_url)
        cached = previews.get(ref)
        if cached:
            return cached
        if not preview_limiter.allow(f"user:{session.user.id}"):
            raise JobRejected("Trop de recherches d'un coup, patiente une minute.", 429)
        try:
            preview = await asyncio.wait_for(
                run_in_threadpool(fetch_preview, ref, limit=settings.max_tracks_per_job), 60
            )
        except asyncio.TimeoutError as exc:
            raise MetadataError("timeout") from exc
        previews.put(preview)
        return preview

    async def _json_body(request: Request) -> dict:
        if len(await request.body()) > 8192:
            raise JobRejected("Requête trop grande.", 413)
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise JobRejected("Requête invalide.")
        return body

    @app.exception_handler(JobRejected)
    async def _rejected(_request: Request, exc: JobRejected):
        return JSONResponse({"error": exc.message}, exc.status_code)

    @app.exception_handler(InvalidSpotifyURL)
    async def _bad_url(_request: Request, exc: InvalidSpotifyURL):
        return JSONResponse({"error": str(exc)}, 400)

    @app.exception_handler(MetadataError)
    async def _metadata(_request: Request, exc: MetadataError):
        log.warning("metadata fetch failed: %s", exc)
        return JSONResponse(
            {"error": "Impossible de lire ce lien chez Spotify. La playlist est-elle publique ?"},
            502,
        )

    @app.post("/api/preview")
    async def api_preview(request: Request, session: Session = Depends(api_csrf)):
        body = await _json_body(request)
        preview = await _preview_for(body.get("url"), session)
        return {"preview": preview.to_json(), "limits": _limits(session)}

    @app.get("/api/jobs")
    async def api_jobs(session: Session = Depends(user_session)):
        return {
            "jobs": [j.snapshot() for j in manager.for_user(session.user)],
            "limits": _limits(session),
        }

    @app.post("/api/jobs", status_code=201)
    async def api_create_job(request: Request, session: Session = Depends(api_csrf)):
        body = await _json_body(request)
        audio_format = body.get("format", "mp3")
        quality = str(body.get("quality", "320"))
        if audio_format not in AUDIO_FORMATS or quality not in QUALITIES:
            raise JobRejected("Format ou qualité non pris en charge.")
        preview = await _preview_for(body.get("url"), session)
        job = manager.submit(session.user, preview, audio_format=audio_format, quality=quality)
        return {"job": job.snapshot(), "limits": _limits(session)}

    def _job_or_404(job_id: str, session: Session):
        job = manager.get(job_id, session.user)
        if job is None:
            raise JobRejected("Téléchargement introuvable.", 404)
        return job

    @app.get("/api/jobs/{job_id}")
    async def api_job(job_id: str, session: Session = Depends(user_session)):
        return {"job": _job_or_404(job_id, session).snapshot()}

    @app.post("/api/jobs/{job_id}/cancel")
    async def api_cancel(job_id: str, session: Session = Depends(api_csrf)):
        job = _job_or_404(job_id, session)
        await manager.cancel(job)
        return {"job": job.snapshot()}

    @app.get("/api/jobs/{job_id}/events")
    async def api_events(request: Request, job_id: str, session: Session = Depends(user_session)):
        job = _job_or_404(job_id, session)

        async def stream():
            seen = -1
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    return
                if job.version != seen:
                    seen = job.version
                    yield f"data: {json.dumps(job.snapshot(), ensure_ascii=False)}\n\n"
                    if job.status not in ACTIVE:
                        return
                else:
                    yield ": ping\n\n"
                await job.wait_change(seen, 15)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/jobs/{job_id}/download")
    async def api_download(job_id: str, session: Session = Depends(user_session)):
        job = _job_or_404(job_id, session)
        files = await run_in_threadpool(job.result_files)
        if not files:
            raise JobRejected("Les fichiers ne sont plus disponibles sur le serveur.", 410)
        if job.ref.kind == "track" and len(files) == 1:
            path, arcname = files[0]
            filename = Path(arcname).name
            media_type = MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
            iterator = stream_file(path)
            size = path.stat().st_size
        else:
            filename = job.download_name() + ".zip"
            media_type = "application/zip"
            iterator = stream_zip(files)
            size = None

        async def body():
            job.active_downloads += 1
            completed = False
            try:
                async for chunk in iterate_in_threadpool(iterator):
                    yield chunk
                completed = True
            finally:
                job.active_downloads -= 1
                if completed:
                    manager.mark_downloaded(job)
                    log.info("job %s downloaded by %s", job.id, session.user.username)

        headers = {"Content-Disposition": content_disposition(filename)}
        if size is not None:
            headers["Content-Length"] = str(size)
        return StreamingResponse(body(), media_type=media_type, headers=headers)

    # admin ------------------------------------------------------------------

    def admin_page(request: Request, session: Session, status: int = 200, **ctx):
        disk = shutil.disk_usage(settings.work_dir)
        return page(
            request, "admin.html", session, status,
            users=db.list_users(),
            jobs=db.recent_jobs(60),
            stats={
                "running": manager.running_count(),
                "queued": manager.queued_count(),
                "slots": settings.max_concurrent_jobs,
                "free_gb": round(disk.free / 1024**3, 1),
            },
            **ctx,
        )

    def _target_user(user_id: int):
        user = db.get_user(user_id)
        if user is None:
            raise Forbidden()
        return user

    @app.get("/admin")
    async def admin(request: Request, session: Session = Depends(admin_session)):
        return admin_page(request, session)

    @app.post("/admin/users")
    async def admin_create(
        request: Request,
        session: Session = Depends(admin_session),
        csrf: str = Form(""),
        username: str = Form("", max_length=64),
        is_admin: str = Form(""),
    ):
        check_csrf(session, csrf)
        username = username.strip()
        if not USERNAME.match(username):
            return admin_page(request, session, 400, error="Nom d'utilisateur invalide : 3 à 32 "
                              "caractères parmi lettres, chiffres, point, tiret et _.")
        password = generate_temp_password()
        try:
            db.create_user(username, await run_in_threadpool(hash_password, password),
                           is_admin=bool(is_admin))
        except sqlite3.IntegrityError:
            return admin_page(request, session, 400, error=f"Le compte « {username} » existe déjà.")
        log.info("admin %s created user %s", session.user.username, username)
        return admin_page(request, session, created={"username": username, "password": password})

    @app.post("/admin/users/{user_id}/reset")
    async def admin_reset(
        request: Request, user_id: int, session: Session = Depends(admin_session),
        csrf: str = Form(""),
    ):
        check_csrf(session, csrf)
        user = _target_user(user_id)
        password = generate_temp_password()
        db.set_password(user.id, await run_in_threadpool(hash_password, password), must_change=True)
        db.delete_user_sessions(user.id)
        log.info("admin %s reset password of %s", session.user.username, user.username)
        if user.id == session.user.id:
            response = RedirectResponse("/login", 303)
            clear_session_cookie(response, settings)
            return response
        return admin_page(request, session, created={"username": user.username,
                                                     "password": password, "reset": True})

    @app.post("/admin/users/{user_id}/active")
    async def admin_active(
        user_id: int, session: Session = Depends(admin_session), csrf: str = Form(""),
        active: str = Form("0"),
    ):
        check_csrf(session, csrf)
        user = _target_user(user_id)
        if user.id == session.user.id:
            raise Forbidden()
        enable = active == "1"
        if not enable and user.is_admin and db.count_admins() <= 1:
            raise Forbidden()
        db.set_active(user.id, enable)
        if not enable:
            for job in manager.for_user(user):
                await manager.cancel(job)
        log.info("admin %s set %s active=%s", session.user.username, user.username, enable)
        return RedirectResponse("/admin", 303)

    @app.post("/admin/users/{user_id}/delete")
    async def admin_delete(
        user_id: int, session: Session = Depends(admin_session), csrf: str = Form(""),
    ):
        check_csrf(session, csrf)
        user = _target_user(user_id)
        if user.id == session.user.id:
            raise Forbidden()
        for job in manager.for_user(user):
            await manager.cancel(job)
        db.delete_user(user.id)
        log.info("admin %s deleted user %s", session.user.username, user.username)
        return RedirectResponse("/admin", 303)

    return app
