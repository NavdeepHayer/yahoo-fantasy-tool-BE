from typing import Any, List, Tuple, Optional

# ---- Small utils (pure, parser-local) ----
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


# ---- Leagues ----
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


# ---- Teams ----
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


# ---- Roster (basic version; same behavior as your current code) ----
def parse_roster(payload: dict, team_id: str) -> Tuple[str, List[dict]]:
    """
    Robust NHL-friendly roster parser.

    Returns (date, players[ {player_id, name, positions, status} ]).
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

    players_raw: List[dict] = []
    for k, v in players_container.items():
        if not str(k).isdigit() or not isinstance(v, dict):
            continue
        p = v.get("player")
        if p is None:
            continue
        flat = flatten_player_node(p)
        if isinstance(flat, dict):
            players_raw.append(flat)

    out: List[dict] = []
    for obj in players_raw:
        pid = obj.get("player_id")
        pname = None
        nm = obj.get("name")
        if isinstance(nm, dict):
            pname = nm.get("full") or nm.get("name")
        elif isinstance(nm, str):
            pname = nm
        positions = extract_positions(obj)
        status = obj.get("status") or None
        if pid and pname:
            out.append({
                "player_id": str(pid),
                "name": str(pname),
                "positions": positions,
                "status": status,
            })

    return (str(date), out)


# --- Minimal scoreboard parsing (league scoreboard;week=N) ---

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
    """
    Returns a dict:
    {
      "week": <int|None>,
      "start_date": <str|None>,
      "end_date": <str|None>,
      "matchups": [
        {"team1_key": str, "team1_name": str|None,
         "team2_key": str, "team2_name": str|None,
         "status": str|None, "is_playoffs": bool|None}
      ]
    }
    This is a tolerant, minimal extractor (no scores/categories yet).
    """
    fc = payload.get("fantasy_content", {})
    out = {"week": None, "start_date": None, "end_date": None, "matchups": []}

    # Try to find a "scoreboard" node
    league_node = None
    L = fc.get("league")
    if isinstance(L, list) and len(L) >= 2:
        league_node = L[1]
    elif isinstance(L, dict):
        league_node = L

    scoreboard = None
    if isinstance(league_node, dict):
        scoreboard = league_node.get("scoreboard") or league_node.get("scoreboards")

    # week meta
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

                # normalize matchup object (dict or list of dict fragments)
                if isinstance(m, list):
                    m_agg = {}
                    for part in m:
                        if isinstance(part, dict):
                            m_agg.update(part)
                    m = m_agg

                # read teams inside matchup
                teams_node = m.get("teams")
                t1_key = t1_name = t2_key = t2_name = None
                if isinstance(teams_node, dict):
                    # Yahoo usually has "0" and "1" entries
                    for tk, tv in teams_node.items():
                        if not str(tk).isdigit() or not isinstance(tv, dict):
                            continue
                        t = tv.get("team")
                        # "team" can be list of fragments or dict
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
    for m in scoreboard_min.get("matchups", []):
        if m.get("team1_key") == my_team_key or m.get("team2_key") == my_team_key:
            return m
    return None


# ---- Enriched scoreboard parsing (categories + points) ----

def _flatten_team_obj(obj):
    """Team node can be a dict or a list of tiny dicts."""
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
    """
    From the team block (which includes a second element holding 'team_stats' etc.),
    return {stat_id: value} (string values as Yahoo provides).
    """
    stats_by_id = {}
    # team_node is usually [ [<team fields...>], {team_stats: {...}}, {team_points: {...}}, ...]
    if isinstance(team_node, list) and len(team_node) >= 2 and isinstance(team_node[1], dict):
        ts = team_node[1].get("team_stats")
        if isinstance(ts, dict):
            stats = ts.get("stats")
            # stats may be a list of {"stat":{stat_id,value}} or dict with numeric keys
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
    """
    Returns:
    {
      "week": int|None,
      "start_date": str|None,
      "end_date": str|None,
      "matchups": [
        {
          "status": str|None,
          "is_playoffs": bool|None,
          "team1": {"key": str, "name": str|None, "points": float|None, "stats": {stat_id: value}},
          "team2": {"key": str, "name": str|None, "points": float|None, "stats": {stat_id: value}},
          "winners": [{"stat_id": str, "winner_team_key": str|None, "is_tied": bool}],
        }
      ]
    }
    """
    fc = payload.get("fantasy_content", {})
    out = {"week": None, "start_date": None, "end_date": None, "matchups": []}

    # locate scoreboard
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

        # flatten matchup
        if isinstance(m, list):
            m_agg = {}
            for part in m:
                if isinstance(part, dict):
                    m_agg.update(part)
            m = m_agg

        status = m.get("status")
        is_playoffs = bool(m.get("is_playoffs")) if "is_playoffs" in m else None

        # winners list
        winners = []
        sw = m.get("stat_winners")
        if isinstance(sw, list):
            for item in sw:
                if isinstance(item, dict):
                    w = item.get("stat_winner", {})
                    winners.append({
                        "stat_id": str(w.get("stat_id")) if w.get("stat_id") is not None else None,
                        "winner_team_key": w.get("winner_team_key"),
                        "is_tied": bool(w.get("is_tied")) if "is_tied" in w else False,
                    })
        elif isinstance(sw, dict):
            for ik, iv in sw.items():
                if not str(ik).isdigit() or not isinstance(iv, dict):
                    continue
                w = iv.get("stat_winner", {})
                winners.append({
                    "stat_id": str(w.get("stat_id")) if w.get("stat_id") is not None else None,
                    "winner_team_key": w.get("winner_team_key"),
                    "is_tied": bool(w.get("is_tied")) if "is_tied" in w else False,
                })

        # teams
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
            # Now bucket ~ [team_obj_for_side1, team_obj_for_side2]
            sides = []
            for team_node in bucket:
                if team_node is None:
                    continue
                # team details
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
            "winners": winners,
        })

    return out
