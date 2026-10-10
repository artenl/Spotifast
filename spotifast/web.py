"""HTTP layer: security middleware, auth dependencies and helpers."""

from __future__ import annotations

import secrets
import time
from urllib.parse import quote, urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import Settings
from .db import Database, Session
from .security import hash_token

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

CSP = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data: https://*.scdn.co https://*.spotifycdn.com",
        "connect-src 'self'",
        "font-src 'self'",
        "manifest-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
    ]
)
SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    # Not "no-referrer": under it browsers send `Origin: null` on same-site
    # form posts and fetches, which the Origin check below would refuse.
    # "same-origin" still sends nothing to other sites.
    (b"referrer-policy", b"same-origin"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
    (b"x-robots-tag", b"noindex, nofollow"),
]


class SecurityMiddleware:
    """Pure ASGI (so streaming/SSE responses pass through untouched).

    - adds security headers to every response;
    - refuses unsafe requests whose Origin isn't ours (CSRF, first layer;
      the per-session token checked in the routes is the second).
    """

    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.origin = settings.public_origin.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope["method"] in UNSAFE_METHODS and not self._same_origin(scope):
            response = JSONResponse({"error": "Requête d'origine inconnue refusée."}, 403)
            await response(scope, receive, self._with_headers(send, scope))
            return
        await self.app(scope, receive, self._with_headers(send, scope))

    def _same_origin(self, scope: Scope) -> bool:
        headers = dict(scope["headers"])
        origin = headers.get(b"origin", b"").decode("latin-1")
        if origin:
            return origin == self.origin
        referer = headers.get(b"referer", b"").decode("latin-1")
        return bool(referer) and (referer == self.origin or referer.startswith(self.origin + "/"))

    @staticmethod
    def _with_headers(send: Send, scope: Scope) -> Send:
        path = scope.get("path", "")

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                headers.extend(h for h in SECURITY_HEADERS if h[0] not in present)
                if not path.startswith("/static/") and b"cache-control" not in present:
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        return wrapped


# auth -------------------------------------------------------------------------


class LoginRequired(Exception):
    pass


class PasswordChangeRequired(Exception):
    pass


class Forbidden(Exception):
    pass


class CSRFError(Exception):
    pass


def is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def load_session(request: Request, db: Database, settings: Settings) -> Session | None:
    token = request.cookies.get(settings.cookie_name)
    if not token or len(token) > 128:
        return None
    session = db.get_session(hash_token(token))
    if session is None:
        return None
    now = time.time()
    expired = (
        now - session.last_seen_at > settings.session_idle_s
        or now - session.created_at > settings.session_max_s
    )
    if expired or not session.user.is_active:
        db.delete_session(session.token_hash)
        return None
    if now - session.last_seen_at > 60:
        db.touch_session(session.token_hash)
    return session


def check_csrf(session: Session, supplied: str | None) -> None:
    if not supplied or not secrets.compare_digest(supplied, session.csrf_token):
        raise CSRFError()


def safe_next(target: str | None) -> str:
    """Only allow local, absolute-path redirects (no //host or scheme)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return "/"
    return target


def set_session_cookie(response, settings: Settings, token: str) -> None:
    response.set_cookie(
        settings.cookie_name,
        token,
        max_age=settings.session_max_s,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        # Lax (not Strict) so opening a shared link from another app still
        # finds you logged in; cross-site POSTs carry no cookie either way,
        # and are refused by the Origin check and CSRF token besides.
        samesite="lax",
    )


def clear_session_cookie(response, settings: Settings) -> None:
    response.delete_cookie(
        settings.cookie_name, path="/", secure=settings.cookie_secure, httponly=True,
        samesite="lax",
    )


def content_disposition(filename: str) -> str:
    fallback = "".join(c for c in filename if c.isascii() and c.isprintable() and c not in '"\\')
    fallback = fallback.strip() or "spotifast"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


def error_response(request: Request, status: int, message: str):
    if is_api(request):
        return JSONResponse({"error": message}, status)
    return None


def login_redirect(request: Request) -> RedirectResponse:
    target = request.url.path
    if request.url.query:
        target += "?" + request.url.query
    return RedirectResponse(f"/login?next={quote(safe_next(target), safe='/')}", 303)
