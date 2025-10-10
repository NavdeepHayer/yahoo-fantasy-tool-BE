from typing import Any, Dict, List, Tuple, Optional

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
