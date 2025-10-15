# app/api/routes_auth.py
import base64
import json
import secrets
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from requests_oauthlib import OAuth2Session
from oauthlib.oauth2.rfc6749.errors import InvalidGrantError

from app.core.config import settings
from app.core.crypto import encrypt_value
from app.core.auth import create_session_token
from app.db.session import get_db
from app.db.models import OAuthToken
from app.services.yahoo_profile import upsert_user_from_yahoo

router = APIRouter(prefix="/auth", tags=["auth"])

# -------------------------------------------------------------------
# Canonical frontend host (all post-login redirects will land here)
# -------------------------------------------------------------------
CANONICAL_FE_HOST = "mynbaassistant.com"

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def _b64url(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

def _b64url_decode(s: str) -> dict | None:
    try:
        s += "=" * ((4 - len(s) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(s.encode("ascii")).decode("utf-8"))
    except Exception:
        return None

def _default_frontend_url() -> str:
    # Prefer explicit envs; fall back to localhost for dev
    for key in ("FRONTEND_URL_REMOTE", "FRONTEND_URL_LOCAL"):
        val = getattr(settings, key, None)
        if isinstance(val, str) and val:
            return val.rstrip("/")
    return "http://localhost:5173"

def _normalize_return_to(request: Request, rt: str | None) -> str:
    """
    In production: always land on https://mynbaassistant.com/leagues
    In dev/local/ngrok: honor the provided return_to (or default FE URL).
    """
    api_host = (request.url.hostname or "").lower()
    is_prod = api_host == "api.mynbaassistant.com"

    if is_prod:
        return f"https://{CANONICAL_FE_HOST}/leagues"

    # dev/local: use FE-provided rt or the configured FE URL
    base = (rt or f"{_default_frontend_url()}/leagues").strip()
    # safety: allow only http(s)
    if base.startswith("http://") or base.startswith("https://"):
        return base
    # if someone passed a path only, join to the default FE
    return f"{_default_frontend_url().rstrip('/')}/{base.lstrip('/')}"

def _cookie_params_for(request: Request, return_to: str | None):
    """
    Set cookie params so the browser will send session_token on XHR.

    - If cross-site (FE origin != API host): SameSite=None; Secure is required.
    - In prod (api.mynbaassistant.com <-> mynbaassistant.com): also set cookie domain.
    """
    api_host = (request.url.hostname or "").lower()
    fe_host = urlparse(return_to).hostname.lower() if return_to else ""

    is_cross_site = bool(fe_host and fe_host != api_host)
    is_prod = api_host.endswith(".mynbaassistant.com")
    cookie_domain = ".mynbaassistant.com" if is_prod else None

    # Secure must be True for SameSite=None
    must_secure = (request.url.scheme == "https") or is_prod

    return {
        "domain": cookie_domain,
        "secure": True if (is_cross_site or must_secure) else False,
        "httponly": True,
        "samesite": "none" if is_cross_site or is_prod else "lax",
        "path": "/",
        "max_age": 7 * 24 * 3600,
    }

# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------
@router.get("/login")
def auth_login(
    request: Request,
    debug: bool = False,
    return_to: str | None = Query(default=None),
):
    # 1) Pick redirect_uri we will actually use now
    redirect_uri = (settings.YAHOO_REDIRECT_URI or "").strip()
    if not redirect_uri:
        raise HTTPException(500, "YAHOO_REDIRECT_URI missing")

    # 2) Normalize the destination we want to land on after OAuth
    #    (env-aware: prod → canonical; dev/local → honor FE)
    return_to = _normalize_return_to(request, return_to)

    # 3) Build state: csrf + encoded payload (return_to + redirect_uri)
    csrf = secrets.token_urlsafe(24)
    payload = {"r": return_to, "u": redirect_uri}
    state = f"{csrf}.{_b64url(payload)}"

    # 4) Build Yahoo authorize URL using the SAME redirect_uri
    oauth = OAuth2Session(
        client_id=settings.YAHOO_CLIENT_ID,
        redirect_uri=redirect_uri,
        scope=["fspt-r"],
    )
    authorize_url, _ = oauth.authorization_url(
        settings.YAHOO_AUTH_URL,
        state=state,
    )

    if debug:
        return JSONResponse({
            "using_redirect_uri": redirect_uri,
            "authorize_url": authorize_url,
            "state": state,
            "env": getattr(settings, "APP_ENV", "unknown"),
            "return_to": return_to,
        })

    # 5) CSRF state cookie (httponly) – short-lived
    resp = RedirectResponse(authorize_url, status_code=302)
    resp.set_cookie(
        key="oauth_state",
        value=state,
        httponly=True,
        secure=(request.url.scheme == "https"),
        max_age=600,
        samesite="lax",
        path="/",
    )
    return resp


@router.get("/callback")
def auth_callback(
    request: Request,
    code: str,
    state: str,
    db: Session = Depends(get_db),
):
    # 1) CSRF check
    cookie_state = request.cookies.get("oauth_state")
    if not cookie_state or cookie_state != state:
        raise HTTPException(400, "Invalid or missing OAuth state")

    # 2) Decode payload we sent at /auth/login
    parts = state.split(".", 1)
    payload = _b64url_decode(parts[1]) if len(parts) == 2 else None
    if not isinstance(payload, dict):
        raise HTTPException(400, "Malformed OAuth state")

    # env-aware normalization here too
    return_to = _normalize_return_to(request, payload.get("r"))
    redirect_uri = payload.get("u")
    if not isinstance(redirect_uri, str) or not redirect_uri:
        raise HTTPException(400, "Missing redirect_uri in state")

    # 3) Exchange code using the SAME redirect_uri
    oauth = OAuth2Session(
        client_id=settings.YAHOO_CLIENT_ID,
        redirect_uri=redirect_uri,
        scope=["fspt-r"],
    )
    try:
        token = oauth.fetch_token(
            token_url=settings.YAHOO_TOKEN_URL,
            code=code,
            include_client_id=True,
            client_secret=settings.YAHOO_CLIENT_SECRET,
            auth=(settings.YAHOO_CLIENT_ID, settings.YAHOO_CLIENT_SECRET),
        )
    except InvalidGrantError:
        # Likely stale login – send the user to start over
        return RedirectResponse(url="/auth/login", status_code=302)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {e}")

    # 4) Persist token + user
    profile = upsert_user_from_yahoo(db, access_token=token["access_token"])
    guid = profile["guid"]

    rec = OAuthToken(
        user_id=guid,
        access_token=encrypt_value(token.get("access_token")),
        refresh_token=encrypt_value(token.get("refresh_token")),
        expires_in=token.get("expires_in"),
        token_type=token.get("token_type"),
        scope=token.get("scope"),
        raw=json.dumps(token),
    )
    db.add(rec)
    db.commit()

    # 5) Session cookie + redirect to FE
    session_token = create_session_token(guid)
    cookie_params = _cookie_params_for(request, return_to)
    resp = RedirectResponse(return_to, status_code=302)
    resp.set_cookie("session_token", session_token, **cookie_params)
    # clear CSRF state cookie
    resp.delete_cookie("oauth_state", path="/")
    return resp
