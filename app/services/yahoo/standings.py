from __future__ import annotations

from typing import Any, Dict, List, Optional
from sqlalchemy.orm import Session

from app.services.yahoo.client import yahoo_get


def _coerce_list(x: Any) -> List[Any]:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _maybe_int(v: Any) -> Optional[int]:
    try:
        if v is None:
            return None
        s = str(v).strip()
        return int(s) if s != "" else None
    except Exception:
        return None


def _maybe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        s = str(v).strip()
        return float(s) if s != "" else None
    except Exception:
        return None


def _extract_team_core(core_list: List[Any]) -> Dict[str, Any]:
    """
    Yahoo puts the 'team' core as a LIST of small dicts + [] gaps.
    This squashes it into a flat dict with key values we care about.
    """
    out: Dict[str, Any] = {}
    for node in core_list:
        if not isinstance(node, dict):
            continue
        # simple 1-key dicts; pull known fields
        if "team_key" in node:
            out["team_key"] = node["team_key"]
        if "team_id" in node:
            out["team_id"] = str(node["team_id"])
        if "name" in node:
            out["name"] = node["name"]
        if "url" in node:
            out["url"] = node["url"]
        if "team_logos" in node:
            try:
                logo = node["team_logos"][0]["team_logo"]["url"]
                out["logo_url"] = logo
            except Exception:
                pass
        if "managers" in node:
            try:
                mgr = node["managers"][0]["manager"]
                out["manager"] = {
                    "guid": mgr.get("guid"),
                    "nickname": mgr.get("nickname"),
                    "manager_id": mgr.get("manager_id"),
                    "email": mgr.get("email"),
                }
            except Exception:
                pass
    return out


def _parse_teams(teams_obj: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(teams_obj, dict):
        return out

    for k in sorted((kk for kk in teams_obj.keys() if str(kk).isdigit()), key=lambda x: int(x)):
        entry = teams_obj.get(k)
        if not isinstance(entry, dict):
            continue
        team_list = entry.get("team")
        if not isinstance(team_list, list) or not team_list:
            continue

        core_block: List[Any] = []
        standings_block: Dict[str, Any] = {}

        for item in team_list:
            if isinstance(item, list):
                core_block = item
            elif isinstance(item, dict) and "team_standings" in item:
                standings_block = item.get("team_standings") or {}

        core = _extract_team_core(core_block)

        rank = _maybe_int(standings_block.get("rank"))

        ot = standings_block.get("outcome_totals") or {}
        wins = _maybe_int(ot.get("wins")) or 0
        losses = _maybe_int(ot.get("losses")) or 0
        ties = _maybe_int(ot.get("ties")) or 0
        pct = _maybe_float(ot.get("percentage"))  # Yahoo may return ""

        # points/roto extras
        points = _maybe_float(standings_block.get("points")) or _maybe_float(standings_block.get("points_for"))
        points_back = standings_block.get("points_back")
        streak = standings_block.get("streak")

        # Keep percentage=None until games exist; otherwise compute from W-L-T
        if pct is None:
            games = wins + losses + ties
            pct = ((wins + 0.5 * ties) / games) if games > 0 else None

        out.append({
            **core,
            "rank": rank,
            "wins": wins,
            "losses": losses,
            "ties": ties,
            "percentage": pct,      # None before games start
            "points": points,
            "points_back": points_back,
            "streak": streak,
        })

    return out


def get_league_standings(db: Session, user_id: str, league_id: str) -> Dict[str, Any]:
    """
    Calls /league/{league_id}/standings and returns a normalized payload:
    {
      "league_id": "...",
      "season": "2025",
      "scoring_type": "head",
      "items": [ { team_id, team_key, name, manager:{guid,nickname,...}, rank, wins, losses, ties, percentage, logo_url? }, ... ]
    }
    """
    payload = yahoo_get(db, user_id, f"/league/{league_id}/standings")
    fc = payload.get("fantasy_content", {})
    league_node = fc.get("league")

    season = None
    scoring_type = None
    teams_obj = None

    # league is usually a list: [ {fields...}, {standings:[ {teams:{...}} ]} ]
    if isinstance(league_node, list) and len(league_node) >= 2:
        fields = league_node[0] if isinstance(league_node[0], dict) else {}
        season = fields.get("season")
        scoring_type = fields.get("scoring_type")

        standings_wrap = league_node[1] if isinstance(league_node[1], dict) else {}
        standings_list = standings_wrap.get("standings")
        if isinstance(standings_list, list) and standings_list and isinstance(standings_list[0], dict):
            teams_obj = standings_list[0].get("teams")

    items = _parse_teams(teams_obj or {})
    # if rank is empty at predraft, it's "", keep None instead of empty string
    for it in items:
        if it.get("rank") is None and isinstance(it.get("rank"), str):
            it["rank"] = None

    return {
        "league_id": league_id,
        "season": str(season) if season is not None else None,
        "scoring_type": scoring_type,
        "items": items,
    }
