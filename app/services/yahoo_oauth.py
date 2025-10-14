import json
import requests
from typing import Optional
from requests_oauthlib import OAuth2Session
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import decrypt_value, encrypt_value
from app.db.models import OAuthToken

import os, json
from urllib.parse import urlencode, quote_plus
import requests
from requests_oauthlib import OAuth2Session
from app.core.config import settings

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

AUTH_URL = getattr(settings, "YAHOO_AUTH_URL", "https://api.login.yahoo.com/oauth2/request_auth")
TOKEN_URL = getattr(settings, "YAHOO_TOKEN_URL", "https://api.login.yahoo.com/oauth2/get_token")
API_BASE  = getattr(settings, "YAHOO_API_BASE", "https://fantasysports.yahooapis.com/fantasy/v2")

def get_authorization_url(state: str, redirect_uri: str | None = None, scope: str = "fspt-r") -> str:
    redirect = (redirect_uri or settings.YAHOO_REDIRECT_URI).strip()
    params = {
        "client_id": settings.YAHOO_CLIENT_ID,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": scope,
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params, quote_via=quote_plus)}"


def exchange_token(code: str) -> dict:
    """
    Exchange the Yahoo auth code for an access/refresh token.
    NOTE: No DB writes here. Persist after you fetch the user's GUID.
    """
    oauth = build_oauth()
    token = oauth.fetch_token(
        token_url=settings.YAHOO_TOKEN_URL,
        code=code,
        client_secret=settings.YAHOO_CLIENT_SECRET,
    )
    # token: {"access_token","refresh_token","expires_in","token_type","scope",...}
    return token


def yahoo_api_get(path: str, access_token: str) -> dict:
    # path like "users;use_login=1"
    url = f"{API_BASE}/{path};format=json"
    r = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
    r.raise_for_status()
    return r.json()


def _persist_token(db: Session, user_id: str, token: dict) -> OAuthToken:
    rec = OAuthToken(
        user_id=user_id,
        access_token=encrypt_value(token.get("access_token", "")),
        refresh_token=encrypt_value(token.get("refresh_token")) if token.get("refresh_token") else None,
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
    plain_refresh = decrypt_value(tok.refresh_token)  # <-- decrypt before sending

    data = {
        "grant_type": "refresh_token",
        "refresh_token": plain_refresh,
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
    return _persist_token(db, user_id, new_token)  # will encrypt new tokens
