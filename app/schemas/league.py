from pydantic import BaseModel
from typing import List

class League(BaseModel):
    id: str
    name: str
    season: str
    scoring_type: str
    categories: List[str]
