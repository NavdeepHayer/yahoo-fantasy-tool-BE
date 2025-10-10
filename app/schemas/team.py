from pydantic import BaseModel
from typing import List

class Team(BaseModel):
    id: str
    name: str
    manager: str | None = None

class RosterPlayer(BaseModel):
    player_id: str
    name: str
    positions: List[str] = []
    status: str | None = None  # ACTIVE/BN/IR etc.

class Roster(BaseModel):
    team_id: str
    date: str
    players: List[RosterPlayer]
