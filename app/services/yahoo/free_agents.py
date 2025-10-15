from __future__ import annotations
from typing import List, Optional
from sqlalchemy.orm import Session

from app.services.yahoo.client import yahoo_get

def _norm_player(p: dict) -> dict:
    # Flatten common Yahoo "player" list/dict shapes
    if isinstance(p, list):
        agg = {}
        for part in p:
            if isinstance(part, dict):
                agg.update(part)
        p = agg if isinstance(agg, dict) else {}
    return p if isinstance(p, dict) else {}

def search_free_agents(
    db: Session,
    user_id: str,
    league_id: str,
    *,
    position: Optional[str] = None,   # e.g., "G", "F", "C", "PG" etc.
    query: Optional[str] = None,      # free text search
    count: int = 25,
    start: int = 0,
    status: str = "FA",               # Yahoo statuses: FA (free agent), W (waivers), T (all)
) -> List[dict]:
    """
    Returns a normalized list of free agents:
    [{player_id, player_key, name, team, positions:[...], status, percent_owned, editorial_team_abbr}]
    """
    # Build Yahoo collection filters
    filters = [f"status={status}", f"count={count}"]
    if start:
        filters.append(f"start={start}")
    if position:
        filters.append(f"position={position}")
    if query:
        # Yahoo supports ;search= query for players collections
        filters.append(f"search={query}")

    # /league/{league_id}/players;status=FA;position=...;search=...;start=0;count=25
    path = f"/{league_id}/players;{';'.join(filters)}"
    payload = yahoo_get(db, user_id, path)

    # Locate players node across shapes
    fc = payload.get("fantasy_content", {})
    league = fc.get("league")
    players_node = None
    if isinstance(league, list) and len(league) >= 2 and isinstance(league[1], dict):
        players_node = league[1].get("players")
    elif isinstance(league, dict):
        players_node = league.get("players")

    out: List[dict] = []
    if not isinstance(players_node, dict):
        return out

    for k, v in players_node.items():
        if not str(k).isdigit() or not isinstance(v, dict):
            continue
        p = _norm_player(v.get("player"))
        if not p:
            continue

        # identifiers
        player_key = p.get("player_key")
        pid = None
        if isinstance(p.get("player_id"), (str, int)):
            pid = str(p.get("player_id"))

        # name
        name = p.get("name")
        full_name = None
        if isinstance(name, dict):
            full_name = name.get("full") or name.get("name")
        elif isinstance(name, str):
            full_name = name

        # team and positions
        team_abbr = p.get("editorial_team_abbr") or p.get("editorial_team")
        display_pos = p.get("display_position")  # e.g., "PG,SG"
        positions = [s.strip() for s in display_pos.split(",")] if isinstance(display_pos, str) else []

        # status & ownership (if present)
        status_txt = p.get("status")  # e.g., "FA", "W", "NA", "IR", etc.
        percent_owned = None
        ownership = p.get("percent_owned") or p.get("ownership")
        if isinstance(ownership, dict):
            po = ownership.get("percent_owned")
            try:
                percent_owned = float(po) if po is not None else None
            except Exception:
                percent_owned = None
        elif ownership is not None:
            try:
                percent_owned = float(ownership)
            except Exception:
                percent_owned = None

        out.append({
            "player_id": pid,
            "player_key": player_key,
            "name": full_name,
            "team": team_abbr,
            "positions": positions,
            "status": status_txt,
            "percent_owned": percent_owned,
            "editorial_team_abbr": team_abbr,
        })

    return out
