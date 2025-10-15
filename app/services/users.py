from __future__ import annotations
from sqlalchemy.orm import Session

from app.db.models import User
from app.services.yahoo_client import yahoo_get

def get_current_user_profile(db: Session, user_id: str) -> dict:
    """
    Fetch the logged-in Yahoo user's profile via Yahoo Fantasy API.
    Returns: {"guid": str, "nickname": str|None, "image_url": str|None}
    """
    raw = yahoo_get(db, user_id, "/users;use_login=1")

    fc = raw.get("fantasy_content", {})
    users = fc.get("users", {})
    user0 = (users.get("0", {}) or {}).get("user", [{}])[0] if isinstance(users, dict) else {}
    guid = user0.get("guid")
    prof = user0.get("profile", {}) if isinstance(user0, dict) else {}
    nickname = prof.get("nickname")
    image_url = prof.get("image_url") or prof.get("image_url_small")

    if not guid:
        raise RuntimeError("Could not parse Yahoo user GUID from /users;use_login=1")

    existing = db.get(User, guid)
    if existing:
        existing.nickname = nickname or existing.nickname
        existing.image_url = image_url or existing.image_url
        db.add(existing)
    else:
        db.add(User(guid=guid, nickname=nickname, image_url=image_url))
    db.commit()

    return {"guid": guid, "nickname": nickname, "image_url": image_url}
