from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.deps import get_current_user
from app.services.yahoo import yahoo_raw_get

router = APIRouter(prefix="/debug", tags=["debug"])

@router.get("/yahoo/raw")
def yahoo_raw(
    request: Request,
    path: str = Query(..., description="Yahoo Fantasy path starting with '/', e.g. /league/466.l.17802/scoreboard"),
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
):
    """
    Proxies a raw Yahoo Fantasy API call. Automatically adds `format=json` unless provided.
    Example: /debug/yahoo/raw?path=/league/466.l.17802/scoreboard&week=2
    """
    try:
        # Pass through all query params except `path`
        qp = dict(request.query_params)
        qp.pop("path", None)
        return yahoo_raw_get(db, guid, path, params=qp)
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Raw Yahoo proxy failed: {e}")
