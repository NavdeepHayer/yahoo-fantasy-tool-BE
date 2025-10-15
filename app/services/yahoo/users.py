from __future__ import annotations
from typing import Optional
from sqlalchemy.orm import Session
import requests

from app.core.config import settings
from app.db.models import User
from app.services.yahoo.client import yahoo_get  # fallback path when user_id is available

def get_current_user_profile(
    db: Session,
    access_token: Optional[str] = None,
    user_id: Optional[str] = None,
) -> dict:
    """
    Fetch the logged-in Yahoo user's profile and upsert into our DB.
    Supports two modes:
      - During OAuth callback: pass access_token (no user_id yet)
      - Later (if needed): pass user_id to use stored tokens via yahoo_get
    Returns: {"guid": str, "nickname": str|None, "image_url": str|None}
    """
    # --- Fetch raw profile ---
    if access_token:
        # Direct call using the fresh OAuth access token (no DB token yet)
        url = f"{settings.YAHOO_API_BASE}/users;use_login=1"
        resp = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            params={"format": "json"},
            timeout=20,
        )
        resp.raise_for_status()
        raw = resp.json()
    elif user_id:
        # Use our client with persisted tokens (requires user_id)
        raw = yahoo_get(db, user_id, "/users;use_login=1")
    else:
        raise ValueError("get_current_user_profile requires access_token or user_id")

    # --- Defensive parse across Yahoo's odd shapes ---
    fc = raw.get("fantasy_content", {})
    users = fc.get("users", {})
    user0 = (users.get("0", {}) or {}).get("user", [{}])[0] if isinstance(users, dict) else {}
    guid = user0.get("guid")
    prof = user0.get("profile", {}) if isinstance(user0, dict) else {}
    nickname = prof.get("nickname")
    image_url = prof.get("image_url") or prof.get("image_url_small")

    if not guid:
        raise RuntimeError("Could not parse Yahoo user GUID from /users;use_login=1")

    # --- Upsert into our users table ---
    existing = db.get(User, guid)
    if existing:
        existing.nickname = nickname or existing.nickname
        existing.image_url = image_url or existing.image_url
        db.add(existing)
    else:
        db.add(User(guid=guid, nickname=nickname, image_url=image_url))
    db.commit()

    return {"guid": guid, "nickname": nickname, "image_url": image_url}
