from typing import List, Optional
from pydantic import BaseModel

class MyCategoryRow(BaseModel):
    name: Optional[str] = None
    me: Optional[str] = None
    opp: Optional[str] = None
    leader: Optional[int] = None  # 1=me, 2=opp, 0=tie

class MyScore(BaseModel):
    points: Optional[dict] = None              # {"me": float, "opp": float}
    categories: Optional[dict] = None          # {"wins": int, "losses": int, "ties": int}
    category_breakdown: Optional[List[MyCategoryRow]] = None

class MyWeeklyMatchupItem(BaseModel):
    league_id: str
    league_name: Optional[str] = None
    season: Optional[str] = None
    sport: Optional[str] = None
    week: Optional[int] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    team_id: str
    team_name: Optional[str] = None
    opponent_team_id: Optional[str] = None
    opponent_team_name: Optional[str] = None
    status: Optional[str] = None          # "pre" | "in_progress" | "final" (best-effort)
    is_playoffs: Optional[bool] = None
    score: Optional[MyScore] = None       # populated later as we enrich

class MyWeeklyMatchups(BaseModel):
    user_id: str
    week: Optional[int] = None            # the requested/derived week (if consistent)
    items: List[MyWeeklyMatchupItem]
