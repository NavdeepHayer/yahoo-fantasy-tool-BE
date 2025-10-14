from typing import Any, List, Tuple, Optional
from sqlalchemy.orm import Session
from app.db.models import User
from app.core.config import settings

# Public OAuth functions (re-exported for compatibility)
from app.services.yahoo_oauth import (
    AUTH_SCOPE,
    build_oauth,
    get_authorization_url,
    exchange_token,
    get_latest_token,
    refresh_token,
)

# Low-level HTTP client
from app.services.yahoo_client import yahoo_get

# Pure parsers (import and re-export underscored aliases for debug compatibility)
from app.services.yahoo_parsers import (
    parse_leagues,
    parse_teams,
    parse_roster,
    parse_scoreboard_min,
    select_matchup_for_team,
    parse_scoreboard_enriched,  # NEW
)

# Back-compat: some debug routes import these with underscores
_parse_leagues = parse_leagues       # noqa
_parse_teams = parse_teams           # noqa
_parse_roster = parse_roster         # noqa


# ---- Public service API (same signatures/behavior as before) ----
def _fetch_league_settings(db: Session, user_id: str, league_keys: List[str]) -> dict[str, List[str]]:
    if not league_keys:
        return {}

    keys_param = ",".join(league_keys)
    payload = yahoo_get(db, user_id, f"/leagues;league_keys={keys_param}/settings")
    fc = payload.get("fantasy_content", {})
    leagues_node = fc.get("leagues")

    out: dict[str, List[str]] = {}

    if isinstance(leagues_node, dict):
        for k, v in leagues_node.items():
            if not str(k).isdigit() or not isinstance(v, dict):
                continue
            league_list = v.get("league")
            if not isinstance(league_list, list) or len(league_list) < 2:
                continue

            league_fields = league_list[0] if isinstance(league_list[0], dict) else {}
            settings_wrapper = league_list[1] if isinstance(league_list[1], dict) else {}

            league_key = league_fields.get("league_key") or league_fields.get("league_id")
            if not league_key:
                continue

            settings_list = settings_wrapper.get("settings")
            if not (isinstance(settings_list, list) and settings_list and isinstance(settings_list[0], dict)):
                continue
            settings_obj = settings_list[0]

            cats: List[str] = []
            stats_arr = settings_obj.get("stat_categories", {}).get("stats")
            if isinstance(stats_arr, list):
                for item in stats_arr:
                    if isinstance(item, dict):
                        stat = item.get("stat", {})
                        dn = stat.get("display_name") or stat.get("name")
                        if dn:
                            cats.append(str(dn))

            out[str(league_key)] = cats

    return out


def get_leagues(
    db: Session,
    user_id: str,
    sport: Optional[str] = None,    # "nba" | "mlb" | "nhl" | "nfl"
    season: Optional[int] = None,   # e.g., 2025
    game_key: Optional[str] = None, # explicit Yahoo game_key like "466"
) -> List[dict]:
    if settings.YAHOO_FAKE_MODE:
        return [{
            "id": "123.l.4567",
            "name": "Nav’s H2H",
            "season": "2024",
            "scoring_type": "h2h",
            "categories": ["PTS", "REB", "AST", "3PTM", "ST", "BLK", "FG%", "FT%"],
        }]

    def _leagues_for_keys(keys: List[str]) -> List[dict]:
        if not keys:
            return []
        payload = yahoo_get(db, user_id, f"/users;use_login=1/games;game_keys={','.join(keys)}/leagues")
        return parse_leagues(payload)

    keys: List[str] = []

    if game_key:
        keys = [game_key]
    else:
        games_payload = yahoo_get(db, user_id, "/users;use_login=1/games")
        fc = games_payload.get("fantasy_content", {})
        user_variants = _as_list(_get(fc, "users", "0", "user"))
        games_node = None
        for item in user_variants:
            if isinstance(item, dict) and "games" in item:
                games_node = item.get("games")
                break
        if not isinstance(games_node, dict):
            return []

        entries: List[Tuple[int, str, str]] = []
        for k, v in games_node.items():
            if not str(k).isdigit() or not isinstance(v, dict):
                continue
            gitems = v.get("game")
            if isinstance(gitems, dict):
                gitems = [gitems]
            if not isinstance(gitems, list):
                continue
            for g in gitems:
                if not isinstance(g, dict):
                    continue
                code = (g.get("code") or "").lower()
                gk = g.get("game_key")
                seas = g.get("season")
                try:
                    seas_int = int(seas) if seas and str(seas).isdigit() else 0
                except Exception:
                    seas_int = 0
                if gk:
                    entries.append((seas_int, code, str(gk)))

        if sport:
            sport_l = sport.lower().strip()
            entries = [e for e in entries if e[1] == sport_l]
        if season is not None:
            try:
                s = int(season)
                entries = [e for e in entries if e[0] == s]
            except Exception:
                pass

        entries.sort(key=lambda t: t[0], reverse=True)
        seen: set[str] = set()
        for _, _, gk in entries:
            if gk in seen:
                continue
            seen.add(gk)
            keys.append(gk)
            if len(keys) >= 6:
                break

    leagues = _leagues_for_keys(keys)

    if leagues:
        BATCH = 10
        for i in range(0, len(leagues), BATCH):
            chunk = leagues[i:i+BATCH]
            mapping = _fetch_league_settings(db, user_id, [L["id"] for L in chunk if "id" in L])
            for L in chunk:
                if L.get("id") in mapping:
                    L["categories"] = mapping[L["id"]]

    return leagues

from typing import List
from sqlalchemy.orm import Session
from app.core.config import settings
from app.services.yahoo_client import yahoo_get  # <- use your low-level client directly

def get_teams_for_user(db: Session, user_id: str, league_id: str) -> List[dict]:
    """
    Return a simplified list of teams for a league:
    [
      {"id": "<league>.t.<id>", "name": "<team name>", "manager": "<GUID or nickname>", "manager_name": "<nickname>"},
      ...
    ]

    Robust against Yahoo's nested list/dict structure and the 'count' int.
    """
    if settings.YAHOO_FAKE_MODE:
        return [
            {"id": f"{league_id}.t.1", "name": "Nav’s Team", "manager": "Nav", "manager_name": "Nav"},
            {"id": f"{league_id}.t.2", "name": "Rival Squad", "manager": "Alex", "manager_name": "Alex"},
        ]

    payload = yahoo_get(db, user_id, f"/league/{league_id}/teams")

    # Locate the "teams" container regardless of list/dict shape
    fc = payload.get("fantasy_content", {})
    league_node = fc.get("league", {})
    if isinstance(league_node, list):
        teams_container = league_node[1].get("teams", {}) if len(league_node) >= 2 and isinstance(league_node[1], dict) else {}
    elif isinstance(league_node, dict):
        teams_container = league_node.get("teams", {})
    else:
        teams_container = {}

    out: List[dict] = []

    # Iterate numeric keys; skip "count" or any non-dict values
    for k, v in teams_container.items():
        if not str(k).isdigit():
            continue
        if not isinstance(v, dict):
            continue

        team_block = v.get("team")
        if team_block is None:
            continue

        # Normalize the team structure into a single flat dict ("agg")
        agg = {}

        def merge_dict(d: dict):
            for kk, vv in d.items():
                agg[kk] = vv

        if isinstance(team_block, dict):
            merge_dict(team_block)
        elif isinstance(team_block, list):
            # Common shape: [ [ {field},{field},[],...,{managers:[{manager:{...}}]} ] ]
            first = team_block[0] if team_block else None
            if isinstance(first, list):
                for part in first:
                    if isinstance(part, dict):
                        merge_dict(part)
            else:
                # Sometimes it's just a list of dicts
                for part in team_block:
                    if isinstance(part, dict):
                        merge_dict(part)

        # Extract team_key and name
        team_key = agg.get("team_key")
        name = agg.get("name")
        if isinstance(name, dict):
            name = name.get("full") or name.get("name")

        # Extract manager guid + nickname
        manager_guid = None
        manager_name = None
        managers = agg.get("managers")
        if isinstance(managers, list) and managers:
            m = managers[0].get("manager", {}) if isinstance(managers[0], dict) else {}
            manager_guid = m.get("guid")
            manager_name = m.get("nickname") or m.get("name")
        elif isinstance(managers, dict):
            # Numeric dict style
            for kk, vv in managers.items():
                if str(kk).isdigit() and isinstance(vv, dict):
                    m = vv.get("manager", {})
                    if isinstance(m, dict):
                        manager_guid = m.get("guid") or manager_guid
                        manager_name = m.get("nickname") or manager_name

        out.append({
            "id": team_key,
            "name": name,
            "manager": manager_guid or manager_name,  # prefer GUID when present
            "manager_name": manager_name,
        })

    return out



def get_roster_for_user(db: Session, user_id: str, team_id: str, date: Optional[str] = None) -> dict:
    if settings.YAHOO_FAKE_MODE:
        return {
            "team_id": team_id,
            "date": date or "2025-10-10",
            "players": [
                {"player_id": "nba.p.201939", "name": "Stephen Curry", "positions": ["PG"], "status": "ACTIVE"},
                {"player_id": "nba.p.2544", "name": "LeBron James", "positions": ["SF", "PF"], "status": "BN"},
            ],
        }

    date_part = f";date={date}" if date else ""

    payload = yahoo_get(db, user_id, f"/team/{team_id}/roster{date_part}")
    r_date, players = parse_roster(payload, team_id)
    if players:
        return {"team_id": team_id, "date": r_date or (date or ""), "players": players}

    payload2 = yahoo_get(db, user_id, f"/teams;team_keys={team_id}/roster{date_part}")
    r_date2, players2 = parse_roster(payload2, team_id)
    return {"team_id": team_id, "date": r_date2 or (date or ""), "players": players2}


# ---- Debug helper (used by /debug/yahoo/raw) ----
def yahoo_raw_get(db: Session, user_id: str, path: str, params: Optional[dict] = None) -> dict:
    return yahoo_get(db, user_id, path, params or {})


# ---- tiny local helpers reused by get_leagues (copied here to avoid circular import) ----
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


# ---- Identify “my team” helpers ----
def _find_my_team_key_from_teams_payload(teams_payload: dict, my_guid: str | None = None) -> tuple[str | None, str | None]:
    """
    Scan /league/<league_id>/teams payload and return (team_key, team_name) for the authed user.

    Matches any of:
      - is_current_login == "1"
      - is_owned_by_current_login == "1"
      - managers[].manager.guid == my_guid  (when provided)
    """
    fc = teams_payload.get("fantasy_content", {})
    found_key = None
    found_name = None

    def norm_name(team_obj: dict) -> str | None:
        nm = team_obj.get("name")
        if isinstance(nm, str):
            return nm
        if isinstance(nm, dict):
            return nm.get("full") or nm.get("name")
        return None

    def is_me_team(team_obj: dict) -> bool:
        # flags first
        if str(team_obj.get("is_current_login", "0")) == "1":
            return True
        if str(team_obj.get("is_owned_by_current_login", "0")) == "1":
            return True
        # match manager guid
        if my_guid:
            mgrs = team_obj.get("managers")
            # list style
            if isinstance(mgrs, list):
                for it in mgrs:
                    if isinstance(it, dict):
                        m = it.get("manager")
                        if isinstance(m, dict) and m.get("guid") == my_guid:
                            return True
            # numeric dict style
            if isinstance(mgrs, dict):
                for k, v in mgrs.items():
                    if str(k).isdigit() and isinstance(v, dict):
                        m = v.get("manager")
                        if isinstance(m, dict) and m.get("guid") == my_guid:
                            return True
        return False

    def normalize_team_node(node: dict | list) -> dict:
        if isinstance(node, list):
            agg = {}
            for part in node:
                if isinstance(part, dict):
                    agg.update(part)
            return agg
        return node if isinstance(node, dict) else {}

    def walk(node):
        nonlocal found_key, found_name
        if found_key:
            return
        if isinstance(node, dict):
            if "team" in node:
                t = normalize_team_node(node["team"])
                if t and is_me_team(t):
                    found_key = t.get("team_key")
                    found_name = norm_name(t)
                    return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(fc)
    return (found_key, found_name)


def _get_my_team_key_for_league(db: Session, user_id: str, league_id: str) -> tuple[str | None, str | None]:
    """
    Rock-solid way to find the user's team key for a given league by calling:
      GET /users;use_login=1/teams
    Then filter by league_key.
    Returns (team_key, team_name) or (None, None).
    """
    payload = yahoo_get(db, user_id, "/users;use_login=1/teams")
    fc = payload.get("fantasy_content", {})
    users_node = fc.get("users")
    if not isinstance(users_node, dict):
        return (None, None)

    # Iterate users → teams
    user_list = _as_list(_get(users_node, "0", "user"))
    for user in user_list:
        teams_node = _get(user, "teams")
        if not isinstance(teams_node, dict):
            continue
        for k, v in teams_node.items():
            if not str(k).isdigit() or not isinstance(v, dict):
                continue
            team_obj = v.get("team")
            if isinstance(team_obj, list):
                # Merge the fragments
                agg = {}
                for part in team_obj:
                    if isinstance(part, dict):
                        agg.update(part)
                team_obj = agg
            if not isinstance(team_obj, dict):
                continue

            t_key = team_obj.get("team_key")
            # team leagues often embeds "league_key" or "league" fragment
            t_league_key = team_obj.get("league_key")
            if not t_league_key:
                # Sometimes it's nested
                lg = team_obj.get("league")
                if isinstance(lg, dict):
                    t_league_key = lg.get("league_key")

            if t_key and t_league_key and str(t_league_key) == str(league_id):
                # name can be string or an object with "full"
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
    """
    Aggregate 'my' matchup across leagues for a given (or derived) week.
    Now first resolves your team via /users;use_login=1/teams (rock-solid),
    then falls back to scanning /league/<league>/teams for is_current_login=1.
    """
    items: List[dict] = []

    # Resolve candidate leagues
    league_list: List[dict] = []
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
        league_list = get_leagues(db, user_id, sport=sport, season=season)
        if limit:
            league_list = league_list[:limit]

    requested_week = week
    my_guid = _get_my_guid(db, user_id)  # fetch once

    for L in league_list:
        lid = L.get("id")
        if not lid:
            continue

        # 1) Try via /users;use_login=1/teams → exact league match
        my_team_key, my_team_name = _get_my_team_key_for_league(db, user_id, lid)

        # 2) Fallback → /league/{lid}/teams (use flags + GUID)
        if not my_team_key:
            teams_payload = yahoo_get(db, user_id, f"/league/{lid}/teams")
            my_team_key, my_team_name = _find_my_team_key_from_teams_payload(teams_payload, my_guid)
            if not my_team_key:
                teams_payload2 = yahoo_get(db, user_id, f"/leagues;league_keys={lid}/teams")
                my_team_key, my_team_name = _find_my_team_key_from_teams_payload(teams_payload2, my_guid)

        # 3) Last-resort → infer from scoreboard payload itself (optional, if you already added that helper)
        if not my_team_key:
            week_part = f";week={requested_week}" if requested_week else ""
            sb_try = yahoo_get(db, user_id, f"/league/{lid}/scoreboard{week_part}")
            my_team_key, my_team_name = _find_my_team_key_from_scoreboard_payload(sb_try, my_guid)
            if not my_team_key:
                sb_try2 = yahoo_get(db, user_id, f"/leagues;league_keys={lid}/scoreboard{week_part}")
                my_team_key, my_team_name = _find_my_team_key_from_scoreboard_payload(sb_try2, my_guid)

        if not my_team_key:
            # still can't identify—skip this league
            continue

        # Determine week
        meta = _get_league_settings_meta(db, user_id, lid)
        use_week = requested_week or meta.get("current_week")
        week_part = f";week={use_week}" if use_week else ""

        # Fetch basic scoreboard and select my matchup
        sb_payload = yahoo_get(db, user_id, f"/league/{lid}/scoreboard{week_part}")
        sb_min = parse_scoreboard_min(sb_payload)
        if not sb_min.get("matchups"):
            sb_payload2 = yahoo_get(db, user_id, f"/leagues;league_keys={lid}/scoreboard{week_part}")
            sb_min = parse_scoreboard_min(sb_payload2)

        m = select_matchup_for_team(sb_min, my_team_key)
        if not m:
            continue

        # Opponent
        if m["team1_key"] == my_team_key:
            opp_key, opp_name = m.get("team2_key"), m.get("team2_name")
        else:
            opp_key, opp_name = m.get("team1_key"), m.get("team1_name")

        score_obj = None
        if include_categories or include_points:
            # Try enriched parse on same payload (and plural fallback)
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
                            if leader == 1:
                                wins_me += 1
                            else:
                                losses_me += 1
                        else:
                            if leader == 2:
                                wins_me += 1
                            else:
                                losses_me += 1

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

# --- NEW: get my Yahoo GUID (once, cheap) ---
def _get_my_guid(db: Session, user_id: str) -> str | None:
    """
    Fetch authed user's GUID via /users;use_login=1.
    """
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
                r


# --- UPDATED: allow passing my_guid; also check managers.guid & is_owned_by_current_login ---
def _find_my_team_key_from_teams_payload(teams_payload: dict, my_guid: str | None = None) -> tuple[str | None, str | None]:
    """
    Scan /league/<league_id>/teams payload and return (team_key, team_name) for the authed user.

    We accept a my_guid to match against managers[].manager.guid.
    We also honor is_current_login==1 and is_owned_by_current_login==1 if present.
    """
    fc = teams_payload.get("fantasy_content", {})
    found_key = None
    found_name = None

    def name_of(team_obj: dict) -> str | None:
        nm = team_obj.get("name")
        if isinstance(nm, str):
            return nm
        if isinstance(nm, dict):
            return nm.get("full") or nm.get("name")
        return None

    def is_me_team(team_obj: dict) -> bool:
        # direct flags
        if str(team_obj.get("is_current_login", "0")) == "1":
            return True
        if str(team_obj.get("is_owned_by_current_login", "0")) == "1":
            return True
        # managers guid match
        if my_guid:
            mgrs = team_obj.get("managers")
            # list style
            if isinstance(mgrs, list):
                for it in mgrs:
                    if isinstance(it, dict):
                        m = it.get("manager")
                        if isinstance(m, dict) and m.get("guid") == my_guid:
                            return True
            # numeric dict style
            if isinstance(mgrs, dict):
                for k, v in mgrs.items():
                    if str(k).isdigit() and isinstance(v, dict):
                        m = v.get("manager")
                        if isinstance(m, dict) and m.get("guid") == my_guid:
                            return True
        return False

    def walk(node: Any):
        nonlocal found_key, found_name
        if found_key:
            return
        if isinstance(node, dict):
            t = node.get("team")
            if t is not None:
                # normalize
                if isinstance(t, list):
                    agg = {}
                    for part in t:
                        if isinstance(part, dict):
                            agg.update(part)
                    t = agg
                if isinstance(t, dict) and is_me_team(t):
                    found_key = t.get("team_key")
                    found_name = name_of(t)
                    return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for itm in node:
                walk(itm)

    walk(fc)
    return (found_key, found_name)


# --- NEW: last-resort identify from scoreboard payload itself ---
def _find_my_team_key_from_scoreboard_payload(scoreboard_payload: dict, my_guid: str | None = None) -> tuple[str | None, str | None]:
    """
    Look inside /league/<league_id>/scoreboard;week=N payload and try to identify the authed user's team
    by flags or manager.guid.
    Returns (team_key, team_name) or (None, None).
    """
    fc = scoreboard_payload.get("fantasy_content", {})
    league = fc.get("league")
    # scoreboard usually in league[1]["scoreboard"]["0"]["matchups"]
    sb = None
    if isinstance(league, list) and len(league) >= 2 and isinstance(league[1], dict):
        sb = league[1].get("scoreboard")
    elif isinstance(league, dict):
        sb = league.get("scoreboard")
    if not isinstance(sb, dict):
        return (None, None)

    matchups = None
    # typical nesting: sb -> "0" -> { "matchups": {...} }
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
        # teams["0"]["team"], teams["1"]["team"]
        for tk in ("0", "1"):
            t = teams.get(tk, {}).get("team")
            if t is None:
                continue
            t = normalize_team(t)
            if is_me_team(t):
                # extract name
                nm = t.get("name")
                if isinstance(nm, dict):
                    nm = nm.get("full") or nm.get("name")
                return (t.get("team_key"), nm if isinstance(nm, str) else None)
    return (None, None)

def get_current_user_profile(db: Session) -> dict:
    """
    Fetch the logged-in Yahoo user's profile via Yahoo Fantasy API.
    Returns: {"guid": str, "nickname": str|None, "image_url": str|None}
    """
    # The 'users;use_login=1' endpoint returns the current account
    raw = yahoo_raw_get("users;use_login=1")
    # Defensive parse across Yahoo's odd shapes
    # Expect something like: {"fantasy_content":{"users":{"0":{"user":[{...}]}}}}
    fc = raw.get("fantasy_content", {})
    users = fc.get("users", {})
    user0 = (users.get("0", {}) or {}).get("user", [{}])[0] if isinstance(users, dict) else {}
    guid = user0.get("guid")
    prof = user0.get("profile", {}) if isinstance(user0, dict) else {}
    nickname = prof.get("nickname")
    image_url = prof.get("image_url") or prof.get("image_url_small")

    if not guid:
        raise RuntimeError("Could not parse Yahoo user GUID from /users;use_login=1")

    # Upsert into our users table
    existing = db.get(User, guid)
    if existing:
        existing.nickname = nickname or existing.nickname
        existing.image_url = image_url or existing.image_url
        db.add(existing)
    else:
        db.add(User(guid=guid, nickname=nickname, image_url=image_url))
    db.commit()

    return {"guid": guid, "nickname": nickname, "image_url": image_url}