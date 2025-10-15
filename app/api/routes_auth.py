# app/api/routes_auth.py
import base64
import json
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session

from app.core.crypto import encrypt_value
from app.db.session import get_db
from app.db.models import OAuthToken
from app.core.config import settings
from app.services.yahoo import get_authorization_url
from requests_oauthlib import OAuth2Session
from oauthlib.oauth2.rfc6749.errors import InvalidGrantError
from app.services.yahoo_profile import upsert_user_from_yahoo
from app.core.auth import create_session_token

router = APIRouter(prefix="/auth", tags=["auth"])


def _encode_state_payload(return_to: str | None) -> str:
    """csrf.<base64url-json> where json={'r': return_to}."""
    csrf = secrets.token_urlsafe(24)
    payload = {"r": return_to} if return_to else {}
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{csrf}.{encoded}"


def _decode_return_to_from_state(state_value: str | None) -> str | None:
    if not state_value or "." not in state_value:
        return None
    try:
        encoded = state_value.split(".", 1)[1]
        # pad for base64
        encoded += "=" * ((4 - len(encoded) % 4) % 4)
        raw = base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8")
        data = json.loads(raw)
        r = data.get("r")
        if isinstance(r, str) and r.startswith(("http://", "https://")):
            return r
    except Exception:
        return None
    return None


@router.get("/login")
def auth_login(debug: bool = False, return_to: str | None = Query(default=None)):
    """
    Start Yahoo OAuth. Optional ?return_to=<frontend_url> tells callback where to go after login.
    """
    if not settings.YAHOO_CLIENT_ID or not settings.YAHOO_REDIRECT_URI:
        raise HTTPException(500, "Yahoo env vars missing")

    state = _encode_state_payload(return_to)
    url = get_authorization_url(state=state)

    if debug:
        return JSONResponse(
            {"redirect_uri": settings.YAHOO_REDIRECT_URI, "authorize_url": url, "state": state}
        )

    resp = RedirectResponse(url)
    resp.set_cookie(
        key="oauth_state",
        value=state,
        httponly=True,
        secure=settings.COOKIE_SECURE,  # True in HTTPS
        max_age=600,
        samesite="lax",
    )
    return resp


@router.get("/callback")
def auth_callback(
    request: Request,
    response: Response,
    code: str,
    state: str,
    db: Session = Depends(get_db),
):
    # Validate state via cookie (CSRF protection)
    cookie_state = request.cookies.get("oauth_state")
    if not cookie_state or cookie_state != state:
        raise HTTPException(400, "Invalid or missing OAuth state")

    # Build OAuth session and exchange code
    redirect_uri = settings.YAHOO_REDIRECT_URI.strip()
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
        # Code expired or reused — restart fresh login (keeps UX simple)
        return RedirectResponse(url="/auth/login")

    # Upsert user and persist token
    profile = upsert_user_from_yahoo(db, access_token=token["access_token"])
    guid = profile["guid"]

    rec = OAuthToken(
        user_id=guid,
        access_token=encrypt_value(token.get("access_token")),
        refresh_token=encrypt_value(token.get("refresh_token")),
        expires_in=token.get("expires_in"),
        token_type=token.get("token_type"),
        scope=token.get("scope"),
        raw=json.dumps(token),  # consider encrypting or omitting in prod
    )
    db.add(rec)
    db.commit()

    # Create session cookie
    session_token = create_session_token(guid)

    # Determine where to send the user next from the state payload
    return_to = _decode_return_to_from_state(cookie_state) or "/"

    resp = RedirectResponse(return_to)
    resp.set_cookie(
        key="session_token",
        value=session_token,
        httponly=True,
        secure=settings.APP_ENV != "local",  # True in non-local
        max_age=7 * 24 * 3600,
        samesite="lax",
    )
    # Clear one-time state cookie
    resp.delete_cookie("oauth_state")
    return resp


@router.post("/logout")
def auth_logout(response: Response):
    """
    Clears the session cookie. Returns 204 No Content.
    """
    resp = Response(status_code=204)
    resp.delete_cookie("session_token")
    return resp
