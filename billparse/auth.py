"""Sign-in for the web app: Google accounts of one domain, admitted once with an invite code.

    AUTH=google                 turn it on (default: off, everything open, as for local development)
    GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET   an OAuth client of type "Web application"
    SESSION_SECRET              signs the session cookie (any long random text; keep it stable)
    ALLOWED_EMAIL_DOMAIN        default archera.ai: the Google account must be verified and of this domain
    INVITE_CODES                label=code pairs, comma separated (e.g. "team=7fK2...,demo=Qx91..."); a person gives
                                one once, at the first sign-in
    PUBLIC_URL                  optional: the address people open (e.g. https://billparse.example.com); otherwise it is
                                worked out from the request (X-Forwarded-* headers of nginx, Vite or a tunnel)

The API checks every request (middleware), so the web app, the /api proxy and port 8000 are all covered. The
redirect URI to register at Google is <the address>/api/auth/callback.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
import time
from functools import lru_cache
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from .db import AppUserRow

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

OPEN_PATHS = ("/health", "/auth/")              # reachable without a session (health checks, the sign-in itself)
MAX_INVITE_TRIES = 8                            # wrong codes per session before it must sign in again


def mode() -> str:
    return "google" if os.environ.get("AUTH", "off").lower() == "google" else "off"


def domain() -> str:
    return os.environ.get("ALLOWED_EMAIL_DOMAIN", "archera.ai").lower().lstrip("@")


def invite_codes() -> dict[str, str]:
    """label -> code. A bare code (no `label=`) gets a numbered label."""
    out = {}
    for i, part in enumerate(x.strip() for x in os.environ.get("INVITE_CODES", "").split(",")):
        if not part:
            continue
        label, _, code = part.partition("=") if "=" in part else (f"code{i + 1}", "", part)
        out[label.strip()] = code.strip()
    return out


def session_secret() -> str:
    secret = os.environ.get("SESSION_SECRET")
    if secret:
        return secret
    if mode() == "google":
        log.warning("SESSION_SECRET is not set: sessions end when the API restarts")
    return secrets.token_urlsafe(32)


def https_only() -> bool:
    return os.environ.get("PUBLIC_URL", "").startswith("https://") or os.environ.get("SESSION_HTTPS_ONLY") == "1"


@lru_cache(maxsize=1)
def _oauth(client_id: str, client_secret: str):
    from authlib.integrations.starlette_client import OAuth
    oauth = OAuth()
    oauth.register(name="google", client_id=client_id, client_secret=client_secret,
                   server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
                   client_kwargs={"scope": "openid email profile"})
    return oauth


def google():
    cid, secret = os.environ.get("GOOGLE_CLIENT_ID"), os.environ.get("GOOGLE_CLIENT_SECRET")
    if not cid or not secret:
        raise HTTPException(500, "AUTH=google needs GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.")
    return _oauth(cid, secret).google


def public_base(request: Request) -> str:
    """The address the browser uses, up to the API's prefix (e.g. https://billparse.example.com/api)."""
    prefix = request.headers.get("x-forwarded-prefix", "")
    if base := os.environ.get("PUBLIC_URL"):
        return base.rstrip("/") + (prefix or "/api")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc)).split(",")[0].strip()
    return f"{proto}://{host}{prefix}"


def _safe_next(next_path: str | None) -> str:
    """Only a path of this site: never an address elsewhere (an open redirect)."""
    if not next_path or not next_path.startswith("/") or next_path.startswith("//") or urlsplit(next_path).netloc:
        return "/"
    return next_path


def _store():
    from .webapi import get_store
    return get_store()


def _store_dependency(request: Request):
    """The API's store, honouring a test's dependency override."""
    from .webapi import get_store
    return request.app.dependency_overrides.get(get_store, get_store)()


def admitted(request: Request) -> str | None:
    """The signed-in, admitted person's email, or None."""
    user = request.session.get("user")
    return user["email"] if isinstance(user, dict) and user.get("admitted") else None


def current_user(request: Request) -> str | None:
    """Who makes this request: the admitted person's email with AUTH=google, else None (no sign-in)."""
    return admitted(request) if mode() == "google" else None


async def require_login(request: Request, call_next):
    """Middleware: with AUTH=google, every API path but the open ones needs an admitted person."""
    if mode() != "google" or request.url.path.startswith(OPEN_PATHS) or request.method == "OPTIONS":
        return await call_next(request)
    if admitted(request) is None:
        return JSONResponse({"detail": "Sign in first."}, status_code=401)
    return await call_next(request)


# ------------------------------------------------------------------ endpoints
class Me(BaseModel):
    auth: Literal["off", "google"]
    state: Literal["ok", "signed_out", "invite_required"]
    email: str | None = None
    name: str | None = None
    picture: str | None = None
    domain: str | None = None


class InviteIn(BaseModel):
    code: str


@router.get("/me", response_model=Me)
def me(request: Request) -> Me:
    if mode() == "off":
        return Me(auth="off", state="ok")
    user = request.session.get("user")
    if not isinstance(user, dict):
        return Me(auth="google", state="signed_out", domain=domain())
    return Me(auth="google", state="ok" if user.get("admitted") else "invite_required", email=user.get("email"),
              name=user.get("name"), picture=user.get("picture"), domain=domain())


@router.get("/login")
async def login(request: Request, next: str | None = None):  # noqa: A002 - the query parameter's name
    if mode() == "off":
        return RedirectResponse(_safe_next(next))
    request.session["next"] = _safe_next(next)
    # hd= limits Google's account chooser to the domain; the callback still checks it (a person can edit the URL)
    return await google().authorize_redirect(request, f"{public_base(request)}/auth/callback", hd=domain(),
                                             prompt="select_account")


@router.get("/callback")
async def callback(request: Request):
    from authlib.integrations.base_client.errors import OAuthError
    if mode() == "off":
        return RedirectResponse("/")
    try:
        token = await google().authorize_access_token(request)
    except OAuthError as e:
        log.warning("Google sign-in failed: %s", e)
        return RedirectResponse("/?auth_error=google")
    info: dict[str, Any] = token.get("userinfo") or {}
    email = str(info.get("email") or "").lower()
    if not (info.get("email_verified") and email.endswith("@" + domain()) and info.get("hd") == domain()):
        log.warning("sign-in refused for %s (hd=%s)", email or "?", info.get("hd"))
        request.session.clear()
        return RedirectResponse("/?auth_error=domain")
    store = _store_dependency(request)
    with store.session() as s:
        row = s.get(AppUserRow, email)
        if row is not None:
            row.last_login, row.name = time.time(), info.get("name") or row.name
            s.add(row)
            s.commit()
    request.session["user"] = {"email": email, "name": info.get("name"), "picture": info.get("picture"),
                               "admitted": row is not None, "tries": 0}
    log.info("signed in: %s%s", email, "" if row is not None else " (needs an invite code)")
    return RedirectResponse(request.session.pop("next", "/"))


@router.post("/invite", response_model=Me)
def invite(body: InviteIn, request: Request) -> Me:
    """Admit the signed-in person with an invite code (once: later sign-ins need no code)."""
    user = request.session.get("user")
    if mode() == "off" or not isinstance(user, dict):
        raise HTTPException(401, "Sign in with Google first.")
    if user.get("admitted"):
        return me(request)
    if user.get("tries", 0) >= MAX_INVITE_TRIES:
        request.session.clear()
        raise HTTPException(429, "Too many wrong codes. Sign in again to try again.")
    given = body.code.strip()
    label = next((lab for lab, code in invite_codes().items() if code and hmac.compare_digest(code, given)), None)
    if label is None:
        user["tries"] = user.get("tries", 0) + 1
        request.session["user"] = user
        raise HTTPException(403, "This invite code is not valid.")
    store = _store_dependency(request)
    now = time.time()
    with store.session() as s:
        s.merge(AppUserRow(email=user["email"], name=user.get("name"), invite=label, admitted_at=now, last_login=now))
        s.commit()
    user["admitted"] = True
    request.session["user"] = user
    log.info("admitted %s with invite '%s'", user["email"], label)
    return me(request)


@router.post("/logout", response_model=Me)
def logout(request: Request) -> Me:
    request.session.clear()
    return me(request)
