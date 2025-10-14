from fastapi import Header, Query, HTTPException

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