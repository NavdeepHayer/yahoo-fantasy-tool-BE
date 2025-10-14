import stat
from typing import Optional
from fastapi import Cookie, Header, Query, HTTPException ,status  

from app.core.auth import decode_session_token

def get_user_id(
    user_id: str | None = Query(None, description="Yahoo GUID"),
    x_user_id: str | None = Header(None, convert_underscores=False),  # exact header name
) -> str:
    uid = (user_id or x_user_id or "").strip()
    if not uid:
        raise HTTPException(
            status_code=400,
            detail="user_id is required (use ?user_id=<GUID> or header X-User-Id: <GUID>)."
        )
    return uid


def get_current_user(session_token: Optional[str] = Cookie(default=None)) -> str:
    """
    Retrieves and validates the session cookie. Returns the user's GUID or raises 401 if invalid/absent.
    """
    if not session_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    guid = decode_session_token(session_token)
    if not guid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session")
    return guid