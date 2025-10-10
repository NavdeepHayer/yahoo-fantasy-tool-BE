import json
import requests
from typing import Optional
from requests_oauthlib import OAuth2Session
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import OAuthToken

# ---- OAuth / Yahoo config ----
# Use read-only scope while developing (write usually requires extra approval)
AUTH_SCOPE = ["fspt-r"]


# ---- OAuth helpers ----
def build_oauth(token: dict | None = None) -> OAuth2Session:
    return OAuth2Session(
        client_id=settings.YAHOO_CLIENT_ID,
        redirect_uri=settings.YAHOO_REDIRECT_URI,
        scope=AUTH_SCOPE,
        token=token,
        auto_refresh_url=settings.YAHOO_TOKEN_URL,
        auto_refresh_kwargs={
            "client_id": settings.YAHOO_CLIENT_ID,
            "client_secret": settings.YAHOO_CLIENT_SECRET,
        },
        token_updater=lambda t: None,  # we persist manually
    )


def get_authorization_url(state: str) -> str:
    oauth = build_oauth()
    auth_url, _ = oauth.authorization_url(settings.YAHOO_AUTH_URL, state=state)
    return auth_url


def exchange_token(db: Session, user_id: str, code: str) -> OAuthToken:
    oauth = build_oauth()
    token = oauth.fetch_token(
        token_url=settings.YAHOO_TOKEN_URL,
        code=code,
        client_secret=settings.YAHOO_CLIENT_SECRET,
    )
    return _persist_token(db, user_id, token)


def _persist_token(db: Session, user_id: str, token: dict) -> OAuthToken:
    rec = OAuthToken(
        user_id=user_id,
        access_token=token.get("access_token", ""),
        refresh_token=token.get("refresh_token"),
        expires_in=token.get("expires_in"),
        token_type=token.get("token_type"),
        scope=token.get("scope"),
        raw=json.dumps(token),
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


def get_latest_token(db: Session, user_id: str) -> Optional[OAuthToken]:
    return (
        db.query(OAuthToken)
        .filter(OAuthToken.user_id == user_id)
        .order_by(OAuthToken.id.desc())
        .first()
    )


def refresh_token(db: Session, user_id: str, tok: OAuthToken) -> OAuthToken:
    if not tok.refresh_token:
        raise RuntimeError("Yahoo token expired and no refresh_token is available.")
    data = {
        "grant_type": "refresh_token",
        "refresh_token": tok.refresh_token,
        "redirect_uri": settings.YAHOO_REDIRECT_URI,
    }
    r = requests.post(
        settings.YAHOO_TOKEN_URL,
        data=data,
        auth=(settings.YAHOO_CLIENT_ID, settings.YAHOO_CLIENT_SECRET),
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Yahoo refresh failed: {r.status_code} {r.text}")
    new_token = r.json()
    return _persist_token(db, user_id, new_token)
