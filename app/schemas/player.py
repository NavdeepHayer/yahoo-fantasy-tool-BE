from __future__ import annotations
from typing import List, Optional
from pydantic import BaseModel

class Player(BaseModel):
    player_id: str
    name: str
    team: Optional[str] = None
    positions: List[str] = []
    status: Optional[str] = None  # FA/W/T, IR, DTD, etc. when available
    jersey: Optional[str] = None
    shoots: Optional[str] = None
    height: Optional[str] = None
    weight: Optional[str] = None
    birthdate: Optional[str] = None
    yahoo_image_url: Optional[str] = None
    image_url: Optional[str] = None  # your enriched/fallback image if you add one later
    eligibility: List[str] = []      # mirrors positions but league-scoped when available

class PlayerSearchResponse(BaseModel):
    items: List[Player]
    page: int
    per_page: int
    next_page: Optional[int] = None
