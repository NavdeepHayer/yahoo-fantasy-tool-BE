# app/api/routes_auth.py
import json, secrets
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.db.models import OAuthToken
from app.core.config import settings
from app.services.yahoo import get_authorization_url
from requests_oauthlib import OAuth2Session
from app.services.yahoo_profile import upsert_user_from_yahoo

router = APIRouter(prefix="/auth", tags=["auth"])

@router.get("/login")
def auth_login(debug: bool = Query(False)):
    if not settings.YAHOO_CLIENT_ID or not settings.YAHOO_REDIRECT_URI:
        raise HTTPException(500, "Yahoo env vars missing")

    state = secrets.token_urlsafe(24)
    url = get_authorization_url(state=state)

    if debug:
        return JSONResponse({"redirect_uri": settings.YAHOO_REDIRECT_URI, "authorize_url": url})
    return RedirectResponse(url)

@router.get("/callback")
def auth_callback(code: str, state: str, db: Session = Depends(get_db)):

    redirect = settings.YAHOO_REDIRECT_URI.strip()
    # 1) Exchange code for tokens (no helper; avoids signature clashes)
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

    # 2) Upsert user using fresh access token (get GUID + nickname)
    profile = upsert_user_from_yahoo(db, access_token=token["access_token"])
    guid = profile["guid"]

    # 3) Persist token with real GUID
    rec = OAuthToken(
        user_id=guid,
        access_token=token.get("access_token", ""),
        refresh_token=token.get("refresh_token"),
        expires_in=token.get("expires_in"),
        token_type=token.get("token_type"),
        scope=token.get("scope"),
        raw=json.dumps(token),
    )
    db.add(rec)
    db.commit()

    # 4) Redirect post-login (adjust if you have a dashboard)
    return RedirectResponse("/")
