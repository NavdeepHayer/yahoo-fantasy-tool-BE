from typing import Any, Dict, List, Optional
from sqlalchemy.orm import Session

from app.services.yahoo_client import yahoo_get
from app.services.yahoo import get_teams_for_user
from app.core.config import settings

def _normalize_team_obj(team_node: Any) -> Dict[str, Any]:
    """
    Yahoo 'team' can be:
      - dict
      - list of dicts
      - [ [ {core...}, {core...}, ... ], {team_stats...}, {team_points...}, ... ]

    This flattens ALL of it into a single dict so downstream lookups work:
      result["team_stats"]["stats"], result["team_points"]["total"], etc.
    """
    agg: Dict[str, Any] = {}

    def merge(d: dict):
        for k, v in d.items():
            agg[k] = v

    if isinstance(team_node, dict):
        merge(team_node)

    elif isinstance(team_node, list):
        # If first element is a list → merge every dict inside it (core fields)
        if team_node and isinstance(team_node[0], list):
            for part in team_node[0]:
                if isinstance(part, dict):
                    merge(part)
            # Also merge the remaining elements in the outer list (stats/points/etc.)
            for part in team_node[1:]:
                if isinstance(part, dict):
                    merge(part)
        else:
            # Plain list-of-dicts case
            for part in team_node:
                if isinstance(part, dict):
                    merge(part)

    return agg
def _find_my_team_key(db: Session, user_id: str, league_id: str) -> Optional[str]:
    """
    Use /me/my-team behavior:
    - fetch /users;use_login=1 for GUID
    - get teams for league
    - match manager GUID to get my team_key
    """
    users_payload = yahoo_get(db, user_id, "/users;use_login=1")
    guid = (
        users_payload.get("fantasy_content", {})
        .get("users", {})
        .get("0", {})
        .get("user", [{}])[0]
        .get("guid")
    )
    if not guid:
        return None

    teams = get_teams_for_user(db, user_id, league_id)
    for t in teams:
        if str(t.get("manager")) == str(guid):
            return t.get("id")
    return None

def _scoreboard_matchups(payload: dict) -> List[dict]:
    """
    Return a list of matchup objects from a scoreboard payload in ALL the weird shapes Yahoo uses.
    Each item is the raw 'matchup' dict.
    """
    out: List[dict] = []
    fc = payload.get("fantasy_content", {})
    league = fc.get("league")
    scoreboard = None
    if isinstance(league, list) and len(league) >= 2 and isinstance(league[1], dict):
        scoreboard = league[1].get("scoreboard")
    elif isinstance(league, dict):
        scoreboard = league.get("scoreboard")
    if not isinstance(scoreboard, dict):
        return out

    # typical nesting: scoreboard -> "0" -> { "matchups": {...} }
    container = scoreboard.get("0") if isinstance(scoreboard.get("0"), dict) else scoreboard
    matchups = container.get("matchups") if isinstance(container, dict) else None
    if not isinstance(matchups, dict):
        return out

    for k, v in matchups.items():
        if not str(k).isdigit() or not isinstance(v, dict):
            continue
        m = v.get("matchup")
        if isinstance(m, dict):
            out.append(m)
        elif isinstance(m, list):
            # rare: matchup is list-of-dicts -> flatten to one dict
            agg = {}
            for part in m:
                if isinstance(part, dict):
                    agg.update(part)
            out.append(agg)
    return out

def _extract_points_from_team(team_node: dict) -> Optional[str]:
    # team_node is the full team array/dict normalized already
    # points block usually sits alongside team_stats in the 2nd element of the team array
    # but after normalization, look for "team_points" nested dict
    tp = team_node.get("team_points")
    if isinstance(tp, dict):
        return tp.get("total")
    return None

def _extract_stats_map(team_node: dict) -> Dict[str, str]:
    # From the normalized "team" node, grab team_stats.stats list and build {stat_id: value}
    stats_block = team_node.get("team_stats", {})
    stats_list = stats_block.get("stats", [])
    out: Dict[str, str] = {}
    if isinstance(stats_list, list):
        for item in stats_list:
            st = item.get("stat", {}) if isinstance(item, dict) else {}
            sid = st.get("stat_id")
            val = st.get("value")
            if sid is not None:
                out[str(sid)] = str(val)
    return out

def get_my_weekly_matchups(
    db: Session,
    user_id: str,
    *,
    week: int | None = None,
    league_id: str | None = None,
    include_categories: bool = False,
    include_points: bool = True,
    limit: int | None = None,  # kept for signature parity
) -> dict:
    """
    Returns exactly ONE item (the user's matchup) when league_id is provided.
    If league_id is omitted, this could be extended to iterate leagues, but for now we focus on your use case.
    """
    if not league_id:
        return {"user_id": user_id, "week": week, "items": []}

    # 1) Find my team key in this league
    my_team_key = _find_my_team_key(db, user_id, league_id)
    if not my_team_key:
        return {"user_id": user_id, "week": week, "items": []}

    # 2) Fetch scoreboard for requested week (or current week if not provided)
    week_part = f";week={week}" if week else ""
    sb_payload = yahoo_get(db, user_id, f"/league/{league_id}/scoreboard{week_part}")

    # 3) Iterate matchups, find the one containing my team
    for m in _scoreboard_matchups(sb_payload):
        teams_obj = m.get("0", {}).get("teams") or m.get("teams")
        if not isinstance(teams_obj, dict):
            continue

        # extract team 0 and 1
        t0 = teams_obj.get("0", {}).get("team")
        t1 = teams_obj.get("1", {}).get("team")
        if t0 is None or t1 is None:
            continue

        t0n = _normalize_team_obj(t0)
        t1n = _normalize_team_obj(t1)

        k0 = t0n.get("team_key")
        k1 = t1n.get("team_key")
        if not k0 or not k1:
            continue

        if my_team_key not in (k0, k1):
            continue

        # we found the matchup: decide sides
        my_is_team0 = (k0 == my_team_key)
        my_team = t0n if my_is_team0 else t1n
        opp_team = t1n if my_is_team0 else t0n

        def _nice_name(n):
            if isinstance(n, dict):
                return n.get("full") or n.get("name")
            return n

        # Pull week meta
        use_week = m.get("week")
        week_start = m.get("week_start")
        week_end = m.get("week_end")

        # Optional scoring bits
        score_obj = None
        if include_categories or include_points:
            my_stats = _extract_stats_map(my_team)
            opp_stats = _extract_stats_map(opp_team)

            points_obj = None
            if include_points:
                p_my = _extract_points_from_team(my_team)
                p_opp = _extract_points_from_team(opp_team)
                points_obj = {"me": p_my, "opp": p_opp}

            cat_summary = None
            cat_breakdown = None
            if include_categories:
                # winners live at m["stat_winners"] as a list of {stat_winner:{...}}
                winners = []
                for it in m.get("stat_winners", []):
                    if isinstance(it, dict):
                        sw = it.get("stat_winner", {})
                        winners.append(sw)

                wins = losses = ties = 0
                cat_breakdown = []
                for sw in winners:
                    sid = str(sw.get("stat_id")) if sw.get("stat_id") is not None else None
                    is_tied = bool(sw.get("is_tied"))
                    winner_key = sw.get("winner_team_key")
                    leader = 0
                    if is_tied:
                        ties += 1
                        leader = 0
                    else:
                        leader = 1 if winner_key == (k0 if my_is_team0 else k1) else 2
                        if leader == 1:
                            wins += 1
                        else:
                            losses += 1
                    cat_breakdown.append({
                        "name": sid,  # you can map to display names later if you want
                        "me": my_stats.get(sid),
                        "opp": opp_stats.get(sid),
                        "leader": leader,  # 0=tied, 1=me, 2=opp
                    })
                cat_summary = {"wins": wins, "losses": losses, "ties": ties}

            score_obj = {
                "points": points_obj,
                "categories": cat_summary,
                "category_breakdown": cat_breakdown,
            }

        item = {
            "league_id": league_id,
            "week": int(use_week) if str(use_week).isdigit() else use_week,
            "start_date": week_start,
            "end_date": week_end,
            "team_id": my_team_key,
            "team_name": _nice_name(my_team.get("name")),
            "opponent_team_id": opp_team.get("team_key"),
            "opponent_team_name": _nice_name(opp_team.get("name")),
            "status": m.get("status"),
            "is_playoffs": str(m.get("is_playoffs", "0")) == "1",
            "score": score_obj,
        }
        return {"user_id": user_id, "week": week, "items": [item]}

    # If we got here, we didn’t find your team in any matchup
    return {"user_id": user_id, "week": week, "items": []}
