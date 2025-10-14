from fastapi import HTTPException
import requests
from typing import Optional, Dict
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import OAuthToken
from app.services.yahoo_oauth import get_latest_token, refresh_token


def _auth_headers(access_token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def yahoo_get(
    db: Session,
    user_id: str,
    path: str,                 # e.g. "/users;use_login=1/games;game_keys=466/leagues"
    params: Optional[dict] = None,
) -> dict:
    """
    Core Yahoo GET with auto-refresh on 401. Mirrors original behavior.
    """
    uid = (user_id or "").strip()
    tok = get_latest_token(db, uid)
    if not tok:
        # TEMP debug to confirm what the server sees
        count = db.query(OAuthToken).filter(OAuthToken.user_id == uid).count()
        raise HTTPException(
            status_code=400,
            detail=f"No Yahoo OAuth token on file for user_id={uid!r} (rows={count}). Call /auth/login and complete the flow first."
        )
    tok = get_latest_token(db, user_id)
    if not tok:
        raise RuntimeError("No Yahoo OAuth token on file. Call /auth/login and complete the flow first.")

    url = f"{settings.YAHOO_API_BASE.rstrip('/')}{path}"
    q = dict(params or {})
    q.setdefault("format", "json")

    resp = requests.get(url, headers=_auth_headers(tok.access_token), params=q, timeout=30)
    if resp.status_code == 401:
        tok = refresh_token(db, user_id, tok)
        resp = requests.get(url, headers=_auth_headers(tok.access_token), params=q, timeout=30)

    resp.raise_for_status()
    return resp.json()
