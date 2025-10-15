from __future__ import annotations
from pydantic import BaseModel
from typing import List, Optional

class FreeAgent(BaseModel):
    player_id: Optional[str] = None
    player_key: Optional[str] = None
    name: Optional[str] = None
    team: Optional[str] = None
    positions: List[str] = []
    status: Optional[str] = None
    percent_owned: Optional[float] = None
    editorial_team_abbr: Optional[str] = None
