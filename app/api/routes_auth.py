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
def auth_login(debug: bool = False):
    if not settings.YAHOO_CLIENT_ID or not settings.YAHOO_REDIRECT_URI:
        raise HTTPException(500, "Yahoo env vars missing")

    state = secrets.token_urlsafe(24)
    url = get_authorization_url(state=state)

    # For debugging, return the URL and state instead of redirecting
    if debug:
        return JSONResponse({"redirect_uri": settings.YAHOO_REDIRECT_URI, "authorize_url": url, "state": state})

    # Create the redirect response and set the cookie on it
    response = RedirectResponse(url)
    response.set_cookie(
        key="oauth_state",
        value=state,
        httponly=True,
        secure=settings.COOKIE_SECURE,  # set to True if you are using HTTPS
        max_age=600,
        samesite="lax"
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
    session_token = create_session_token(guid)
    response = RedirectResponse("/")  # redirect back to your frontend root
    # secure=True only when running behind HTTPS (e.g. ngrok or production)
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