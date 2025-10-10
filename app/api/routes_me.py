from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List

from app.db.session import get_db
from app.schemas.league import League
from app.services.yahoo import get_leagues , get_teams_for_user, yahoo_raw_get
from app.deps import get_user_id
from app.services.yahoo_matchups import get_my_weekly_matchups 

router = APIRouter(prefix="/me", tags=["me"])

@router.get("/leagues", response_model=List[League])
def me_leagues(
    sport: str | None = Query(default=None, description="nba/mlb/nhl/nfl"),
    season: int | None = Query(default=None, description="e.g., 2025"),
    game_key: str | None = Query(default=None, description="Explicit Yahoo game_key, e.g. 466"),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    try:
        leagues = get_leagues(db, user_id, sport=sport, season=season, game_key=game_key)
        return [League(**l) for l in leagues]
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/my-team")
def my_team(
    league_id: str = Query(..., description="Yahoo league key, e.g. 466.l.17802"),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    try:
        teams = get_teams_for_user(db, user_id, league_id)
        # Get your Yahoo GUID from a trivial call (first user guid)
        raw = yahoo_raw_get(db, user_id, "/users;use_login=1", params={"format": "json"})
        guid = raw.get("fantasy_content", {}).get("users", {}).get("0", {}) \
                  .get("user", [{}])[0].get("guid")

        mine = None
        if guid:
            # teams manager nickname/guid was parsed in get_teams_for_user
            for t in teams:
                if t.get("manager") == guid:
                    mine = t
                    break
        return {"guid": guid, "team": mine, "teams": teams}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

def coerce_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    return str(val).strip().lower() in {"1","true","t","yes","y","on"}
  
@router.get("/matchups")
def my_matchups(
    week: int | None = Query(default=None),
    league_id: str | None = Query(default=None),
    include_categories: str | bool = Query(default="false"),
    include_points: str | bool = Query(default="true"),
    limit: int | None = Query(default=None),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    try:
        return get_my_weekly_matchups(
            db,
            user_id,
            week=week,
            league_id=league_id,
            include_categories=coerce_bool(include_categories),
            include_points=coerce_bool(include_points),
            limit=limit,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))