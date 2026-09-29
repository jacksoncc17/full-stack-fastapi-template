"""
Google OAuth login, alongside the template's email/password login.

Flow
----
1. `GET /auth/google/login` returns an authorization URL and a `state` value.
   The frontend stores `state` and redirects the browser to the URL.
2. Google sends the browser back to `GET /auth/google/callback?code=...&state=...`.
3. The callback exchanges the code, fetches the Google profile, finds or creates
   the local user, and redirects to the frontend with a JWT in the query string.

Why redirect a token rather than set a cookie: the existing frontend keeps its
token in `localStorage` and sends it as a Bearer header (see hooks/useAuth.ts).
Issuing it the same way means the whole authenticated app works unchanged, with
no second session mechanism to keep in sync. The tradeoff is that the token
appears in the URL at that one hop, which is why the frontend callback route
immediately stores it and replaces the history entry.

Account linking rules, stated explicitly because they are a security decision:
* The link key is `(provider, provider_account_id)` -- Google's `sub`, which is
  stable and is not the email (emails change and are not unique across providers).
* If no link exists and a local user already owns that email, the accounts are
  linked ONLY when Google reports the email as verified. Otherwise an attacker
  could register a Google account at an unverified address and take over an
  existing local account.
* A Google user gets no local password, so `hashed_password` stays NULL and the
  password routes refuse rather than crash (see crud.authenticate).
"""

import secrets
import urllib.parse
from datetime import timedelta
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlmodel import select

from app.api.deps import SessionDep
from app.core import security
from app.core.config import settings
from app.models import GoogleAuthURL, OAuthAccount, Token, User

router = APIRouter(prefix="/auth/google", tags=["login"])

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"

# openid+email+profile are the minimum to identify a user; nothing sensitive.
GOOGLE_SCOPES = "openid email profile"

# in-memory state store: adequate for a single-process deployment and honest
# about it. Multi-process or multi-replica needs a shared store (Redis), because
# the state issued by one worker must be verifiable by another.
_ISSUED_STATES: set[str] = set()


def _require_configured() -> None:
    if not settings.google_oauth_enabled:
        raise HTTPException(
            status_code=503,
            detail="Google login is not configured "
                   "(GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are unset)",
        )


@router.get("/login", response_model=GoogleAuthURL)
def google_login() -> Any:
    """Return the Google consent URL to redirect the browser to."""
    _require_configured()
    state = secrets.token_urlsafe(32)
    _ISSUED_STATES.add(state)
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": GOOGLE_SCOPES,
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return GoogleAuthURL(
        authorization_url=f"{GOOGLE_AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}",
        state=state,
    )


def _exchange_code(code: str) -> dict:
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=f"Google token exchange failed: {resp.text[:200]}",
        )
    return resp.json()


def _fetch_userinfo(access_token: str) -> dict:
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(
            GOOGLE_USERINFO_ENDPOINT,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=f"Google userinfo failed: {resp.text[:200]}",
        )
    return resp.json()


@router.get("/callback")
def google_callback(
    session: SessionDep,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> Any:
    """Handle Google's redirect, then bounce the browser to the frontend."""
    _require_configured()

    def fail(reason: str) -> RedirectResponse:
        target = (f"{settings.FRONTEND_OAUTH_REDIRECT}"
                  f"?error={urllib.parse.quote(reason)}")
        return RedirectResponse(target, status_code=307)

    if error:
        return fail(error)
    if not code:
        return fail("no_code")
    # single-use: consuming the state here is what prevents a replay
    if not state or state not in _ISSUED_STATES:
        return fail("invalid_state")
    _ISSUED_STATES.discard(state)

    token_payload = _exchange_code(code)
    access_token = token_payload.get("access_token")
    if not access_token:
        return fail("no_access_token")
    info = _fetch_userinfo(access_token)

    sub = str(info.get("sub") or "")
    email = info.get("email")
    email_verified = bool(info.get("email_verified"))
    if not sub:
        return fail("no_subject")

    account = session.exec(
        select(OAuthAccount).where(
            OAuthAccount.provider == "google",
            OAuthAccount.provider_account_id == sub,
        )
    ).first()

    if account is not None:
        user = session.get(User, account.user_id)
        if user is None or not user.is_active:
            return fail("inactive_user")
    else:
        user = None
        if email:
            user = session.exec(select(User).where(User.email == email)).first()
            if user is not None and not email_verified:
                # refuse to link onto an existing account unless Google vouches
                # for the address; otherwise this is account takeover
                return fail("email_not_verified")
        if user is None:
            user = User(
                email=email or f"{sub}@google.invalid",
                hashed_password=None,          # no local password
                full_name=info.get("name"),
                is_active=True,
            )
            session.add(user)
            session.commit()
            session.refresh(user)
        session.add(
            OAuthAccount(
                provider="google", provider_account_id=sub, email=email,
                user_id=user.id,
            )
        )
        session.commit()

    if not user.is_active:
        return fail("inactive_user")

    jwt = security.create_access_token(
        user.id,
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    target = (f"{settings.FRONTEND_OAUTH_REDIRECT}"
              f"?access_token={urllib.parse.quote(jwt)}")
    return RedirectResponse(target, status_code=307)


@router.get("/status", response_model=dict)
def google_status() -> Any:
    """Lets the frontend decide whether to show the Google button."""
    return {
        "enabled": settings.google_oauth_enabled,
        "login_url": f"{settings.API_V1_STR}/auth/google/login"
        if settings.google_oauth_enabled else None,
    }
