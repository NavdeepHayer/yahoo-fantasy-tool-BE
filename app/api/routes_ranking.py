# app/api/routes_ranking.py
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.deps import get_current_user
from app.services.yahoo.client import yahoo_get
from app.services.ranking.power_ranking import (
    build_week_power_table_and_scores,
    debug_probe_week,
)

router = APIRouter(prefix="/ranking", tags=["ranking"])


@router.get("/league/{league_id}/week")
def ranking_week(
    league_id: str,
    week: int = Query(..., description="Yahoo scoring week number"),
    normalize: str = Query("totals", pattern="^(totals|per_game)$"),
    punt: str = Query("", description="Comma-separated list of categories to punt"),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    data = build_week_power_table_and_scores(
        db=db,
        user_id=user_id,
        league_id=league_id,
        week=week,
        normalize=normalize,
        punt_csv=punt,
    )
    return data


# Debug: probe roster & teams discovery (remove later if you like)
@router.get("/debug/league/{league_id}/probe-week")
def ranking_debug_probe_week(
    league_id: str,
    week: int = Query(..., description="Yahoo scoring week number"),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    return debug_probe_week(db, user_id, league_id, week)


# Debug: raw Yahoo proxy (quick shape inspection) — optional
@router.get("/debug/raw")
def ranking_debug_raw(
    path: str = Query(..., description="Yahoo Fantasy path, e.g. /league/{id}/teams"),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    return yahoo_get(db, user_id, path)
