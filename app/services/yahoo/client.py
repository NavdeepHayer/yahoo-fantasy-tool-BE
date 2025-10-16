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

def yahoo_raw_get(
    db,
    user_id: str,
    path: str,                                   # may include its own query, e.g. "/league/.../players;status=FA;count=25?foo=bar"
    params: Optional[Dict[str, Any]] = None,
) -> dict:
    """
    Raw Yahoo GET with safe URL join and query merging.
    - Accepts `path` that MAY include its own query string.
    - Merges caller `params` (from /debug/yahoo/raw), preserving embedded keys.
    - Ensures `format=json` is present.
    - Delegates the HTTP call to `yahoo_get` so auth/refresh behavior is identical.
    """
    # Keep the relative path clean (no leading slash); we'll add one right before calling yahoo_get
    rel = path.lstrip("/")

    # If the caller embedded a query string in `path`, peel and merge it
    merged: Dict[str, Any] = {}
    if "?" in rel:
        rel, embedded_qs = rel.split("?", 1)
        merged.update(dict(parse_qsl(embedded_qs, keep_blank_values=True)))

    # Merge forwarded query params from the FastAPI route (excluding `path`)
    if params:
        merged.update(params)

    # Always request JSON unless explicitly provided
    merged.setdefault("format", "json")

    # Debug (optional): uncomment while testing
    # print("RAW GET ->", "/" + rel, merged)

    # ✅ Use the proven flow (auth headers + auto-refresh) via yahoo_get
    return yahoo_get(db=db, user_id=user_id, path="/" + rel, params=merged)