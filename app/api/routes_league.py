from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List
from app.db.session import get_db
from app.deps import get_user_id
from app.schemas.team import Team, Roster
from app.services.yahoo import get_teams_for_user, get_roster_for_user
from app.deps import get_current_user

from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.deps import get_user_id
from app.services.yahoo import search_free_agents, get_scoreboard
from app.schemas.free_agent import FreeAgent
from app.services.yahoo.matchups import get_league_week_matchups_scores

from typing import Dict, Any
from app.services.yahoo.standings import get_league_standings
from app.deps import get_current_user

router = APIRouter(prefix="/league", tags=["league"])

@router.get("/{league_id}/teams", response_model=List[Team])
def league_teams(
    league_id: str,
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
):
    """
    Returns all teams in a given Yahoo league.
    """
    try:
        teams = get_teams_for_user(db, guid, league_id)
    except HTTPException as he:
        # Re‑raise any HTTPExceptions from downstream calls
        raise he
    except ValueError as ve:
        # Bad input
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception:
        # Unexpected error – do not leak details to client
        raise HTTPException(status_code=500, detail="Failed to fetch teams")
    return [Team(**t) for t in teams]

@router.get("/team/{team_id}/roster", response_model=Roster)
def team_roster(
    team_id: str,
    date: str | None = Query(
        default=None, description="Optional YYYY-MM-DD to fetch roster on a specific date"
    ),
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
):
    """
    Returns the roster for a specific team. Optionally provide a date in YYYY-MM-DD format.
    """
    try:
        data = get_roster_for_user(db, guid, team_id, date)
    except HTTPException as he:
        raise he
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to fetch roster")
    return Roster(**data)

@router.get("/{league_id}/free-agents", response_model=List[FreeAgent])
def league_free_agents(
    league_id: str,
    position: Optional[str] = Query(default=None, description="e.g., G,F,C,PG,SG,SF,PF"),
    query: Optional[str] = Query(default=None, description="name search"),
    count: int = Query(default=25, ge=1, le=50),
    start: int = Query(default=0, ge=0),
    status: str = Query(default="FA", description="FA (free agent), W (waivers), T (all)"),
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
):
    return search_free_agents(
        db, guid, league_id,
        position=position, query=query, count=count, start=start, status=status
    )


@router.get("{league_id}/scoreboard")
def league_scoreboard(
    league_id: str,
    week: Optional[int] = Query(default=None, description="If omitted, current week"),
    enriched: bool = Query(default=True),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
) -> Dict[str, Any]:
    return get_scoreboard(db, user_id, league_id, week=week, enriched=enriched)


@router.get("/{league_id}/matchups/scores")
def league_matchups_scores(
    league_id: str,
    week: int | None = Query(default=None, description="Week number (integer)"),
    include_points: bool = Query(default=True),
    include_categories: bool = Query(default=True),
    compact: bool = Query(default=True, description="If false, include per-stat breakdown"),
    debug: bool = Query(default=False),
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
):
    try:
        return get_league_week_matchups_scores(
            db,
            guid,
            league_id=league_id,
            week=week,
            include_points=include_points,
            include_categories=include_categories,
            compact=compact,
            debug=debug,
        )
    except HTTPException as he:
        raise he
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to fetch league matchups scores")


@router.get("/{league_id}/standings")
def league_standings(
    league_id: str,
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Normalized standings for a league.
    items[] contain: team_id, team_key, name, manager{guid,nickname,...}, rank, wins, losses, ties, percentage, logo_url?
    """
    return get_league_standings(db, guid, league_id)

@router.get("/league/{league_id}/free-agents", tags=["league"])
def league_free_agents_raw(
    league_id: str,
    status: str = Query("FA"),
    start: int = Query(0),
    count: int = Query(25, le=25, ge=1),
    position: Optional[str] = Query(None),
    sort: Optional[str] = Query(None),
    sort_type: Optional[str] = Query(None),
    out: Optional[str] = Query(None, description="Comma list, e.g. stats,ownership,percent_owned"),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """
    **Raw** free agents for a league (no parsing yet).
    Mirrors the debug route but under the main API.
    """
    out_arg = [s.strip() for s in out.split(",")] if out else None
    return fetch_free_agents_raw(
        db, user_id, league_id,
        status=status, start=start, count=count,
        position=position, sort=sort, sort_type=sort_type,
        out=out_arg,
    )