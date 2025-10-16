# app/api/routes_league.py
from __future__ import annotations

from typing import List, Optional, Dict, Any
import inspect
from datetime import date as _date

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.deps import get_user_id, get_current_user
from app.schemas.team import Team, Roster
from app.services.yahoo import get_teams_for_user, get_roster_for_user
from app.services.yahoo import search_free_agents, get_scoreboard
from app.schemas.free_agent import FreeAgent
from app.services.yahoo.matchups import get_league_week_matchups_scores
from app.services.yahoo.standings import get_league_standings

# NEW: caching
from app.services.cache import cache_route, key_tuple

router = APIRouter(prefix="/league", tags=["league"])

# ---------------- TEAMS (cache 6h) ----------------
@router.get("/{league_id}/teams", response_model=List[Team])
@cache_route(
    namespace="league_teams",
    ttl_seconds=6 * 60 * 60,  # 6h
    key_builder=lambda *args, **kwargs: key_tuple(
        "teams", kwargs["guid"], kwargs["league_id"]
    ),
)
def league_teams(
    league_id: str,
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
    response: Response = None,
):
    """
    Returns all teams in a given Yahoo league.
    """
    try:
        teams = get_teams_for_user(db, guid, league_id)
    except HTTPException as he:
        raise he
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to fetch teams")
    return [Team(**t) for t in teams]


# ---------------- ROSTER (5m today, "forever" past) ----------------
@router.get("/team/{team_id}/roster", response_model=Roster)
async def team_roster(
    team_id: str,
    date: str | None = Query(
        default=None,
        description="Optional YYYY-MM-DD to fetch roster on a specific date",
    ),
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
    response: Response = None,
):
    """
    Returns the roster for a specific team. Optionally provide a date in YYYY-MM-DD format.
    """
    # Normalize date default on server if not provided
    date_str = date or _date.today().isoformat()
    ttl = 5 * 60 if date_str == _date.today().isoformat() else 365 * 24 * 60 * 60

    # one-off decorator with dynamic TTL
    decorator = cache_route(
        namespace="team_roster",
        ttl_seconds=ttl,
        key_builder=lambda *a, **k: key_tuple("roster", guid, team_id, date_str),
    )

    @decorator
    def _inner(
        team_id: str,
        date_str: str,
        db: Session,
        guid: str,
        response: Response = None,
    ):
        try:
            data = get_roster_for_user(db, guid, team_id, date_str)
        except HTTPException as he:
            raise he
        except ValueError as ve:
            raise HTTPException(status_code=400, detail=str(ve))
        except Exception:
            raise HTTPException(status_code=500, detail="Failed to fetch roster")

        # Ensure minimal shape safety for response_model
        if not isinstance(data, dict):
            raise HTTPException(status_code=502, detail="Invalid roster shape from service")

        data.setdefault("team_id", team_id)
        data.setdefault("date", date_str)
        data.setdefault("players", [])

        return Roster(**data)

    # Call the decorated function and await if needed (depending on cache wrapper)
    result = _inner(team_id=team_id, date_str=date_str, db=db, guid=guid, response=response)
    if inspect.isawaitable(result):
        result = await result
    return result


# ---------------- FREE AGENTS (no cache for now) ----------------
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


# ---------------- SCOREBOARD (fix path + no cache for now) ----------------
@router.get("/{league_id}/scoreboard")
def league_scoreboard(
    league_id: str,
    week: Optional[int] = Query(default=None, description="If omitted, current week"),
    enriched: bool = Query(default=True),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    return get_scoreboard(db, user_id, league_id, week=week, enriched=enriched)


# ---------------- MATCHUPS SCORES (cache 30m) ----------------
@router.get("/{league_id}/matchups/scores")
@cache_route(
    namespace="league_matchups_scores",
    ttl_seconds=30 * 60,  # 30m
    key_builder=lambda *args, **kwargs: key_tuple(
        "matchups_scores",
        kwargs["guid"],
        kwargs["league_id"],
        kwargs["week"],
        kwargs["include_points"],
        kwargs["include_categories"],
        kwargs["compact"],
        kwargs["debug"],
    ),
)
def league_matchups_scores(
    league_id: str,
    week: int | None = Query(default=None, description="Week number (integer)"),
    include_points: bool = Query(default=True),
    include_categories: bool = Query(default=True),
    compact: bool = Query(default=True, description="If false, include per-stat breakdown"),
    debug: bool = Query(default=False),
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
    response: Response = None,
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


# ---------------- STANDINGS (cache 12h) ----------------
@router.get("/{league_id}/standings")
@cache_route(
    namespace="league_standings",
    ttl_seconds=12 * 60 * 60,  # 12h
    key_builder=lambda *args, **kwargs: key_tuple(
        "standings", kwargs["guid"], kwargs["league_id"]
    ),
)
def league_standings_route(
    league_id: str,
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
    response: Response = None,
) -> Dict[str, Any]:
    """
    Normalized standings for a league.
    items[] contain: team_id, team_key, name, manager{guid,nickname,...}, rank, wins, losses, ties, percentage, logo_url?
    """
    return get_league_standings(db, guid, league_id)


# ---------------- RAW free agents passthrough (untouched) ----------------
# NOTE: This path becomes /league/league/{league_id}/free-agents because the router has prefix="/league".
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
    from app.services.yahoo import fetch_free_agents_raw  # local import to avoid unused if not used elsewhere
    out_arg = [s.strip() for s in out.split(",")] if out else None
    return fetch_free_agents_raw(
        db, user_id, league_id,
        status=status, start=start, count=count,
        position=position, sort=sort, sort_type=sort_type,
        out=out_arg,
    )
