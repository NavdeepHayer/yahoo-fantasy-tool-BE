from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List

from app.db.session import get_db
from app.schemas.league import League
from app.services.yahoo import get_leagues, get_teams_for_user, yahoo_raw_get
from app.deps import get_user_id
from app.services.yahoo_matchups import get_my_weekly_matchups
from app.deps import get_current_user

router = APIRouter(prefix="/me", tags=["me"])

@router.get("/leagues", response_model=List[League])
def me_leagues(
    sport: str | None = Query(default=None, description="nba/mlb/nhl/nfl"),
    season: int | None = Query(default=None, description="e.g., 2025"),
    game_key: str | None = Query(default=None, description="Explicit Yahoo game_key, e.g. 466"),
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
):
    """
    Fetch and parse the user’s leagues, optionally filtered by sport, season, or explicit game_key.
    """
    try:
        leagues = get_leagues(db, guid, sport=sport, season=season, game_key=game_key)
    except HTTPException as he:
        raise he
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to fetch leagues")
    return [League(**l) for l in leagues]

@router.get("/my-team")
def my_team(
    league_id: str = Query(..., description="Yahoo league key, e.g. 466.l.17802"),
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
):
    """
    Returns the caller’s team details within the specified league.
    """
    try:
        teams = get_teams_for_user(db, guid, league_id)
        # Retrieve the caller’s GUID
        raw = yahoo_raw_get(db, guid, "/users;use_login=1", params={"format": "json"})
        guid = (
            raw.get("fantasy_content", {})
               .get("users", {})
               .get("0", {})
               .get("user", [{}])[0]
               .get("guid")
        )
        mine = next((t for t in teams if t.get("manager") == guid), None) if guid else None
    except HTTPException as he:
        raise he
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to fetch team info")
    return {"guid": guid, "team": mine, "teams": teams}

def coerce_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    return str(val).strip().lower() in {"1", "true", "t", "yes", "y", "on"}

@router.get("/matchups")
def my_matchups(
    week: int | None = Query(default=None, description="Week number (integer)"),
    league_id: str | None = Query(default=None, description="Yahoo league key, e.g. 466.l.34067"),
    include_categories: str | bool = Query(default="false", description="Include category stats"),
    include_points: str | bool = Query(default="true", description="Include points"),
    limit: int | None = Query(default=None, description="Limit number of matchups returned"),
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
):
    """
    Fetch weekly matchups. You can include category stats, points, and limit the number of returned entries.
    """
    try:
        return get_my_weekly_matchups(
            db,
            guid,
            week=week,
            league_id=league_id,
            include_categories=coerce_bool(include_categories),
            include_points=coerce_bool(include_points),
            limit=limit,
        )
    except HTTPException as he:
        raise he
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to fetch matchups")
    

@router.get("/whoami")
def whoami(guid: str = Depends(get_current_user)):
    return {"guid": guid}
