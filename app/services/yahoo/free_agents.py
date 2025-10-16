from __future__ import annotations
from typing import Any, Dict, List, Optional, Iterable
from sqlalchemy.orm import Session

from app.services.yahoo.client import yahoo_get


def _flatten_list_dicts(node: Any) -> Dict[str, Any]:
    """
    Yahoo often returns player as: [ [ {..}, {..}, ... ] ] or a mix of lists/dicts.
    This recursively walks lists and merges dicts into one flat dict (last key wins).
    """
    out: Dict[str, Any] = {}
    if isinstance(node, dict):
        return dict(node)
    if isinstance(node, list):
        for item in node:
            if isinstance(item, dict):
                out.update(item)
            elif isinstance(item, list):
                out.update(_flatten_list_dicts(item))
    return out


def _parse_percent_owned(raw: Any) -> Optional[float]:
    """
    percent_owned can arrive in a few shapes:
      - {"value": "97"} or {"value": "97%"}
      - "97" (string) or 97 (number)
      - nested under 'ownership' -> {'percent_owned': '97'}
    Returns float in 0..100 or None.
    """
    def _to_float(x: Any) -> Optional[float]:
        if x is None:
            return None
        if isinstance(x, (int, float)):
            return float(x)
        if isinstance(x, str):
            s = x.strip().rstrip("%")
            try:
                return float(s)
            except Exception:
                return None
        return None

    if isinstance(raw, dict):
        # direct percent_owned dict
        if "value" in raw:
            return _to_float(raw.get("value"))
        if "percent_owned" in raw:  # sometimes appears like that
            return _to_float(raw.get("percent_owned"))

    # Already a primitive
    return _to_float(raw)


def search_free_agents(
    db: Session,
    user_id: str,
    league_id: str,
    *,
    position: Optional[str] = None,     # e.g., "G", "F", "C", "PG" etc.
    query: Optional[str] = None,        # free text search
    count: int = 25,                    # Yahoo hard max is 25 per page
    start: int = 0,
    status: str = "FA",                 # FA (free agent), W (waivers), A/T (all)
    include_out: Optional[Iterable[str]] = ("ownership", "percent_owned"),  # ask Yahoo for these blocks
) -> List[dict]:
    """
    Returns a normalized list of free agents:
    [{player_id, player_key, name, team, positions:[...], status, percent_owned, editorial_team_abbr}]
    NOTE: To actually get percent_owned/ownership, Yahoo needs ;out=ownership,percent_owned
    (provided by include_out).
    """
    # Build Yahoo collection filters
    filters = [f"status={status}", f"count={count}"]
    if start:
        filters.append(f"start={start}")
    if position:
        filters.append(f"position={position}")
    if query:
        filters.append(f"search={query}")
    if include_out:
        out_str = ",".join(include_out)
        filters.append(f"out={out_str}")

    # /league/{league_id}/players;status=FA;position=...;search=...;start=0;count=25;out=ownership,percent_owned
    path = f"/league/{league_id}/players;" + ";".join(filters)

    payload = yahoo_get(db=db, user_id=user_id, path=path)

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

        # Yahoo has v["player"] which is usually a list (sometimes nested list)
        raw_player = v.get("player")
        p = _flatten_list_dicts(raw_player)
        if not p:
            continue

        # identifiers
        player_key = p.get("player_key")
        pid = str(p.get("player_id")) if isinstance(p.get("player_id"), (str, int)) else None

        # name
        full_name = None
        name = p.get("name")
        if isinstance(name, dict):
            full_name = name.get("full") or name.get("name")
        elif isinstance(name, str):
            full_name = name

        # team + positions
        team_abbr = p.get("editorial_team_abbr") or p.get("editorial_team")
        display_pos = p.get("display_position")  # e.g., "PG,SG"
        positions = [s.strip() for s in display_pos.split(",")] if isinstance(display_pos, str) else []

        # status
        status_txt = p.get("status")

        # ownership / percent_owned (several shapes)
        percent_owned = None
        # Direct percent_owned block if Yahoo returned it via ;out=percent_owned
        if "percent_owned" in p:
            percent_owned = _parse_percent_owned(p.get("percent_owned"))
        # Sometimes wrapped under ownership
        if percent_owned is None and isinstance(p.get("ownership"), dict):
            percent_owned = _parse_percent_owned(p["ownership"].get("percent_owned"))

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
