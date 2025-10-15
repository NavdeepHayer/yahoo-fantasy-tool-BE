# app/api/routes_auth.py
import base64
import json
import secrets
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session

from app.core.crypto import encrypt_value
from app.db.session import get_db
from app.db.models import OAuthToken
from app.core.config import settings
from requests_oauthlib import OAuth2Session
from oauthlib.oauth2.rfc6749.errors import InvalidGrantError
from app.services.yahoo_profile import upsert_user_from_yahoo
from app.core.auth import create_session_token

router = APIRouter(prefix="/auth", tags=["auth"])


# ---------- helpers ----------

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
    for key in ("FRONTEND_URL_REMOTE", "FRONTEND_URL_LOCAL"):
        val = getattr(settings, key, None)
        if isinstance(val, str) and val:
            return val.rstrip("/")
    return "http://localhost:5173"

def _cookie_policy_for_session(request: Request, return_to: str | None):
    is_https = (request.url.scheme == "https")
    api_host = request.url.netloc
    fe_host = urlparse(return_to).netloc if return_to else ""
    cross_site = bool(fe_host and fe_host != api_host)
    if is_https and cross_site:
        return {"secure": True, "samesite": "none"}  # string "none"
    return {"secure": is_https, "samesite": "lax"}


# ---------- routes ----------

@router.get("/login")
def auth_login(
    request: Request,
    debug: bool = False,
    return_to: str | None = Query(default=None),
):
    # pick the redirect_uri we will actually use NOW
    redirect_uri = settings.YAHOO_REDIRECT_URI.strip()
    if not redirect_uri:
        raise HTTPException(500, "YAHOO_REDIRECT_URI missing")

    if not return_to:
        return_to = f"{_default_frontend_url()}/leagues"

    # state = csrf + encoded payload with BOTH return_to and redirect_uri
    csrf = secrets.token_urlsafe(24)
    payload = {"r": return_to, "u": redirect_uri}
    state = f"{csrf}.{_b64url(payload)}"

    # Build auth URL HERE using the same redirect_uri
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
        })

    # CSRF cookie
    resp = RedirectResponse(authorize_url)
    resp.set_cookie(
        key="oauth_state",
        value=state,
        httponly=True,
        secure=(request.url.scheme == "https"),
        max_age=600,
        samesite="lax",
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

    return_to = payload.get("r") or "https://mynbaassistant.com/leagues"
    redirect_uri = payload.get("u")
    if not isinstance(redirect_uri, str):
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

    # 5) Set cross-site session cookie and redirect back to FE
    session_token = create_session_token(guid)
    cookie_params = _cookie_params_for(request, return_to)
    resp = RedirectResponse(return_to, status_code=302)
    resp.set_cookie("session_token", session_token, **cookie_params)
    # clear CSRF state cookie
    resp.delete_cookie("oauth_state", path="/")
    return resp

def _b64url_decode(s: str):
    try:
        pad = '=' * (-len(s) % 4)
        return json.loads(base64.urlsafe_b64decode(s + pad).decode("utf-8"))
    except Exception:
        return None

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
