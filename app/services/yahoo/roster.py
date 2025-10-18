# app/services/yahoo/roster.py
from __future__ import annotations
from typing import Optional, Dict, Any, List
from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.yahoo.client import yahoo_get
from app.services.yahoo.parsers import parse_roster

def _ensure_slot_field(players: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Make sure every player dict includes 'slot' (None if parser didn't set it).
    """
    out: List[Dict[str, Any]] = []
    for p in players or []:
        if "slot" not in p:
            p = {**p, "slot": None}
        out.append(p)
    return out

def get_roster_for_user(
    db: Session,
    user_id: str,
    team_id: str,
    date: Optional[str] = None
) -> Dict[str, Any]:
    """
    Fetch roster for a team, always returning players with a 'slot' key.
    Tries the /team/{id}/roster path first, then the /teams;team_keys=... fallback.
    """
    if settings.YAHOO_FAKE_MODE:
        return {
            "team_id": team_id,
            "date": date or "2025-10-10",
            "players": _ensure_slot_field([
                {"player_id": "nba.p.201939", "name": "Stephen Curry", "positions": ["PG"], "status": "ACTIVE", "slot": "PG"},
                {"player_id": "nba.p.2544", "name": "LeBron James", "positions": ["SF", "PF"], "status": "BN", "slot": "BN"},
            ]),
        }

    date_part = f";date={date}" if date else ""

    # primary request
    payload = yahoo_get(db, user_id, f"/team/{team_id}/roster{date_part}")
    r_date, players = parse_roster(payload, team_id)
    players = _ensure_slot_field(players)
    if players:
        return {"team_id": team_id, "date": r_date or (date or ""), "players": players}

    # fallback request
    payload2 = yahoo_get(db, user_id, f"/teams;team_keys={team_id}/roster{date_part}")
    r_date2, players2 = parse_roster(payload2, team_id)
    players2 = _ensure_slot_field(players2)
    return {"team_id": team_id, "date": r_date2 or (date or ""), "players": players2}
