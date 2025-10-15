# app/api/routes_auth.py
import json, secrets
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from app.core.crypto import encrypt_value
from app.db.session import get_db
from app.db.models import OAuthToken
from app.core.config import settings
from app.services.yahoo import get_authorization_url
from requests_oauthlib import OAuth2Session
from app.services.yahoo_profile import upsert_user_from_yahoo
from app.core.auth import create_session_token

router = APIRouter(prefix="/auth", tags=["auth"])

@router.get("/login")
def auth_login(debug: bool = False, return_to: str | None = Query(default=None)):
    """
    Start Yahoo OAuth. Optional ?return_to=<frontend_url> tells callback where to go after login.
    """
    if not settings.YAHOO_CLIENT_ID or not settings.YAHOO_REDIRECT_URI:
        raise HTTPException(500, "Yahoo env vars missing")

    import base64, json
    # store return_to inside state so we can decode later
    state_payload = {"r": return_to} if return_to else {}
    state_raw = json.dumps(state_payload).encode()
    state = secrets.token_urlsafe(8) + "." + base64.urlsafe_b64encode(state_raw).decode()

    url = get_authorization_url(state=state)

    if debug:
        return JSONResponse({"authorize_url": url, "state": state})

    response = RedirectResponse(url)
    response.set_cookie(
        key="oauth_state",
        value=state,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        max_age=600,
        samesite="lax",
    )
    return response


@router.get("/callback")
def auth_callback(
    request: Request,
    response: Response,
    code: str,
    state: str,
    db: Session = Depends(get_db)
):
    # Retrieve state from cookie
    cookie_state = request.cookies.get("oauth_state")
    if not cookie_state or cookie_state != state:
        raise HTTPException(400, "Invalid or missing OAuth state")

    # Optional: clear the cookie immediately
    response.delete_cookie(key="oauth_state")

    redirect = settings.YAHOO_REDIRECT_URI.strip()
    oauth = OAuth2Session(
        client_id=settings.YAHOO_CLIENT_ID,
        redirect_uri=redirect,
        scope=["fspt-r"],
    )
    token = oauth.fetch_token(
        token_url=settings.YAHOO_TOKEN_URL,
        code=code,
        include_client_id=True,
        client_secret=settings.YAHOO_CLIENT_SECRET,
        auth=(settings.YAHOO_CLIENT_ID, settings.YAHOO_CLIENT_SECRET),
    )

    profile = upsert_user_from_yahoo(db, access_token=token["access_token"])
    guid = profile["guid"]

    rec = OAuthToken(
        user_id=guid,
        access_token=encrypt_value(token.get("access_token")),
        refresh_token=encrypt_value(token.get("refresh_token")),
        expires_in=token.get("expires_in"),
        token_type=token.get("token_type"),
        scope=token.get("scope"),
        raw=json.dumps(token),  # you may want to omit or encrypt raw as well
    )
    db.add(rec)
    db.commit()
    
    # 🎫 Create a new session token for this user
      # 🎫 Create a new session token for this user
    session_token = create_session_token(guid)

    # Decode return_to from state (if present)
    import base64, json
    return_to = None
    try:
        if "." in state:
            encoded = state.split(".", 1)[1]
            padded = encoded + "=" * ((4 - len(encoded) % 4) % 4)
            raw = base64.urlsafe_b64decode(padded.encode()).decode()
            payload = json.loads(raw)
            return_to = payload.get("r")
    except Exception:
        pass

    # fallback if nothing valid
    if not return_to or not return_to.startswith(("http://", "https://")):
        return_to = "/"  # default: API root

    response = RedirectResponse(return_to)
    response.set_cookie(
        key="session_token",
        value=session_token,
        httponly=True,
        secure=settings.APP_ENV != "local",
        max_age=7 * 24 * 3600,
        samesite="lax",
    )
    return response

@router.post("/logout")
def auth_logout(response: Response):
    """
    Clears the session cookie. Returns 204 No Content.
    """
    response = Response(status_code=204)
    response.delete_cookie("session_token")
    return response