# app/services/yahoo/matchups.py
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy.orm import Session

from app.services.yahoo.client import yahoo_get
from app.services.yahoo.parsers import (
    parse_scoreboard_min,
    select_matchup_for_team,
    parse_scoreboard_enriched,
)

# -------- tiny local helpers (avoid cycles) --------
def _get(d: Any, *keys) -> Any:
    cur = d
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur

def _as_list(x: Any) -> List:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]

def _normalize_league_id(lid: str | None) -> str | None:
    if not lid:
        return lid
    # Handle team keys like "465.l.34067.t.11" → "465.l.34067"
    if ".t." in lid:
        return lid.split(".t.", 1)[0]
    return lid

def _to_dict(node: Any) -> dict:
    """
    Recursively flatten Yahoo nodes:
    - Merge all dicts found at any depth inside lists-of-lists etc.
    - Last write wins for duplicate keys (Yahoo usually doesn't collide meaningful keys).
    This fixes shapes like team = [ [ {...}, {...} ], {"team_stats": ...}, {"team_points": ...} ]
    where the first element is itself a list of dicts.
    """
    out: dict = {}

    def rec(n: Any):
        if isinstance(n, dict):
            # merge keys
            for k, v in n.items():
                out[k] = v
        elif isinstance(n, list):
            for it in n:
                rec(it)
        # ignore scalars

    rec(node)
    return out

def _get_teams_from_matchup(m: dict) -> Optional[dict]:
    """
    Return the 'teams' dict from a matchup regardless of shape:
      - m['teams'] (flat)
      - m['0']['teams'] (nested under numeric key)
    """
    if isinstance(m, dict):
        if "teams" in m and isinstance(m["teams"], dict):
            return m["teams"]
        # Yahoo often nests the content under a numeric key "0"
        for k, v in m.items():
            if str(k).isdigit() and isinstance(v, dict) and isinstance(v.get("teams"), dict):
                return v["teams"]
    return None

def _team_name(team_obj: dict) -> Optional[str]:
    nm = team_obj.get("name")
    if isinstance(nm, dict):
        return nm.get("full") or nm.get("name")
    return nm if isinstance(nm, str) else None

def _iter_scoreboard_matchups(sb: dict):
    """
    Yield each 'matchup' dict from all known Yahoo shapes:
      - scoreboard["0"]["matchups"][*]["matchup"]
      - scoreboard["matchups"][*]["matchup"]
      - scoreboard["<digit>"]["matchup"]  (siblings 1..N)
    """
    # Case A: scoreboard["0"]["matchups"]
    if "0" in sb and isinstance(sb["0"], dict):
        inner = sb["0"]
        matchups = inner.get("matchups")
        if isinstance(matchups, dict):
            for mv in matchups.values():
                if isinstance(mv, dict):
                    m = mv.get("matchup")
                    if isinstance(m, (dict, list)):
                        yield m

    # Case B: scoreboard["matchups"]
    matchups2 = sb.get("matchups")
    if isinstance(matchups2, dict):
        for mv in matchups2.values():
            if isinstance(mv, dict):
                m = mv.get("matchup")
                if isinstance(m, (dict, list)):
                    yield m

    # Case C: sibling numeric entries: scoreboard["1"]["matchup"], ["2"]["matchup"], ...
    for k, v in sb.items():
        if str(k).isdigit() and isinstance(v, dict):
            m = v.get("matchup")
            if isinstance(m, (dict, list)):
                yield m

def _get_teams_from_matchup(m: dict) -> Optional[dict]:
    """
    Return the 'teams' dict from a matchup regardless of shape:
      - m['teams'] (flat)
      - m['0']['teams'] (nested under numeric key)
    """
    if isinstance(m, dict):
        if "teams" in m and isinstance(m["teams"], dict):
            return m["teams"]
        for k, v in m.items():
            if str(k).isdigit() and isinstance(v, dict) and isinstance(v.get("teams"), dict):
                return v["teams"]
    return None

def _stats_map_from_team_node(team_node: dict) -> dict[str, Any]:
    """
    Build {stat_id: value} from a flattened team node.
    Expects 'team_stats': {'stats': [{'stat': {'stat_id': '1','value':'...'}}, ...]}
    """
    t = _to_dict(team_node)
    out: dict[str, Any] = {}
    ts = t.get("team_stats")
    if isinstance(ts, dict):
        stats = ts.get("stats")
        if isinstance(stats, list):
            for it in stats:
                if isinstance(it, dict):
                    st = it.get("stat")
                    if isinstance(st, dict) and "stat_id" in st:
                        out[str(st["stat_id"])] = st.get("value")
    return out

def _team_points_from_team_node(team_node: dict) -> Optional[Any]:
    tp = _to_dict(team_node).get("team_points")
    if isinstance(tp, dict):
        total = tp.get("total")
        return total
    return None

def _iter_stat_winners(m: dict, sb: dict):
    """
    Yield winner objects from either per-matchup or scoreboard-level stat_winners.
    Each item yielded is a dict like {'stat_id': '1', 'winner_team_key': '...', 'is_tied': 1?}
    """
    def extract(lst):
        if isinstance(lst, list):
            for item in lst:
                if isinstance(item, dict):
                    sw = item.get("stat_winner")
                    if isinstance(sw, dict):
                        yield {
                            "stat_id": str(sw.get("stat_id")) if sw.get("stat_id") is not None else None,
                            "winner_team_key": sw.get("winner_team_key"),
                            "is_tied": bool(sw.get("is_tied")),
                        }

    # prefer per-matchup
    for it in extract(m.get("stat_winners")):
        yield it
    # fallback to scoreboard-level
    for it in extract(sb.get("stat_winners")):
        yield it

def _enrich_score_from_raw(sb_payload: dict, my_team_key: str, stat_map: dict[str, str], include_points: bool, include_categories: bool):
    """
    Build score object (points + categories) from the raw scoreboard structure.
    """
    fc = sb_payload.get("fantasy_content", {})
    league = fc.get("league")
    sb = None
    if isinstance(league, list) and len(league) >= 2 and isinstance(league[1], dict):
        sb = league[1].get("scoreboard")
    elif isinstance(league, dict):
        sb = league.get("scoreboard")
    if not isinstance(sb, dict):
        return None

    for matchup in _iter_scoreboard_matchups(sb):
        m = _to_dict(matchup)
        teams = _get_teams_from_matchup(m)
        if not isinstance(teams, dict):
            continue

        t0 = _to_dict(teams.get("0", {}).get("team"))
        t1 = _to_dict(teams.get("1", {}).get("team"))
        if not t0 or not t1:
            continue

        k0 = t0.get("team_key"); k1 = t1.get("team_key")
        if my_team_key not in (k0, k1):
            continue

        my_is_team1 = (my_team_key == k0)
        # --- points
        points_obj = None
        if include_points:
            p0 = _team_points_from_team_node(t0)
            p1 = _team_points_from_team_node(t1)
            points_obj = {"me": p0 if my_is_team1 else p1, "opp": p1 if my_is_team1 else p0}

        # --- categories
        category_breakdown = None
        cat_summary = None
        if include_categories:
            stats0 = _stats_map_from_team_node(t0)
            stats1 = _stats_map_from_team_node(t1)

            rows = []
            wins_me = losses_me = ties_me = 0
            for w in _iter_stat_winners(m, sb):
                sid = w.get("stat_id")
                if not sid:
                    continue
                name = stat_map.get(sid, sid)
                v0 = stats0.get(sid)
                v1 = stats1.get(sid)

                if w.get("is_tied"):
                    leader = 0
                    ties_me += 1
                else:
                    winner_key = w.get("winner_team_key")
                    leader = 1 if winner_key == k0 else 2
                    if my_is_team1:
                        if leader == 1: wins_me += 1
                        else: losses_me += 1
                    else:
                        if leader == 2: wins_me += 1
                        else: losses_me += 1

                # normalize leader relative to "me"
                leader_norm = leader if my_is_team1 else (2 if leader == 1 else (1 if leader == 2 else 0))

                rows.append({
                    "name": name,
                    "me": v0 if my_is_team1 else v1,
                    "opp": v1 if my_is_team1 else v0,
                    "leader": leader_norm,
                })

            category_breakdown = rows
            cat_summary = {"wins": wins_me, "losses": losses_me, "ties": ties_me}

        return {
            "points": points_obj,
            "categories": cat_summary,
            "category_breakdown": category_breakdown,
        }
    return None


# ----------------------------- GUID / team discovery -----------------------------

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
    """
    Rock-solid lookup via /users;use_login=1/teams and match league key.
    """
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


def _find_my_team_key_from_teams_payload(teams_payload: dict, my_guid: str | None = None) -> tuple[str | None, str | None]:
    """
    Fallback: scan /league/<lid>/teams or /leagues;league_keys=<lid>/teams payload.
    """
    fc = teams_payload.get("fantasy_content", {})
    found_key = None
    found_name = None

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

    def normalize_team_node(node: dict | list) -> dict:
        return _to_dict(node)

    def walk(node):
        nonlocal found_key, found_name
        if found_key:
            return
        if isinstance(node, dict):
            if "team" in node:
                t = normalize_team_node(node["team"])
                if t and is_me_team(t):
                    found_key = t.get("team_key")
                    found_name = _team_name(t)
                    return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(fc)
    return (found_key, found_name)


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

    for matchup in _iter_scoreboard_matchups(sb):
        mm = _to_dict(matchup)
        teams = _get_teams_from_matchup(mm)
        if not isinstance(teams, dict):
            continue
        for tk in ("0", "1"):
            t = teams.get(tk, {}).get("team")
            if not t:
                continue
            t = _to_dict(t)
            if is_me_team(t):
                return (t.get("team_key"), _team_name(t))

    return (None, None)


# ----------------------------- raw fallback over Yahoo scoreboard -----------------------------

def _extract_matchup_from_scoreboard_raw(sb_payload: dict, my_team_key: str) -> dict | None:
    fc = sb_payload.get("fantasy_content", {})
    league = fc.get("league")
    sb = None
    if isinstance(league, list) and len(league) >= 2 and isinstance(league[1], dict):
        sb = league[1].get("scoreboard")
    elif isinstance(league, dict):
        sb = league.get("scoreboard")
    if not isinstance(sb, dict):
        return None

    for matchup in _iter_scoreboard_matchups(sb):
        m = _to_dict(matchup)
        teams = _get_teams_from_matchup(m)
        if not isinstance(teams, dict):
            continue

        t0 = _to_dict(teams.get("0", {}).get("team"))
        t1 = _to_dict(teams.get("1", {}).get("team"))
        if not t0 or not t1:
            continue

        k0 = t0.get("team_key"); k1 = t1.get("team_key")
        if my_team_key not in (k0, k1):
            continue

        # metadata (prefer per-matchup, fallback to scoreboard)
        week_no = m.get("week") or sb.get("week")
        status = m.get("status") or sb.get("status")
        start_date = m.get("week_start") or sb.get("week_start")
        end_date = m.get("week_end") or sb.get("week_end")
        is_playoffs = bool(int(m.get("is_playoffs", sb.get("is_playoffs", "0"))))

        opp = t1 if my_team_key == k0 else t0

        return {
            "week": week_no,
            "start_date": start_date,
            "end_date": end_date,
            "status": status,
            "is_playoffs": is_playoffs,
            "team1_key": k0,
            "team1_name": _team_name(t0),
            "team2_key": k1,
            "team2_name": _team_name(t1),
            "my_key": my_team_key,
            "opp_key": opp.get("team_key"),
            "opp_name": _team_name(opp),
        }
    return None


# ----------------------------- league metadata / stat map -----------------------------

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

# ----------------------------- main API -----------------------------

def get_my_weekly_matchups(
    db: Session,
    user_id: str,
    *,
    week: int | None = None,
    sport: str | None = None,      # kept for signature parity
    season: int | None = None,     # kept for signature parity
    league_id: str | None = None,
    include_categories: bool = False,
    include_points: bool = True,
    limit: int | None = None,      # kept for signature parity
    debug: bool = False,           # optional diagnostics
) -> dict:
    """
    Return YOUR matchup(s). If league_id is provided, returns one item for that league;
    otherwise iterates your recent leagues (keeping signature parity with previous API).
    """
    items: List[dict] = []
    diag = [] if debug else None

    # --- normalize league_id if a team key was passed (e.g., 465.l.34067.t.11 → 465.l.34067)
    orig_league_id = league_id
    league_id = _normalize_league_id(league_id)
    if debug and orig_league_id and orig_league_id != league_id:
        diag.append({
            "stage": "normalize_league_id",
            "input": orig_league_id,
            "normalized": league_id
        })

    # Resolve leagues list
    if league_id:
        try:
            meta = _get_league_settings_meta(db, user_id, league_id)
            league_list = [{
                "id": league_id,
                "name": meta.get("league_name"),
                "season": meta.get("season"),
                "sport": meta.get("sport"),
            }]
        except Exception as e:
            league_list = [{"id": league_id, "name": None, "season": None, "sport": None}]
            if debug: diag.append({"stage": "league_meta_error", "error": str(e)})
    else:
        # local import to avoid circular
        from app.services.yahoo.leagues import get_leagues
        league_list = get_leagues(db, user_id, sport=sport, season=season)
        if limit:
            league_list = league_list[:limit]

    requested_week = week
    my_guid = _get_my_guid(db, user_id)
    if debug: diag.append({"stage": "guid", "my_guid": my_guid})

    for L in league_list:
        lid = L.get("id")
        if not lid:
            continue

        # Determine week early using league settings, and validate requested_week
        meta = _get_league_settings_meta(db, user_id, lid)
        weeks_meta = {int(w["week"]) for w in meta.get("weeks", []) if isinstance(w, dict) and w.get("week") is not None}
        use_week = requested_week or meta.get("current_week")
        if requested_week is not None and weeks_meta and requested_week not in weeks_meta:
            if debug:
                diag.append({"stage": "week_not_in_schedule", "league": lid, "requested_week": requested_week, "fallback_to": meta.get("current_week")})
            use_week = meta.get("current_week")
        week_part = f";week={use_week}" if use_week else ""

        # 1) exact via /users;use_login=1/teams
        my_team_key, my_team_name = _get_my_team_key_for_league(db, user_id, lid)

        # 2) fallback via teams payload(s)
        if not my_team_key:
            teams_payload = yahoo_get(db, user_id, f"/league/{lid}/teams")
            my_team_key, my_team_name = _find_my_team_key_from_teams_payload(teams_payload, my_guid)
            if debug and not my_team_key:
                diag.append({"stage":"teams_payload_scan_failed","league":lid})

            if not my_team_key:
                teams_payload2 = yahoo_get(db, user_id, f"/leagues;league_keys={lid}/teams")
                my_team_key, my_team_name = _find_my_team_key_from_teams_payload(teams_payload2, my_guid)
                if debug and not my_team_key:
                    diag.append({"stage":"plural_teams_payload_scan_failed","league":lid})

        # 3) last resort via scoreboard payload(s)
        if not my_team_key:
            sb_try = yahoo_get(db, user_id, f"/league/{lid}/scoreboard{week_part}")
            my_team_key, my_team_name = _find_my_team_key_from_scoreboard_payload(sb_try, my_guid)
            if debug and not my_team_key:
                diag.append({"stage":"scoreboard_scan_failed","league":lid,"week":use_week})

            if not my_team_key:
                sb_try2 = yahoo_get(db, user_id, f"/leagues;league_keys={lid}/scoreboard{week_part}")
                my_team_key, my_team_name = _find_my_team_key_from_scoreboard_payload(sb_try2, my_guid)
                if debug and not my_team_key:
                    diag.append({"stage":"plural_scoreboard_scan_failed","league":lid,"week":use_week})

        if not my_team_key:
            # can't identify your team → skip
            if debug:
                diag.append({
                    "stage": "no_my_team_in_league",
                    "league": lid,
                    "hint": "Check the token/account belongs to this league."
                })
            continue

        if debug:
            diag.append({"stage":"team_key_found","league":lid,"team_key":my_team_key,"team_name":my_team_name})

        # Fetch scoreboard & select your matchup (min parse)
        sb_payload = yahoo_get(db, user_id, f"/league/{lid}/scoreboard{week_part}")
        sb_min = parse_scoreboard_min(sb_payload)

        chosen_min = None
        if sb_min.get("matchups"):
            m = select_matchup_for_team(sb_min, my_team_key)
            if m:
                chosen_min = m
        else:
            # try plural endpoint for min parse
            sb_payload2 = yahoo_get(db, user_id, f"/leagues;league_keys={lid}/scoreboard{week_part}")
            sb_min2 = parse_scoreboard_min(sb_payload2)
            if sb_min2.get("matchups"):
                m2 = select_matchup_for_team(sb_min2, my_team_key)
                if m2:
                    chosen_min = m2

        # If min parse didn't find it, walk the raw payload structure (all shapes)
        opp_key = opp_name = status = None
        is_playoffs = False
        sb_min_week = sb_min.get("week") or use_week
        start_date = sb_min.get("start_date"); end_date = sb_min.get("end_date")

        if not chosen_min:
            raw_pick = _extract_matchup_from_scoreboard_raw(sb_payload, my_team_key)
            if not raw_pick:
                # try plural raw
                sb_payload2 = yahoo_get(db, user_id, f"/leagues;league_keys={lid}/scoreboard{week_part}")
                raw_pick = _extract_matchup_from_scoreboard_raw(sb_payload2, my_team_key)

            if not raw_pick:
                if debug:
                    diag.append({"stage":"select_matchup_none", "league":lid, "team_key":my_team_key, "use_week":use_week})
                continue

            opp_key, opp_name = raw_pick["opp_key"], raw_pick["opp_name"]
            status = raw_pick["status"]; is_playoffs = raw_pick["is_playoffs"]
            sb_min_week = raw_pick["week"] or sb_min_week
            start_date = raw_pick["start_date"] or start_date
            end_date = raw_pick["end_date"] or end_date
        else:
            # Opponent from min object
            if chosen_min["team1_key"] == my_team_key:
                opp_key, opp_name = chosen_min.get("team2_key"), chosen_min.get("team2_name")
            else:
                opp_key, opp_name = chosen_min.get("team1_key"), chosen_min.get("team1_name")
            status = chosen_min.get("status")
            is_playoffs = chosen_min.get("is_playoffs")
            # week/start/end already from sb_min

        score_obj = None
        if include_categories or include_points:
            # try normal enriched parser first
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
            else:
                # fallback: compute from raw scoreboard (handles nesting quirks)
                stat_map = _get_stat_id_map(db, user_id, lid) if include_categories else {}
                score_obj = _enrich_score_from_raw(sb_payload, my_team_key, stat_map, include_points, include_categories)
                if not score_obj:
                    # one more try using plural endpoint raw
                    sb_payload2 = yahoo_get(db, user_id, f"/leagues;league_keys={lid}/scoreboard{week_part}")
                    score_obj = _enrich_score_from_raw(sb_payload2, my_team_key, stat_map, include_points, include_categories)

        items.append({
            "league_id": lid,
            "league_name": L.get("name") or meta.get("league_name"),
            "season": L.get("season") or meta.get("season"),
            "sport": L.get("sport") or meta.get("sport"),
            "week": sb_min_week or use_week,
            "start_date": start_date,
            "end_date": end_date,
            "team_id": my_team_key,
            "team_name": my_team_name,
            "opponent_team_id": opp_key,
            "opponent_team_name": opp_name,
            "status": status,
            "is_playoffs": is_playoffs,
            "score": score_obj,
        })

    result = {"user_id": user_id, "week": requested_week, "items": items}
    if debug:
        result["debug"] = diag
    return result
