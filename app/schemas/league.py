from pydantic import BaseModel
from typing import List, Optional

class League(BaseModel):
    id: str
    name: str
    season: str
    scoring_type: str
    categories: List[str]
    current_week: Optional[int] = None
