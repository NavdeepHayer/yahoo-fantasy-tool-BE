# app/services/yahoo_matchups.py
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy.orm import Session

from app.services.yahoo_client import yahoo_get
from app.services.yahoo import get_teams_for_user
from app.core.config import settings


# ----------------------------- utils -----------------------------

def _normalize_team_obj(team_node: Any) -> Dict[str, Any]:
    """
    Yahoo 'team' can be:
      - dict
      - list of dicts
      - nested list [[{...},{...}, ...]]
    Flatten into a single dict while preserving common nested blocks
    (team_stats, team_points) as-is when encountered.
    """
    agg: Dict[str, Any] = {}

    def merge(d: dict):
        for k, v in d.items():
            # keep last wins, yahoo fragments are mostly disjoint
            agg[k] = v

    if isinstance(team_node, dict):
        merge(team_node)
    elif isinstance(team_node, list):
        first = team_node[0] if team_node else None
        if isinstance(first, list):
            for part in first:
                if isinstance(part, dict):
                    merge(part)
        else:
            for part in team_node:
                if isinstance(part, dict):
                    merge(part)
    return agg


def _extract_points_from_team(team_node: dict) -> Optional[str]:
    """From the normalized team object, pull team_points.total if present."""
    tp = team_node.get("team_points")
    if isinstance(tp, dict):
        return tp.get("total")
    return None


def _extract_stats_map(team_node: dict) -> Dict[str, str]:
    """
    From the normalized team object, build {stat_id: value} using team_stats.stats.
    """
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


def _num(val: Optional[str]):
    """
    Best-effort numeric coercion for stat/point strings.
    Returns int if whole number, else float. Falls back to original.
    """
    if val is None:
        return None
    try:
        f = float(val)
        return int(f) if f.is_integer() else f
    except Exception:
        return val


# ----------------------------- lookups -----------------------------

def _get_stat_id_map(db: Session, user_id: str, league_id: str) -> dict[str, str]:
    """
    Map stat_id -> display_name using league settings.
    Works across sports (NHL/NBA/NFL/MLB), Yahoo format is consistent.
    """
    payload = yahoo_get(db, user_id, f"/league/{league_id}/settings")
    fc = payload.get("fantasy_content", {})
    L = fc.get("league")
    settings = None

    if isinstance(L, list) and len(L) >= 2 and isinstance(L[1], dict):
        sl = L[1].get("settings")
        if isinstance(sl, list) and sl and isinstance(sl[0], dict):
            settings = sl[0]
    elif isinstance(L, dict):
        settings = L.get("settings")

    stat_map: dict[str, str] = {}
    if isinstance(settings, dict):
        sc = settings.get("stat_categories", {})
        stats = sc.get("stats")
        if isinstance(stats, list):
            for item in stats:
                st = item.get("stat", {}) if isinstance(item, dict) else {}
                sid = st.get("stat_id")
                dn = st.get("display_name") or st.get("name")
                if sid is not None and dn:
                    stat_map[str(sid)] = str(dn)
        elif isinstance(stats, dict):
            # sometimes "stats" is numeric-keyed dict
            for k, v in stats.items():
                if not str(k).isdigit() or not isinstance(v, dict):
                    continue
                st = v.get("stat", {})
                sid = st.get("stat_id")
                dn = st.get("display_name") or st.get("name")
                if sid is not None and dn:
                    stat_map[str(sid)] = str(dn)

    return stat_map


def _find_my_team_key(db: Session, user_id: str, league_id: str) -> Optional[str]:
    """
    Resolve 'my' team_key in a league by:
      1) Fetching my GUID via /users;use_login=1
      2) Fetching teams for that league and matching manager GUID
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
    Extract the list of 'matchup' dicts from a scoreboard payload
    (handles both /league/... and /leagues;league_keys=... shapes).
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
            # rare: 'matchup' is list-of-dicts -> flatten
            agg = {}
            for part in m:
                if isinstance(part, dict):
                    agg.update(part)
            out.append(agg)
    return out


# ----------------------------- main API -----------------------------

def get_my_weekly_matchups(
    db: Session,
    user_id: str,
    *,
    week: int | None = None,
    league_id: str | None = None,
    include_categories: bool = False,
    include_points: bool = True,
    limit: int | None = None,  # kept for signature parity
    numeric_values: bool = False,  # set True if you want numbers instead of strings
) -> dict:
    """
    Return YOUR matchup for the given league/week (one item).
    If league_id is omitted, you could extend to iterate across leagues; current use focuses on the given league.

    Works across sports (NHL/NBA/NFL/MLB) as long as the scoring type is H2H with stat_winners and/or points.
    """
    if not league_id:
        return {"user_id": user_id, "week": week, "items": []}

    # 1) Find my team in this league
    my_team_key = _find_my_team_key(db, user_id, league_id)
    if not my_team_key:
        return {"user_id": user_id, "week": week, "items": []}

    # 2) Fetch scoreboard for requested week (or current if not provided)
    week_part = f";week={week}" if week else ""
    sb_payload = yahoo_get(db, user_id, f"/league/{league_id}/scoreboard{week_part}")

    # 3) Iterate matchups and find mine
    for m in _scoreboard_matchups(sb_payload):
        teams_obj = m.get("0", {}).get("teams") or m.get("teams")
        if not isinstance(teams_obj, dict):
            continue

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

        # we found the matchup
        my_is_team0 = (k0 == my_team_key)
        my_team = t0n if my_is_team0 else t1n
        opp_team = t1n if my_is_team0 else t0n

        def _nice_name(n):
            if isinstance(n, dict):
                return n.get("full") or n.get("name")
            return n

        use_week = m.get("week")
        week_start = m.get("week_start")
        week_end = m.get("week_end")

        # build score object
        score_obj = None
        if include_categories or include_points:
            my_stats = _extract_stats_map(my_team)
            opp_stats = _extract_stats_map(opp_team)

            # points
            points_obj = None
            if include_points:
                p_my = _extract_points_from_team(my_team)
                p_opp = _extract_points_from_team(opp_team)
                if numeric_values:
                    p_my = _num(p_my)
                    p_opp = _num(p_opp)
                points_obj = {"me": p_my, "opp": p_opp}

            # categories (respect Yahoo winners, so lower-is-better cats are already handled)
            cat_summary = None
            cat_breakdown = None
            if include_categories:
                winners = []
                for it in m.get("stat_winners", []):
                    if isinstance(it, dict):
                        sw = it.get("stat_winner", {})
                        winners.append(sw)

                # stat names
                stat_map = _get_stat_id_map(db, user_id, league_id)

                wins = losses = ties = 0
                cat_breakdown = []
                for sw in winners:
                    sid = str(sw.get("stat_id")) if sw.get("stat_id") is not None else None
                    is_tied = bool(sw.get("is_tied"))
                    winner_key = sw.get("winner_team_key")

                    if is_tied:
                        leader = 0
                        ties += 1
                    else:
                        # leader relative to *me*
                        leader_abs = 1 if winner_key == (k0 if my_is_team0 else k1) else 2
                        leader = 1 if leader_abs == 1 else 2
                        if leader == 1:
                            wins += 1
                        else:
                            losses += 1

                    me_v = my_stats.get(sid)
                    opp_v = opp_stats.get(sid)
                    if numeric_values:
                        me_v = _num(me_v)
                        opp_v = _num(opp_v)

                    cat_breakdown.append({
                        "name": stat_map.get(sid, sid),
                        "me": me_v,
                        "opp": opp_v,
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

    # none found
    return {"user_id": user_id, "week": week, "items": []}
