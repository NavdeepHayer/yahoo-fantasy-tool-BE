from __future__ import annotations
from typing import Any, List, Tuple, Optional

# ---------------- Small utils ----------------
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

def _coalesce_str(*vals):
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None

def _deep_first_position(val) -> str | None:
    """
    Recursively search dict/list nodes for a string under common keys ONLY:
    'position', 'abbr', 'pos', 'display_position'. Do NOT return arbitrary strings
    (e.g., 'date' from coverage_type), only those found under the keys above.
    """
    KEYS = ("position", "abbr", "pos", "display_position")

    # If the value is already a string, we only accept it if the caller passed it
    # directly (we won't hit this path during 'scan other keys' — see below).
    if isinstance(val, str) and val.strip():
        return val.strip()

    if isinstance(val, dict):
        # 1) Look at our known keys first
        for k in KEYS:
            v = val.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, (dict, list)):
                got = _deep_first_position(v)
                if got:
                    return got
        # 2) Recurse ONLY into child dict/list values (avoid returning raw strings
        # like "date" from coverage_type/date blocks)
        for v in val.values():
            if isinstance(v, (dict, list)):
                got = _deep_first_position(v)
                if got:
                    return got
        return None

    if isinstance(val, list):
        for item in val:
            got = _deep_first_position(item)
            if got:
                return got
        return None

    return None

def _deep_find_any(node, keys=("selected_position", "selected_positions", "selected_position_list",
                                "roster_position", "current_position", "assigned_slot", "slot")):
    """
    Recursively find the first value under any of the provided keys.
    Returns the value (can be str|dict|list) or None.
    """
    if isinstance(node, dict):
        # direct hit
        for k in keys:
            if k in node:
                return node[k]
        # scan deeper
        for v in node.values():
            found = _deep_find_any(v, keys)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _deep_find_any(item, keys)
            if found is not None:
                return found
    return None

def _extract_selected_slot(container: dict, flat_player: dict | None = None) -> str | None:
    """
    Ultra-robust slot extractor. Looks for aliases anywhere on the roster item
    (container and flattened player node), then resolves to a displayable slot.
    """
    # 1) simple aliases if they are strings
    direct = _coalesce_str(
        container.get("slot"),
        container.get("roster_position"),
        container.get("current_position"),
        container.get("assigned_slot"),
    ) or (flat_player and _coalesce_str(
        flat_player.get("slot"),
        flat_player.get("roster_position"),
        flat_player.get("current_position"),
        flat_player.get("assigned_slot"),
    ))
    if direct:
        return direct.upper()

    # 2) deep search for any of the keys (including selected_position-tree)
    cand = _deep_find_any(container) or (flat_player and _deep_find_any(flat_player))
    if cand is None:
        return None

    # If the candidate is a string, use it. If object/list, dive for first 'position'/'abbr'/etc
    pos = _deep_first_position(cand) if not isinstance(cand, str) else cand
    return pos.upper() if isinstance(pos, str) and pos.strip() else None

# ---------------- Leagues ----------------
def parse_leagues(payload: dict) -> List[dict]:
    """Parse leagues whether Yahoo nests under users→games or at top-level, and scan all indices (0..count-1)."""
    fc = payload.get("fantasy_content", {})
    out: List[dict] = []

    def _collect_league_dicts(leagues_node: Any) -> List[dict]:
        leagues_flat: List[dict] = []
        if isinstance(leagues_node, dict):
            for k, v in leagues_node.items():
                if str(k).isdigit() and isinstance(v, dict):
                    items = v.get("league")
                    if isinstance(items, dict):
                        leagues_flat.append(items)
                    elif isinstance(items, list):
                        leagues_flat.extend([i for i in items if isinstance(i, dict)])
        elif isinstance(leagues_node, list):
            leagues_flat.extend([i for i in leagues_node if isinstance(i, dict)])
        return leagues_flat

    def _extract_from_leagues(leagues_node: Any):
        for L in _collect_league_dicts(leagues_node):
            league_id = _get(L, "league_key") or _get(L, "league_id")
            name = _get(L, "name")
            season = _get(L, "season")
            scoring_type = _get(L, "scoring_type") or _get(L, "settings", "scoring_type")
            cats: List[str] = []
            stats = _as_list(_get(L, "settings", "stat_categories", "stats", "stat"))
            for s in stats:
                if isinstance(s, dict):
                    dn = s.get("display_name") or s.get("name")
                    if dn:
                        cats.append(dn)
            if league_id and name:
                out.append({
                    "id": str(league_id),
                    "name": str(name),
                    "season": str(season) if season is not None else "",
                    "scoring_type": str(scoring_type) if scoring_type is not None else "",
                    "categories": cats,
                })

    # Top-level
    top = _get(fc, "leagues")
    if top is not None:
        _extract_from_leagues(top)

    # Nested under users → games
    users_node = _get(fc, "users")
    if isinstance(users_node, dict):
        user_variants = _as_list(_get(users_node, "0", "user"))
        for user in user_variants:
            games_node = _get(user, "games")
            if not isinstance(games_node, dict):
                continue
            for k, v in games_node.items():
                if not str(k).isdigit() or not isinstance(v, dict):
                    continue
                gitems = v.get("game")
                if isinstance(gitems, dict):
                    gitems = [gitems]
                if not isinstance(gitems, list):
                    continue
                for g in gitems:
                    if isinstance(g, dict) and "leagues" in g:
                        _extract_from_leagues(g["leagues"])

    return out

# ---------------- Teams ----------------
def parse_teams(payload: dict, league_id: str) -> List[dict]:
    """
    Robust teams parser for Yahoo's NHL/NBA responses.
    """
    out: List[dict] = []

    def flatten_team_node(node: Any) -> Optional[dict]:
        if isinstance(node, dict):
            return node
        if isinstance(node, list):
            items = node[0] if node and isinstance(node[0], list) else node
            agg: dict = {}
            for part in items:
                if isinstance(part, dict):
                    for k, v in part.items():
                        agg[k] = v
            return agg if agg else None
        return None

    def extract_name(team_obj: dict) -> Optional[str]:
        nm = team_obj.get("name")
        if isinstance(nm, str):
            return nm
        if isinstance(nm, dict):
            full = nm.get("full")
            if isinstance(full, str):
                return full
        return None

    def extract_manager(team_obj: dict) -> Optional[str]:
        mgrs = team_obj.get("managers")
        if isinstance(mgrs, list):
            for item in mgrs:
                if isinstance(item, dict):
                    m = item.get("manager")
                    if isinstance(m, dict):
                        nick = m.get("nickname")
                        guid = m.get("guid")
                        if nick or guid:
                            return nick or guid
        if isinstance(mgrs, dict):
            for k, v in mgrs.items():
                if str(k).isdigit() and isinstance(v, dict):
                    m = v.get("manager")
                    if isinstance(m, dict):
                        nick = m.get("nickname")
                        guid = m.get("guid")
                        if nick or guid:
                            return nick or guid
        return None

    def maybe_take(team_node: Any):
        obj = flatten_team_node(team_node)
        if not isinstance(obj, dict):
            return
        team_key = obj.get("team_key")
        name = extract_name(obj)
        manager = extract_manager(obj)
        if team_key and name:
            out.append({"id": str(team_key), "name": str(name), "manager": manager})

    def walk(node: Any):
        if isinstance(node, dict):
            if "team" in node:
                maybe_take(node["team"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload.get("fantasy_content", {}))

    seen: set[str] = set()
    deduped: List[dict] = []
    for t in out:
        if t["id"] in seen:
            continue
        seen.add(t["id"])
        deduped.append(t)

    return deduped

# ---------------- Roster (emits assigned lineup slot) ----------------
def parse_roster(payload: dict, team_id: str) -> Tuple[str, List[dict]]:
    """
    Robust NHL-friendly roster parser.

    Returns (date, players[ {player_id, name, positions, status, slot?} ]).
    """
    def find_roster(node: Any) -> Optional[dict]:
        if isinstance(node, dict):
            if "roster" in node and isinstance(node["roster"], dict):
                return node["roster"]
            for v in node.values():
                r = find_roster(v)
                if r is not None:
                    return r
        elif isinstance(node, list):
            for item in node:
                r = find_roster(item)
                if r is not None:
                    return r
        return None

    def flatten_player_node(pnode: Any) -> Optional[dict]:
        if isinstance(pnode, dict):
            return pnode
        if isinstance(pnode, list):
            core = pnode[0] if pnode and isinstance(pnode[0], list) else pnode
            agg: dict = {}
            for part in core:
                if isinstance(part, dict):
                    for k, v in part.items():
                        agg[k] = v
            return agg or None
        return None

    def extract_positions(obj: dict) -> List[str]:
        positions: List[str] = []
        pos_raw = obj.get("eligible_positions")
        if isinstance(pos_raw, list):
            for pr in pos_raw:
                if isinstance(pr, dict) and "position" in pr:
                    positions.append(str(pr["position"]))
                elif isinstance(pr, str):
                    positions.append(pr)
        elif isinstance(pos_raw, dict) and "position" in pos_raw:
            positions.append(str(pos_raw["position"]))
        elif isinstance(pos_raw, str):
            positions.append(pos_raw)
        return positions

    fc = payload.get("fantasy_content", {})
    roster = find_roster(fc)
    if not isinstance(roster, dict):
        return "", []

    date = roster.get("date") or (isinstance(roster.get("0"), dict) and roster["0"].get("date")) or ""

    players_container = None
    r0 = roster.get("0")
    if isinstance(r0, dict):
        players_container = r0.get("players")

    if players_container is None:
        def find_players(n: Any) -> Optional[dict]:
            if isinstance(n, dict):
                if "players" in n and isinstance(n["players"], dict):
                    return n["players"]
                for v in n.values():
                    fp = find_players(v)
                    if fp is not None:
                        return fp
            elif isinstance(n, list):
                for itm in n:
                    fp = find_players(itm)
                    if fp is not None:
                        return fp
            return None
        players_container = find_players(roster)

    if not isinstance(players_container, dict):
        return str(date), []

    players_raw: List[tuple[dict, dict]] = []  # (container_item, flat_player)
    for k, v in players_container.items():
        if not str(k).isdigit() or not isinstance(v, dict):
            continue
        p = v.get("player")
        if p is None:
            continue
        flat = flatten_player_node(p)
        if isinstance(flat, dict):
            players_raw.append((v, flat))   # keep container 'v' to read selected_position sibling

    out: List[dict] = []
    for container, obj in players_raw:
        pid = obj.get("player_id")
        nm = obj.get("name")
        pname = nm.get("full") if isinstance(nm, dict) else (nm if isinstance(nm, str) else None)
        positions = extract_positions(obj)
        status = obj.get("status") or None

        # assigned slot (robust)
        slot = _extract_selected_slot(container, obj)

        if pid and pname:
            out.append({
                "player_id": str(pid),          # numeric is fine; FE normalizes with game key
                "name": str(pname),
                "positions": positions,
                "status": status,
                "slot": slot,                   # <-- now included
            })

    return (str(date), out)

# ---------------- Scoreboard (minimal & enriched) ----------------
def _normalize_team_name(team_obj: dict) -> str | None:
    nm = team_obj.get("name")
    if isinstance(nm, str):
        return nm
    if isinstance(nm, dict):
        full = nm.get("full")
        if isinstance(full, str):
            return full
    return None

def parse_scoreboard_min(payload: dict) -> dict:
    fc = payload.get("fantasy_content", {})
    out = {"week": None, "start_date": None, "end_date": None, "matchups": []}

    league_node = None
    L = fc.get("league")
    if isinstance(L, list) and len(L) >= 2:
        league_node = L[1]
    elif isinstance(L, dict):
        league_node = L

    scoreboard = None
    if isinstance(league_node, dict):
        scoreboard = league_node.get("scoreboard") or league_node.get("scoreboards")

    if isinstance(scoreboard, dict):
        out["week"] = scoreboard.get("week") or scoreboard.get("current_week")
        out["start_date"] = scoreboard.get("start_date")
        out["end_date"] = scoreboard.get("end_date")

        matchups_node = scoreboard.get("matchups")
        if isinstance(matchups_node, dict):
            for k, v in matchups_node.items():
                if not str(k).isdigit() or not isinstance(v, dict):
                    continue
                m = v.get("matchup")
                if not isinstance(m, (dict, list)):
                    continue

                if isinstance(m, list):
                    m_agg = {}
                    for part in m:
                        if isinstance(part, dict):
                            m_agg.update(part)
                    m = m_agg

                teams_node = m.get("teams")
                t1_key = t1_name = t2_key = t2_name = None
                if isinstance(teams_node, dict):
                    for tk, tv in teams_node.items():
                        if not str(tk).isdigit() or not isinstance(tv, dict):
                            continue
                        t = tv.get("team")
                        if isinstance(t, list):
                            t_agg = {}
                            for part in t:
                                if isinstance(part, dict):
                                    t_agg.update(part)
                            t = t_agg
                        if isinstance(t, dict):
                            key = t.get("team_key")
                            name = _normalize_team_name(t)
                            if t1_key is None:
                                t1_key, t1_name = key, name
                            else:
                                t2_key, t2_name = key, name

                status = m.get("status") or None
                is_playoffs = bool(m.get("is_playoffs")) if "is_playoffs" in m else None

                if t1_key and t2_key:
                    out["matchups"].append({
                        "team1_key": str(t1_key),
                        "team1_name": t1_name,
                        "team2_key": str(t2_key),
                        "team2_name": t2_name,
                        "status": status,
                        "is_playoffs": is_playoffs,
                    })

    return out

def select_matchup_for_team(scoreboard_min: dict, my_team_key: str) -> dict | None:
    """
    Given the minimal scoreboard dict from parse_scoreboard_min, return the matchup
    that involves my_team_key (team key like "465.l.34067.t.11"), or None if not found.
    """
    for m in scoreboard_min.get("matchups", []):
        if m.get("team1_key") == my_team_key or m.get("team2_key") == my_team_key:
            return m
    return None

def _flatten_team_obj(obj):
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, list):
        agg = {}
        for part in obj:
            if isinstance(part, dict):
                agg.update(part)
        return agg
    return {}

def _collect_team_stats(team_node) -> dict:
    stats_by_id = {}
    if isinstance(team_node, list) and len(team_node) >= 2 and isinstance(team_node[1], dict):
        ts = team_node[1].get("team_stats")
        if isinstance(ts, dict):
            stats = ts.get("stats")
            if isinstance(stats, list):
                for item in stats:
                    if isinstance(item, dict):
                        st = item.get("stat", {})
                        sid = st.get("stat_id")
                        val = st.get("value")
                        if sid is not None:
                            stats_by_id[str(sid)] = val
            elif isinstance(stats, dict):
                for k, v in stats.items():
                    if not str(k).isdigit() or not isinstance(v, dict):
                        continue
                    st = v.get("stat", {})
                    sid = st.get("stat_id")
                    val = st.get("value")
                    if sid is not None:
                        stats_by_id[str(sid)] = val
    return stats_by_id

def _collect_team_points(team_node) -> float | None:
    if isinstance(team_node, list):
        for part in team_node:
            if isinstance(part, dict) and "team_points" in part:
                tp = part["team_points"]
                if isinstance(tp, dict):
                    total = tp.get("total")
                    try:
                        return float(total) if total is not None else None
                    except Exception:
                        return None
    return None

def parse_scoreboard_enriched(payload: dict) -> dict:
    fc = payload.get("fantasy_content", {})
    out = {"week": None, "start_date": None, "end_date": None, "matchups": []}

    league_node = None
    L = fc.get("league")
    if isinstance(L, list) and len(L) >= 2:
        league_node = L[1]
    elif isinstance(L, dict):
        league_node = L

    sb = None
    if isinstance(league_node, dict):
        sb = league_node.get("scoreboard") or league_node.get("scoreboards")

    if not isinstance(sb, dict):
        return out

    out["week"] = (lambda x: int(x) if isinstance(x, str) and x.isdigit() else x)(sb.get("week"))
    out["start_date"] = sb.get("start_date")
    out["end_date"] = sb.get("end_date")

    matchups_node = sb.get("matchups")
    if not isinstance(matchups_node, dict):
        return out

    for k, v in matchups_node.items():
        if not str(k).isdigit() or not isinstance(v, dict):
            continue
        m = v.get("matchup")
        if not isinstance(m, (dict, list)):
            continue

        if isinstance(m, list):
            m_agg = {}
            for part in m:
                if isinstance(part, dict):
                    m_agg.update(part)
            m = m_agg

        status = m.get("status")
        is_playoffs = bool(m.get("is_playoffs")) if "is_playoffs" in m else None

        t1 = {"key": None, "name": None, "points": None, "stats": {}}
        t2 = {"key": None, "name": None, "points": None, "stats": {}}
        teams_node = m.get("teams")
        if isinstance(teams_node, dict):
            idx_sorted = sorted([i for i in teams_node.keys() if str(i).isdigit()], key=lambda x: int(x))
            bucket = []
            for idx in idx_sorted:
                tv = teams_node[idx]
                if isinstance(tv, dict):
                    bucket.append(tv.get("team"))
            sides = []
            for team_node in bucket:
                if team_node is None:
                    continue
                team_fields = None
                if isinstance(team_node, list) and len(team_node) >= 1:
                    team_fields = _flatten_team_obj(team_node[0])
                elif isinstance(team_node, dict):
                    team_fields = _flatten_team_obj(team_node)
                else:
                    team_fields = _flatten_team_obj(team_node)

                key = team_fields.get("team_key")
                name = _normalize_team_name(team_fields)
                points = _collect_team_points(team_node)
                stats = _collect_team_stats(team_node)
                sides.append({"key": key, "name": name, "points": points, "stats": stats})

            if len(sides) >= 2:
                t1, t2 = sides[0], sides[1]

        out["matchups"].append({
            "status": status,
            "is_playoffs": is_playoffs,
            "team1": t1,
            "team2": t2,
            "winners": [],
        })

    return out
