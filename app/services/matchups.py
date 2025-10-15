from __future__ import annotations
from typing import Any, List, Optional, Tuple
from sqlalchemy.orm import Session

from app.services.yahoo_client import yahoo_get
from app.services.yahoo_parsers import parse_scoreboard_min, select_matchup_for_team, parse_scoreboard_enriched
from app.services.leagues import _get  # reuse tiny helpers

def _get_my_guid(db: Session, user_id: str) -> str | None:
    try:
        payload = yahoo_get(db, user_id, "/users;use_login=1")
    except Exception:
        return None

    fc = payload.get("fantasy_content", {})
    users = fc.get("users")
    if isinstance(users, dict):
        u0 = users.get("0")
        if isinstance(u0, dict):
            user_node = u0.get("user")
            if isinstance(user_node, list):
                for part in user_node:
                    if isinstance(part, dict) and part.get("guid"):
                        return part["guid"]
            elif isinstance(user_node, dict):
                if user_node.get("guid"):
                    return user_node["guid"]
    return None

def _get_my_team_key_for_league(db: Session, user_id: str, league_id: str) -> tuple[str | None, str | None]:
    payload = yahoo_get(db, user_id, "/users;use_login=1/teams")
    fc = payload.get("fantasy_content", {})
    users_node = fc.get("users")
    if not isinstance(users_node, dict):
        return (None, None)

    user_list = []
    u = _get(users_node, "0", "user")
    if isinstance(u, list):
        user_list = u
    elif u:
        user_list = [u]

    for user in user_list:
        teams_node = _get(user, "teams")
        if not isinstance(teams_node, dict):
            continue
        for k, v in teams_node.items():
            if not str(k).isdigit() or not isinstance(v, dict):
                continue
            team_obj = v.get("team")
            if isinstance(team_obj, list):
                agg = {}
                for part in team_obj:
                    if isinstance(part, dict):
                        agg.update(part)
                team_obj = agg
            if not isinstance(team_obj, dict):
                continue

            t_key = team_obj.get("team_key")
            t_league_key = team_obj.get("league_key")
            if not t_league_key:
                lg = team_obj.get("league")
                if isinstance(lg, dict):
                    t_league_key = lg.get("league_key")

            if t_key and t_league_key and str(t_league_key) == str(league_id):
                nm = team_obj.get("name")
                if isinstance(nm, dict):
                    nm = nm.get("full") or nm.get("name")
                return (t_key, nm if isinstance(nm, str) else None)

    return (None, None)

def _get_league_settings_meta(db: Session, user_id: str, league_id: str) -> dict:
    payload = yahoo_get(db, user_id, f"/league/{league_id}/settings")
    fc = payload.get("fantasy_content", {})
    out = {"current_week": None, "weeks": [], "sport": None, "season": None, "league_name": None}

    L = fc.get("league")
    league_fields = None
    settings = None
    if isinstance(L, list) and len(L) >= 2:
        league_fields = L[0] if isinstance(L[0], dict) else None
        settings_wrapper = L[1] if isinstance(L[1], dict) else None
        if isinstance(settings_wrapper, dict):
            sl = settings_wrapper.get("settings")
            if isinstance(sl, list) and sl and isinstance(sl[0], dict):
                settings = sl[0]
    elif isinstance(L, dict):
        league_fields = L

    if isinstance(league_fields, dict):
        out["league_name"] = league_fields.get("name")
        out["season"] = league_fields.get("season")
        out["sport"] = league_fields.get("game_code")

    if isinstance(settings, dict):
        out["current_week"] = settings.get("current_week")
        sched = settings.get("schedule") or settings.get("weeks")
        if isinstance(sched, dict):
            for k, v in sched.items():
                if not str(k).isdigit() or not isinstance(v, dict):
                    continue
                w = v.get("week") or int(k)
                out["weeks"].append({
                    "week": w,
                    "start_date": v.get("start_date"),
                    "end_date": v.get("end_date"),
                    "is_playoffs": bool(v.get("is_playoffs")) if "is_playoffs" in v else False,
                })
    return out

def _get_stat_id_map(db: Session, user_id: str, league_id: str) -> dict[str, str]:
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
        sc = settings.get("stat_categories")
        stats = None
        if isinstance(sc, dict):
            stats = sc.get("stats")
        if isinstance(stats, list):
            for item in stats:
                if isinstance(item, dict):
                    st = item.get("stat", {})
                    sid = st.get("stat_id")
                    dn = st.get("display_name") or st.get("name")
                    if sid is not None and dn:
                        stat_map[str(sid)] = str(dn)
        elif isinstance(stats, dict):
            for k, v in stats.items():
                if not str(k).isdigit() or not isinstance(v, dict):
                    continue
                st = v.get("stat", {})
                sid = st.get("stat_id")
                dn = st.get("display_name") or st.get("name")
                if sid is not None and dn:
                    stat_map[str(sid)] = str(dn)
    return stat_map

def _find_my_team_key_from_scoreboard_payload(scoreboard_payload: dict, my_guid: str | None = None) -> tuple[str | None, str | None]:
    fc = scoreboard_payload.get("fantasy_content", {})
    league = fc.get("league")
    sb = None
    if isinstance(league, list) and len(league) >= 2 and isinstance(league[1], dict):
        sb = league[1].get("scoreboard")
    elif isinstance(league, dict):
        sb = league.get("scoreboard")
    if not isinstance(sb, dict):
        return (None, None)

    matchups = None
    if "0" in sb and isinstance(sb["0"], dict):
        inner = sb["0"]
        matchups = inner.get("matchups")
    else:
        matchups = sb.get("matchups")
    if not isinstance(matchups, dict):
        return (None, None)

    def is_me_team(team_obj: dict) -> bool:
        if str(team_obj.get("is_current_login", "0")) == "1":
            return True
        if str(team_obj.get("is_owned_by_current_login", "0")) == "1":
            return True
        if my_guid:
            mgrs = team_obj.get("managers")
            if isinstance(mgrs, list):
                for it in mgrs:
                    if isinstance(it, dict):
                        m = it.get("manager")
                        if isinstance(m, dict) and m.get("guid") == my_guid:
                            return True
            if isinstance(mgrs, dict):
                for k, v in mgrs.items():
                    if str(k).isdigit() and isinstance(v, dict):
                        m = v.get("manager")
                        if isinstance(m, dict) and m.get("guid") == my_guid:
                            return True
        return False

    def normalize_team(node: Any) -> dict:
        if isinstance(node, list):
            agg = {}
            for part in node:
                if isinstance(part, dict):
                    agg.update(part)
            return agg
        return node if isinstance(node, dict) else {}

    for _, mv in matchups.items():
        if not isinstance(mv, dict):
            continue
        m = mv.get("matchup")
        if not isinstance(m, (dict, list)):
            continue
        if isinstance(m, list):
            agg = {}
            for part in m:
                if isinstance(part, dict):
                    agg.update(part)
            m = agg
        teams = m.get("teams")
        if not isinstance(teams, dict):
            continue
        for tk in ("0", "1"):
            t = teams.get(tk, {}).get("team")
            if t is None:
                continue
            t = normalize_team(t)
            if is_me_team(t):
                nm = t.get("name")
                if isinstance(nm, dict):
                    nm = nm.get("full") or nm.get("name")
                return (t.get("team_key"), nm if isinstance(nm, str) else None)
    return (None, None)

def get_my_weekly_matchups(
    db: Session,
    user_id: str,
    *,
    week: int | None = None,
    sport: str | None = None,
    season: int | None = None,
    league_id: str | None = None,
    include_categories: bool = False,
    include_points: bool = True,
    limit: int | None = None,
) -> dict:
    items: List[dict] = []

    if league_id:
        try:
            meta = _get_league_settings_meta(db, user_id, league_id)
            league_list = [{
                "id": league_id,
                "name": meta.get("league_name"),
                "season": meta.get("season"),
                "sport": meta.get("sport"),
            }]
        except Exception:
            league_list = [{"id": league_id, "name": None, "season": None, "sport": None}]
    else:
        from app.services.yahoo.leagues import get_leagues  # local import to avoid cycles
        league_list = get_leagues(db, user_id, sport=sport, season=season)
        if limit:
            league_list = league_list[:limit]

    requested_week = week
    my_guid = _get_my_guid(db, user_id)

    for L in league_list:
        lid = L.get("id")
        if not lid:
            continue

        my_team_key, my_team_name = _get_my_team_key_for_league(db, user_id, lid)
        if not my_team_key:
            from app.services.yahoo.teams import _find_my_team_key_from_teams_payload
            teams_payload = yahoo_get(db, user_id, f"/league/{lid}/teams")
            my_team_key, my_team_name = _find_my_team_key_from_teams_payload(teams_payload, my_guid)
            if not my_team_key:
                teams_payload2 = yahoo_get(db, user_id, f"/leagues;league_keys={lid}/teams")
                my_team_key, my_team_name = _find_my_team_key_from_teams_payload(teams_payload2, my_guid)

        if not my_team_key:
            week_part = f";week={requested_week}" if requested_week else ""
            sb_try = yahoo_get(db, user_id, f"/league/{lid}/scoreboard{week_part}")
            my_team_key, my_team_name = _find_my_team_key_from_scoreboard_payload(sb_try, my_guid)
            if not my_team_key:
                sb_try2 = yahoo_get(db, user_id, f"/leagues;league_keys={lid}/scoreboard{week_part}")
                my_team_key, my_team_name = _find_my_team_key_from_scoreboard_payload(sb_try2, my_guid)

        if not my_team_key:
            continue

        meta = _get_league_settings_meta(db, user_id, lid)
        use_week = requested_week or meta.get("current_week")
        week_part = f";week={use_week}" if use_week else ""

        sb_payload = yahoo_get(db, user_id, f"/league/{lid}/scoreboard{week_part}")
        sb_min = parse_scoreboard_min(sb_payload)
        if not sb_min.get("matchups"):
            sb_payload2 = yahoo_get(db, user_id, f"/leagues;league_keys={lid}/scoreboard{week_part}")
            sb_min = parse_scoreboard_min(sb_payload2)

        m = select_matchup_for_team(sb_min, my_team_key)
        if not m:
            continue

        if m["team1_key"] == my_team_key:
            opp_key, opp_name = m.get("team2_key"), m.get("team2_name")
        else:
            opp_key, opp_name = m.get("team1_key"), m.get("team1_name")

        score_obj = None
        if include_categories or include_points:
            sb_enriched = parse_scoreboard_enriched(sb_payload)
            if not sb_enriched.get("matchups"):
                sb_payload2 = yahoo_get(db, user_id, f"/leagues;league_keys={lid}/scoreboard{week_part}")
                sb_enriched = parse_scoreboard_enriched(sb_payload2)

            chosen = None
            for mm in sb_enriched.get("matchups", []):
                t1k = mm.get("team1", {}).get("key")
                t2k = mm.get("team2", {}).get("key")
                if t1k and t2k and (my_team_key in [t1k, t2k]):
                    chosen = mm
                    break

            if chosen:
                stat_map = _get_stat_id_map(db, user_id, lid)

                t1 = chosen["team1"]; t2 = chosen["team2"]
                my_is_team1 = (t1.get("key") == my_team_key)

                rows = []
                wins_me = losses_me = ties_me = 0
                for w in chosen.get("winners", []):
                    sid = w.get("stat_id")
                    if not sid:
                        continue
                    name = stat_map.get(sid, sid)
                    v1 = t1.get("stats", {}).get(sid)
                    v2 = t2.get("stats", {}).get(sid)

                    if w.get("is_tied"):
                        leader = 0
                        ties_me += 1
                    else:
                        winner_key = w.get("winner_team_key")
                        leader = 1 if winner_key == t1.get("key") else 2
                        if my_is_team1:
                            if leader == 1: wins_me += 1
                            else: losses_me += 1
                        else:
                            if leader == 2: wins_me += 1
                            else: losses_me += 1

                    leader_norm = leader if my_is_team1 else (2 if leader == 1 else (1 if leader == 2 else 0))

                    rows.append({
                        "name": name,
                        "me": v1 if my_is_team1 else v2,
                        "opp": v2 if my_is_team1 else v1,
                        "leader": leader_norm,
                    })

                cat_summary = {"wins": wins_me, "losses": losses_me, "ties": ties_me} if include_categories else None
                points_obj = None
                if include_points:
                    p1 = t1.get("points"); p2 = t2.get("points")
                    points_obj = {"me": p1 if my_is_team1 else p2, "opp": p2 if my_is_team1 else p1}

                score_obj = {
                    "points": points_obj,
                    "categories": cat_summary,
                    "category_breakdown": rows if include_categories else None,
                }

        items.append({
            "league_id": lid,
            "league_name": L.get("name") or meta.get("league_name"),
            "season": L.get("season") or meta.get("season"),
            "sport": L.get("sport") or meta.get("sport"),
            "week": sb_min.get("week") or use_week,
            "start_date": sb_min.get("start_date"),
            "end_date": sb_min.get("end_date"),
            "team_id": my_team_key,
            "team_name": my_team_name,
            "opponent_team_id": opp_key,
            "opponent_team_name": opp_name,
            "status": m.get("status"),
            "is_playoffs": m.get("is_playoffs"),
            "score": score_obj,
        })

    return {"user_id": user_id, "week": requested_week, "items": items}
