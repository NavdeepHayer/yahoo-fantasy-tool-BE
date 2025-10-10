from fastapi import Header, HTTPException

# Super simple "user" for dev: supply X-User-Id header in Postman
async def get_user_id(x_user_id: str | None = Header(default="local-dev")) -> str:
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-Id header")
    return x_user_id
