from fastapi import HTTPException
import requests
from typing import Optional, Dict
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import decrypt_value
from app.db.models import OAuthToken
from app.services.yahoo.oauth import get_latest_token, refresh_token
from typing import Dict, Any, Optional
from urllib.parse import parse_qsl

from app.core.config import settings
from urllib.parse import parse_qsl
from typing import Dict, Any, Optional

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
    base = settings.YAHOO_API_BASE.rstrip("/")      # https://fantasysports.yahooapis.com/fantasy/v2
    rel  = path.lstrip("/")                          # e.g., league/466.l.17802/standings
    url  = f"{base}/{rel}"                           # -> https://.../v2/league/466.l.17802/standings
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

def yahoo_get(
    db: Session,
    user_id: str,
    path: str,                 # e.g., "/league/466.l.17802/standings"
    params: Optional[dict] = None,
) -> dict:
    """
    Core Yahoo GET with auto-refresh on 401. Decrypts stored tokens before use.
    FIXED: safe URL join (no more ...v2league... issues).
    """
    uid = (user_id or "").strip()
    tok = get_latest_token(db, uid)
    if not tok:
        count = db.query(OAuthToken).filter(OAuthToken.user_id == uid).count()
        raise HTTPException(
            status_code=400,
            detail=f"No Yahoo OAuth token on file for user_id={uid!r} (rows={count}). Call /auth/login first."
        )

    access_token = decrypt_value(tok.access_token)

    base = settings.YAHOO_API_BASE.rstrip("/")     # https://fantasysports.yahooapis.com/fantasy/v2
    rel  = path.lstrip("/")                        # league/466.l.17802/standings
    url  = f"{base}/{rel}"                         # -> https://.../v2/league/466.l.17802/standings

    q = dict(params or {})
    q.setdefault("format", "json")

    resp = requests.get(url, headers=_auth_headers(access_token), params=q, timeout=30)
    if resp.status_code == 401:
        new_tok = refresh_token(db, uid, tok)
        access_token = decrypt_value(new_tok.access_token)
        resp = requests.get(url, headers=_auth_headers(access_token), params=q, timeout=30)

    resp.raise_for_status()
    return resp.json()


def yahoo_raw_get(
    db,
    user_id: str,
    path: str,                                   # may include query, e.g. "/league/.../scoreboard?week=2"
    params: Optional[Dict[str, Any]] = None,
) -> dict:
    """
    Raw Yahoo GET with safe URL join and query merging.
    - Accepts `path` that MAY include its own query string.
    - Merges caller `params` (from /debug/yahoo/raw), preserving embedded query keys.
    - Ensures `format=json` is present.
    Delegates the actual call to `yahoo_get` (handles tokens + refresh).
    """
    # peel embedded query from path and merge with forwarded params
    merged: Dict[str, Any] = {}
    rel = path
    if "?" in rel:
        rel, embedded_qs = rel.split("?", 1)
        merged.update(dict(parse_qsl(embedded_qs, keep_blank_values=True)))
    if params:
        merged.update(params)
    merged.setdefault("format", "json")

    # ensure leading slash for yahoo_get()
    rel = "/" + rel.lstrip("/")
    return yahoo_get(db, user_id, rel, params=merged)