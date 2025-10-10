from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List

from app.db.session import get_db
from app.deps import get_user_id
from app.schemas.team import Team, Roster
from app.services.yahoo import get_teams_for_user, get_roster_for_user

router = APIRouter(prefix="/league", tags=["league"])

@router.get("/{league_id}/teams", response_model=List[Team])
def league_teams(
    league_id: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    try:
        teams = get_teams_for_user(db, user_id, league_id)
        return [Team(**t) for t in teams]
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/team/{team_id}/roster", response_model=Roster)
def team_roster(
    team_id: str,
    date: str | None = Query(default=None, description="Optional YYYY-MM-DD"),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    try:
        data = get_roster_for_user(db, user_id, team_id, date)
        return Roster(**data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
