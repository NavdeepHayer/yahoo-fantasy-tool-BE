from sqlalchemy.orm import Session
from app.db.models import User
from app.core.config import settings
import requests

def upsert_user_from_yahoo(db: Session, access_token: str) -> dict:
    """
    Fetch the current Yahoo user with the fresh access_token and upsert.
    Adds strong diagnostics if Yahoo returns non-JSON (e.g., 401/HTML).
    """
    url = f"{settings.YAHOO_API_BASE}/users;use_login=1"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    # Prefer query param for format to avoid any path quirk
    r = requests.get(url, headers=headers, params={"format": "json"}, timeout=20)

    # Raise for obvious HTTP errors first
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        # Bubble up with helpful context (status + first 300 chars)
        snippet = r.text[:300] if r.text else ""
        raise RuntimeError(
            f"Yahoo profile HTTP error {r.status_code}: {snippet}"
        ) from e

    # Parse JSON safely; surface non-JSON bodies
    try:
        raw = r.json()
    except Exception as e:
        ct = r.headers.get("content-type", "")
        snippet = r.text[:300] if r.text else ""
        raise RuntimeError(
            f"Yahoo profile returned non-JSON (content-type={ct!r}). "
            f"Body starts with: {snippet}"
        ) from e

    # ---- Normal parse below ----
    fc = (raw or {}).get("fantasy_content", {})
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
        if nickname:  existing.nickname = nickname
        if image_url: existing.image_url = image_url
        db.add(existing)
    else:
        db.add(User(guid=guid, nickname=nickname, image_url=image_url))
    db.commit()

    return {"guid": guid, "nickname": nickname, "image_url": image_url}
