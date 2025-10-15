from __future__ import annotations
from typing import Optional, Dict, Any
from sqlalchemy.orm import Session

from app.services.yahoo.client import yahoo_get
from app.services.yahoo.parsers import parse_scoreboard_min, parse_scoreboard_enriched

def get_scoreboard(
    db: Session,
    user_id: str,
    league_id: str,
    *,
    week: Optional[int] = None,
    enriched: bool = True,
) -> Dict[str, Any]:
    """
    Return this week's (or specified week) scoreboard for the league.
    If enriched=True, returns the parsed enriched structure; otherwise minimal.
    """
    wk = f";week={week}" if week else ""
    # try singular first
    payload = yahoo_get(db, user_id, f"/league/{league_id}/scoreboard{wk}")
    if enriched:
        data = parse_scoreboard_enriched(payload)
        if not data.get("matchups"):
            payload2 = yahoo_get(db, user_id, f"/leagues;league_keys={league_id}/scoreboard{wk}")
            data = parse_scoreboard_enriched(payload2)
        return data

    data = parse_scoreboard_min(payload)
    if not data.get("matchups"):
        payload2 = yahoo_get(db, user_id, f"/leagues;league_keys={league_id}/scoreboard{wk}")
        data = parse_scoreboard_min(payload2)
    return data
