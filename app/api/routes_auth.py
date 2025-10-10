from fastapi import APIRouter, Depends, Response, Request, HTTPException
from app.core.security import gen_state
from app.services.yahoo import get_authorization_url, exchange_token
from app.db.session import get_db
from sqlalchemy.orm import Session
from app.deps import get_user_id
from urllib.parse import quote_plus
from app.core.security import gen_state
from app.core.config import settings
from app.deps import get_user_id

router = APIRouter(prefix="/auth", tags=["auth"])

@router.get("/login")
def login(user_id: str = Depends(get_user_id)):
    state = gen_state()
    # Build the Yahoo authorize URL explicitly to guarantee scope & redirect
    params = (
        f"response_type=code"
        f"&client_id={quote_plus(settings.YAHOO_CLIENT_ID)}"
        f"&redirect_uri={quote_plus(settings.YAHOO_REDIRECT_URI)}"
        f"&scope={quote_plus('fspt-r')}"  # <-- force read-only scope
        f"&state={quote_plus(state)}"
    )
    url = f"{settings.YAHOO_AUTH_URL}?{params}"
    return {"state": state, "authorization_url": url, "user_id": user_id}

@router.get("/callback")
def callback(request: Request, code: str | None = None, state: str | None = None,
             db: Session = Depends(get_db), user_id: str = Depends(get_user_id)):
    if not code:
        raise HTTPException(status_code=400, detail="Missing ?code from Yahoo")
    token = exchange_token(db, user_id, code)
    return {"ok": True, "user_id": user_id, "token_id": token.id}
