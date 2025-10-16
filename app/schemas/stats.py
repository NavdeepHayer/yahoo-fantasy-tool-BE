from __future__ import annotations
from typing import Dict, List, Optional
from pydantic import BaseModel

class PlayerStatQuery(BaseModel):
    league_id: str
    season: Optional[str] = None
    week: Optional[int] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    kind: str = "season"  # season | last7 | last14 | last30 | date_range | week

class PlayerStatLine(BaseModel):
    player_id: str
    scope: str                # e.g., "season:2025", "week:2", "date_range:2025-10-01..2025-10-08"
    values: Dict[str, float]  # keyed by *league* category keys (e.g., {"FGM": 34, "AST": 22, ...})

class TeamWeeklyStats(BaseModel):
    league_id: str
    team_id: str
    week: int
    totals: Dict[str, float]         # league categories total for the team that week
    players: List[PlayerStatLine]    # individual player lines if you want to render a breakdown table
