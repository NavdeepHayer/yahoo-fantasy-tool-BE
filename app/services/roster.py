from __future__ import annotations
from typing import Optional
from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.yahoo_client import yahoo_get
from app.services.yahoo_parsers import parse_roster

def get_roster_for_user(db: Session, user_id: str, team_id: str, date: Optional[str] = None) -> dict:
    if settings.YAHOO_FAKE_MODE:
        return {
            "team_id": team_id,
            "date": date or "2025-10-10",
            "players": [
                {"player_id": "nba.p.201939", "name": "Stephen Curry", "positions": ["PG"], "status": "ACTIVE"},
                {"player_id": "nba.p.2544", "name": "LeBron James", "positions": ["SF", "PF"], "status": "BN"},
            ],
        }

    date_part = f";date={date}" if date else ""

    payload = yahoo_get(db, user_id, f"/team/{team_id}/roster{date_part}")
    r_date, players = parse_roster(payload, team_id)
    if players:
        return {"team_id": team_id, "date": r_date or (date or ""), "players": players}

    payload2 = yahoo_get(db, user_id, f"/teams;team_keys={team_id}/roster{date_part}")
    r_date2, players2 = parse_roster(payload2, team_id)
    return {"team_id": team_id, "date": r_date2 or (date or ""), "players": players2}
