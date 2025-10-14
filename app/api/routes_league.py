from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List
from app.db.session import get_db
from app.deps import get_user_id
from app.schemas.team import Team, Roster
from app.services.yahoo import get_teams_for_user, get_roster_for_user
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
