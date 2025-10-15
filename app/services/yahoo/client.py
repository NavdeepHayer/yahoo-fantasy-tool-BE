from fastapi import HTTPException
import requests
from typing import Optional, Dict
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import decrypt_value
from app.db.models import OAuthToken
from app.services.yahoo.oauth import get_latest_token, refresh_token


def _auth_headers(access_token: str) -> Dict[str, str]:
    """Build the Authorization header for Yahoo API requests."""
    return {"Authorization": f"Bearer {access_token}"}


def yahoo_get(
    db: Session,
    user_id: str,
    path: str,                 # e.g. "/users;use_login=1/games;game_keys=466/leagues"
    params: Optional[dict] = None,
) -> dict:
    """
    Core Yahoo GET with auto-refresh on 401. Mirrors original behavior,
    but decrypts stored tokens before use.
    """
    uid = (user_id or "").strip()
    tok = get_latest_token(db, uid)
    if not tok:
        # Helpful debug to show how many token rows exist for this user
        count = db.query(OAuthToken).filter(OAuthToken.user_id == uid).count()
        raise HTTPException(
            status_code=400,
            detail=f"No Yahoo OAuth token on file for user_id={uid!r} (rows={count}). Call /auth/login and complete the flow first."
        )

    # Decrypt the stored access token
    access_token = decrypt_value(tok.access_token)
    url = f"{settings.YAHOO_API_BASE.rstrip('/')}{path}"
    q = dict(params or {})
    q.setdefault("format", "json")

    resp = requests.get(url, headers=_auth_headers(access_token), params=q, timeout=30)
    if resp.status_code == 401:
        # Token expired; refresh and retry
        new_tok = refresh_token(db, uid, tok)
        access_token = decrypt_value(new_tok.access_token)
        resp = requests.get(url, headers=_auth_headers(access_token), params=q, timeout=30)

    resp.raise_for_status()
    return resp.json()

def yahoo_raw_get(db: Session, user_id: str, path: str, params: dict | None = None):
    """
    Raw GET passthrough to Yahoo Fantasy API. Ensures format=json unless explicitly overridden.
    """
    q = dict(params or {})
    q.setdefault("format", "json")
    return yahoo_get(db, user_id, path, params=q)
