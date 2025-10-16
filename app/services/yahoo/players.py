# app/services/yahoo/players.py
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy.orm import Session

from app.services.yahoo.client import yahoo_get
from app.db.models import OAuthToken


# =========================
# basics / small utilities
# =========================

def _active_user_id(db: Session) -> str:
    tok = db.query(OAuthToken).order_by(OAuthToken.created_at.desc()).first()
    return (getattr(tok, "user_id", None) or getattr(tok, "xoauth_yahoo_guid", None) or "").strip()

def _find_first(node: Any, keys: List[str]) -> Optional[str]:
    if isinstance(node, dict):
        for k, v in node.items():
            if k in keys and isinstance(v, (str, int, float)):
                return str(v)
            got = _find_first(v, keys)
            if got is not None:
                return got
    elif isinstance(node, list):
        for x in node:
            got = _find_first(x, keys)
            if got is not None:
                return got
    return None


# =========================
# players node extraction
# =========================

def _normalize_player_node(node: Any) -> List[Any]:
    if isinstance(node, list) and len(node) == 1 and isinstance(node[0], list):
        return node[0]
    return node if isinstance(node, list) else [node]

def _find_players_strict(raw: Any) -> List[Any]:
    out: List[Any] = []

    def collect(container: Any):
        if isinstance(container, list):
            for item in container:
                if isinstance(item, dict) and "player" in item and isinstance(item["player"], list):
                    out.append(_normalize_player_node(item["player"]))
        elif isinstance(container, dict):
            for k, v in container.items():
                if k == "count":
                    continue
                if isinstance(v, dict) and "player" in v and isinstance(v["player"], list):
                    out.append(_normalize_player_node(v["player"]))

    def rec(n: Any):
        if isinstance(n, dict):
            if "players" in n:
                collect(n["players"])
                return
            for v in n.values():
                rec(v)
        elif isinstance(n, list):
            for x in n:
                rec(x)

    rec(raw)
    return out

def _find_players_any(raw: Any) -> List[Any]:
    out: List[Any] = []
    def rec(n: Any):
        if isinstance(n, dict):
            for k, v in n.items():
                if k == "player" and isinstance(v, list):
                    out.append(_normalize_player_node(v))
                rec(v)
        elif isinstance(n, list):
            for x in n:
                rec(x)
    rec(raw)
    return out

def _players_match_q(nodes: List[Any], q: Optional[str]) -> bool:
    if not q:
        return True
    ql = q.strip().lower()
    if not ql:
        return True
    for n in nodes:
        c = _player_from_node(n)
        text = f"{c.get('name','')} {c.get('team','')}".lower()
        if ql in text or all(tok in text for tok in ql.split()):
            return True
    return False

def _find_players(raw: Any, q: Optional[str] = None) -> List[Any]:
    strict_nodes = _find_players_strict(raw)
    if q:
        if not strict_nodes or not _players_match_q(strict_nodes, q):
            perm = _find_players_any(raw)
            return perm if _players_match_q(perm, q) else strict_nodes or perm
        return strict_nodes
    return strict_nodes or _find_players_any(raw)


# =========================
# player coercion helpers
# =========================

def _collect_positions(node: Any) -> List[str]:
    pos: List[str] = []
    def rec(n: Any):
        if isinstance(n, dict):
            for k, v in n.items():
                if k in ("eligible_positions", "primary_position", "positions"):
                    if isinstance(v, list):
                        for p in v:
                            if isinstance(p, (str, int)):
                                pos.append(str(p))
                            elif isinstance(p, dict):
                                for vv in p.values():
                                    if isinstance(vv, (str, int)):
                                        pos.append(str(vv))
                    elif isinstance(v, (str, int)):
                        pos.append(str(v))
                else:
                    rec(v)
        elif isinstance(n, list):
            for x in n:
                rec(x)
    rec(node)
    seen, out = set(), []
    for p in pos:
        if p and p not in seen:
            out.append(p); seen.add(p)
    return out

def _player_from_node(node: Any) -> Dict[str, Any]:
    if isinstance(node, list):
        fields = node[0] if len(node) == 1 and isinstance(node[0], list) else node
    elif isinstance(node, dict):
        fields = [node]
    else:
        fields = []

    merged: Dict[str, Any] = {}
    for piece in fields:
        if isinstance(piece, dict):
            for k, v in piece.items():
                merged.setdefault(k, v)  # first wins

    name = _find_first(merged, ["full", "full_name", "name", "fullName"]) or ""
    if not name and isinstance(merged.get("name"), dict):
        name = _find_first(merged["name"], ["full", "full_name"]) or ""

    player_id = _find_first(merged, ["player_key", "playerKey", "player_id", "playerId"]) or ""
    team = _find_first(merged, ["editorial_team_abbr", "team_abbr", "team"])
    jersey = _find_first(merged, ["uniform_number", "jersey"])

    status = _find_first(merged, ["roster_status", "status", "injury_status"])
    if status in ("True", "False", "true", "false"):
        status = None

    img = None
    if isinstance(merged.get("headshot"), dict):
        img = _find_first(merged["headshot"], ["url"])
    if not img:
        img = _find_first(merged, ["image_url", "imageUrl"])
    if img and isinstance(img, str) and not img.startswith("http"):
        img = None

    positions = _collect_positions(merged)

    return {
        "player_id": player_id,
        "name": name,
        "team": team,
        "positions": positions,
        "eligibility": positions,
        "jersey": jersey,
        "status": status,
        "yahoo_image_url": img,
        "image_url": img,
    }


# =========================
# league stat id -> key map
# =========================

# Simple in-memory cache keyed by (user_id, league_id)
_STAT_CACHE: Dict[tuple[str, str], Dict[str, str]] = {}

def _league_stat_map(db: Session, league_id: str) -> Dict[str, str]:
    """
    Map Yahoo stat_id -> display key (prefer abbr -> display_name -> name).
    Works for shapes like:
      stat_categories -> stats -> [{"stat": {...}}, ...]
    or
      stat_categories -> stats -> [{...}, ...]
    Caches per (user_id, league_id).
    """
    user_id = _active_user_id(db)
    cache_key = (user_id, league_id)
    if cache_key in _STAT_CACHE:
        return _STAT_CACHE[cache_key]

    raw = yahoo_get(db, user_id, f"/league/{league_id}/settings")

    # Collect every stats list under stat_categories
    stats_lists: List[List[Dict[str, Any]]] = []

    def rec(n: Any):
        if isinstance(n, dict):
            # Find 'stat_categories' container and pull its inner 'stats' (list)
            if "stat_categories" in n:
                cat = n["stat_categories"]
                # It can be a list of dicts or a single dict
                containers = cat if isinstance(cat, list) else [cat]
                for c in containers:
                    if isinstance(c, dict) and "stats" in c and isinstance(c["stats"], list):
                        stats_lists.append(c["stats"])
            # keep walking
            for v in n.values():
                rec(v)
        elif isinstance(n, list):
            for x in n:
                rec(x)

    rec(raw)

    m: Dict[str, str] = {}

    def pull_sid_and_name(item: Dict[str, Any]) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        """Return (sid, abbr, display_name, name) for either {'stat':{...}} or flat {...}."""
        node = item.get("stat") if isinstance(item.get("stat"), dict) else item
        if not isinstance(node, dict):
            return None, None, None, None
        sid = node.get("stat_id") or node.get("statId") or node.get("id")
        abbr = node.get("abbr") or node.get("stat_abbr")
        disp = node.get("display_name") or node.get("displayName")
        name = node.get("name")
        return (str(sid) if sid is not None else None,
                str(abbr) if abbr is not None else None,
                str(disp) if disp is not None else None,
                str(name) if name is not None else None)

    for lst in stats_lists:
        for it in lst:
            if not isinstance(it, dict):
                continue
            sid, abbr, disp, name = pull_sid_and_name(it)
            if not sid:
                continue
            key = abbr or disp or name
            if key:
                # first write wins (avoid overwriting if duplicates)
                m.setdefault(str(sid), key)

    # cache + return
    _STAT_CACHE[cache_key] = m
    return m



# =========================
# PUBLIC API (routes expect these)
# =========================

def search_players(
    db: Session,
    league_id: str,
    q: Optional[str] = None,
    position: Optional[str] = None,
    status: Optional[str] = None,  # FA | W | T
    page: int = 1,
    per_page: int = 25,
) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    """
    League-scoped search (Yahoo: /league/{league_id}/players;search=...).
    Works for NBA/NHL and returns normalized items. If nothing with `status`, retries without it.
    """
    user_id = _active_user_id(db)
    start = (page - 1) * per_page

    filters: List[str] = []
    if q:
        filters.append(f"search={q}")
    if position:
        filters.append(f"position={position}")
    if status:
        filters.append(f"status={status}")

    def build_path(with_filters: List[str]) -> str:
        fs = ";" + ";".join(with_filters) if with_filters else ""
        return f"/league/{league_id}/players{fs};start={start};count={per_page}"

    raw = yahoo_get(db, user_id, build_path(filters))
    nodes = _find_players(raw, q=q)

    # if empty and we filtered by status, retry without status
    if (not nodes) and status:
        nf = [f for f in filters if not f.startswith("status=")]
        raw = yahoo_get(db, user_id, build_path(nf))
        nodes = _find_players(raw, q=q)

    seen: set[str] = set()
    items: List[Dict[str, Any]] = []
    for n in nodes:
        c = _player_from_node(n)
        pid = c.get("player_id")
        if pid and pid not in seen:
            items.append(c); seen.add(pid)

    next_page = page + 1 if len(items) == per_page else None
    return items, next_page


def get_player(
    db: Session,
    player_id: str,
    league_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Fetch one player by Yahoo player_key (e.g. '466.p.4244').
    If league_id is provided, Yahoo returns league-scoped eligibility.
    """
    user_id = _active_user_id(db)
    if league_id:
        raw = yahoo_get(db, user_id, f"/league/{league_id}/players;player_keys={player_id}")
    else:
        raw = yahoo_get(db, user_id, f"/players;player_keys={player_id}")
    nodes = _find_players(raw)
    return _player_from_node(nodes[0]) if nodes else {"player_id": player_id, "name": ""}


def get_players_batch(
    db: Session,
    player_ids: List[str],
    league_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Batch fetch by IDs (1–25 per Yahoo call).
    """
    user_id = _active_user_id(db)
    ids = [pid for pid in player_ids if pid]
    if not ids:
        return []

    CHUNK = 25
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()

    for i in range(0, len(ids), CHUNK):
        chunk = ids[i:i+CHUNK]
        keys = ",".join(chunk)
        path = (
            f"/league/{league_id}/players;player_keys={keys}"
            if league_id else
            f"/players;player_keys={keys}"
        )
        raw = yahoo_get(db, user_id, path)
        nodes = _find_players(raw)
        for n in nodes:
            c = _player_from_node(n)
            pid = c.get("player_id")
            if pid and pid not in seen:
                out.append(c); seen.add(pid)
    return out


def get_player_stats(
    db: Session,
    player_id: str,
    *,
    league_id: str,
    kind: str = "season",           # season | week | last7 | last14 | last30 | date_range
    season: Optional[str] = None,
    week: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Returns a list of PlayerStatLine dicts: {"player_id","scope","values":{<display_key>:float,...}}
    Robust behavior:
      - date_range: fan out to per-day calls (type=date;date=YYYY-MM-DD), then sum
      - week: try Yahoo week first; if empty, aggregate per-day using scoreboard's week range
    """
    user_id = _active_user_id(db)

    # small inner: fetch one scope path and parse + map ids -> league keys
    def _fetch_and_parse(path: str) -> Dict[str, float]:
        raw = yahoo_get(db, user_id, path)

        # collect all "stats" blocks (player_stats, player_advanced_stats, etc.)
        def _iter_stats_items(node: Any):
            if not isinstance(node, list):
                return
            for s in node:
                if not isinstance(s, dict):
                    continue
                # {"stat": {"stat_id": "...", "value": "..." }}
                if "stat" in s and isinstance(s["stat"], dict):
                    sid = str(s["stat"].get("stat_id") or s["stat"].get("statId") or s["stat"].get("id") or "")
                    val = s["stat"].get("value")
                    yield sid, val
                    continue
                # {"stat_id": "...", "value": "..."}
                sid = str(s.get("stat_id") or s.get("statId") or s.get("id") or "")
                val = s.get("value") if "value" in s else s.get("val")
                if sid:
                    yield sid, val

        lines: List[Dict[str, float]] = []

        def dig_stats(n: Any):
            if isinstance(n, dict):
                if "stats" in n and isinstance(n["stats"], list):
                    acc: Dict[str, float] = {}
                    for sid, val in _iter_stats_items(n["stats"]):
                        if val in (None, "", "-"):
                            fval = 0.0
                        else:
                            try:
                                fval = float(val)
                            except Exception:
                                try:
                                    fval = float(str(val).replace("%", ""))
                                except Exception:
                                    fval = 0.0
                        if sid:
                            acc[sid] = acc.get(sid, 0.0) + fval
                    if acc:
                        lines.append(acc)
                for v in n.values():
                    dig_stats(v)
            elif isinstance(n, list):
                for x in n:
                    dig_stats(x)

        dig_stats(raw)

        merged: Dict[str, float] = {}
        for ln in lines:
            for k, v in ln.items():
                merged[k] = merged.get(k, 0.0) + float(v or 0.0)

        id2key = _league_stat_map(db, league_id)
        pretty: Dict[str, float] = {}
        for sid, val in merged.items():
            key = id2key.get(sid) or sid
            pretty[key] = pretty.get(key, 0.0) + float(val or 0.0)
        return pretty

    # --------- KINDS ----------
    if kind == "date_range" and date_from and date_to:
        # Fan-out day-by-day (Yahoo player endpoint expects 'date', not start/end)
        scope = f"date:{date_from}" if date_from == date_to else f"date_range:{date_from}..{date_to}"
        totals: Dict[str, float] = {}
        for d in _iter_dates_inclusive(date_from, date_to):
            p = f"/league/{league_id}/players;player_keys={player_id}/stats;type=date;date={d}"
            day_vals = _fetch_and_parse(p)
            _sum_into(totals, day_vals)
        return [{"player_id": player_id, "scope": scope, "values": totals}]

    if kind == "week" and week:
        # First try Yahoo's native week call
        path = f"/league/{league_id}/players;player_keys={player_id}/stats;type=week;week={week}"
        week_vals = _fetch_and_parse(path)
        if week_vals:  # success path
            return [{"player_id": player_id, "scope": f"week:{week}", "values": week_vals}]

        # Fallback: resolve week boundaries and aggregate per-day
        ws, we = _week_bounds(db, user_id, league_id, week)
        totals: Dict[str, float] = {}
        for d in _iter_dates_inclusive(ws, we):
            p = f"/league/{league_id}/players;player_keys={player_id}/stats;type=date;date={d}"
            day_vals = _fetch_and_parse(p)
            _sum_into(totals, day_vals)
        return [{"player_id": player_id, "scope": f"week:{week}", "values": totals}]

    if kind in ("last7", "last14", "last30"):
        window = kind.replace("last", "")
        path = f"/league/{league_id}/players;player_keys={player_id}/stats;type=last{window}"
        vals = _fetch_and_parse(path)
        return [{"player_id": player_id, "scope": kind, "values": vals}]

    # default: season (optionally explicit season)
    extra = f";season={season}" if season else ""
    path = f"/league/{league_id}/players;player_keys={player_id}/stats;type=season{extra}"
    vals = _fetch_and_parse(path)
    return [{"player_id": player_id, "scope": f"season:{season}" if season else "season", "values": vals}]




def get_team_weekly_totals(
    db: Session,
    *,
    league_id: str,
    team_id: str,
    week: int,
) -> Dict[str, Any]:
    """
    Sums weekly stats for all players on a team for the given week.
    """
    user_id = _active_user_id(db)

    raw = yahoo_get(db, user_id, f"/team/{team_id}/roster;week={week}")
    player_nodes = _find_players(raw)

    totals: Dict[str, float] = {}
    per_player: List[Dict[str, Any]] = []

    for n in player_nodes:
        pid = _find_first(n, ["player_id", "playerId", "player_key", "playerKey"])
        if not pid:
            continue
        stat_lines = get_player_stats(
            db,
            pid,
            league_id=league_id,
            kind="week",
            week=week,
        )
        if not stat_lines:
            continue
        vals = stat_lines[0]["values"]
        for k, v in vals.items():
            totals[k] = totals.get(k, 0.0) + float(v or 0.0)
        per_player.append({
            "player_id": pid,
            "scope": f"week:{week}",
            "values": vals,
        })

    return {
        "league_id": league_id,
        "team_id": team_id,
        "week": week,
        "totals": totals,
        "players": per_player,
    }


# =========================
# optional: global search
# =========================

def _resolve_game_key(
    db: Session, *,
    sport: Optional[str] = None,
    season: Optional[str] = None,
    game_key: Optional[str] = None,
) -> str:
    if game_key:
        return str(game_key)
    if not sport:
        raise ValueError("Provide either game_key or sport")

    user_id = _active_user_id(db)
    season = season or ""
    path = f"/games;game_codes={sport}" + (f";seasons={season}" if season else "")
    raw = yahoo_get(db, user_id, path)

    keys: List[str] = []
    def rec(n: Any):
        if isinstance(n, dict):
            if "game_key" in n and isinstance(n["game_key"], (str, int)):
                keys.append(str(n["game_key"]))
            for v in n.values():
                rec(v)
        elif isinstance(n, list):
            for x in n:
                rec(x)
    rec(raw)

    if not keys:
        raise ValueError(f"Could not resolve game_key for sport={sport!r} season={season!r}")
    return keys[-1]

def search_players_global(
    db: Session,
    *,
    q: Optional[str] = None,
    position: Optional[str] = None,
    page: int = 1,
    per_page: int = 25,
    sport: Optional[str] = None,
    season: Optional[str] = None,
    game_key: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    """
    League-agnostic player search (Yahoo: /game/{game_key}/players;search=...).
    """
    user_id = _active_user_id(db)
    gkey = _resolve_game_key(db, sport=sport, season=season, game_key=game_key)

    start = (page - 1) * per_page
    filters: List[str] = []
    if q:
        filters.append(f"search={q}")
    if position:
        filters.append(f"position={position}")
    fs = ";" + ";".join(filters) if filters else ""

    raw = yahoo_get(db, user_id, f"/game/{gkey}/players{fs};start={start};count={per_page}")
    nodes = _find_players(raw, q=q)

    seen: set[str] = set()
    items_unfiltered: List[Dict[str, Any]] = []
    for n in nodes:
        c = _player_from_node(n)
        pid = c.get("player_id")
        if pid and pid not in seen:
            items_unfiltered.append(c); seen.add(pid)

    # token fallback (first/last) only if nothing matched
    if not items_unfiltered and q and " " in q:
        first, last = q.split()[0], q.split()[-1]
        for part in (first, last):
            raw2 = yahoo_get(db, user_id, f"/game/{gkey}/players;search={part};start={start};count={per_page}")
            for n2 in _find_players(raw2, q=part):
                c2 = _player_from_node(n2)
                pid2 = c2.get("player_id")
                if pid2 and pid2 not in seen:
                    items_unfiltered.append(c2); seen.add(pid2)

    # If a query is present, filter to real matches; otherwise return all
    if q:
        ql = q.lower()
        items = [it for it in items_unfiltered
                 if (ql in f"{it.get('name','')} {it.get('team','')}".lower()
                     or all(tok in f"{it.get('name','')} {it.get('team','')}".lower() for tok in ql.split()))]
    else:
        items = items_unfiltered

    next_page = page + 1 if len(items) == per_page else None
    return items, next_page


def _get_league_season(db: Session, league_id: str) -> Optional[str]:
    """
    Fetch the league to discover its season (e.g. '2025').
    If Yahoo responds in an unexpected shape, we try a couple nests.
    """
    user_id = _active_user_id(db)
    try:
        raw = yahoo_get(db, user_id, f"/league/{league_id}")
    except Exception:
        return None

    # Try the common shapes
    # A) fantasy_content -> league -> [{...}, {"season": "2025"}, ...]
    node = raw.get("fantasy_content", {}).get("league")
    if isinstance(node, list):
        for piece in node:
            if isinstance(piece, dict) and "season" in piece and piece["season"]:
                return str(piece["season"])

    # B) nested dicts
    def _find_season(n: Any) -> Optional[str]:
        if isinstance(n, dict):
            if "season" in n and n["season"]:
                return str(n["season"])
            for v in n.values():
                got = _find_season(v)
                if got:
                    return got
        elif isinstance(n, list):
            for x in n:
                got = _find_season(x)
                if got:
                    return got
        return None

    return _find_season(raw)


# --- add near the top of this file (helpers) -------------------------------

from datetime import date, timedelta

def _date_yyyymmdd(s: str) -> date:
    # safe parse YYYY-MM-DD
    y, m, d = (int(x) for x in s.split("-"))
    return date(y, m, d)

def _iter_dates_inclusive(a: str, b: str):
    start, end = _date_yyyymmdd(a), _date_yyyymmdd(b)
    if end < start:
        start, end = end, start
    cur = start
    one = timedelta(days=1)
    while cur <= end:
        yield cur.isoformat()
        cur += one

def _week_bounds(db: Session, user_id: str, league_id: str, week: int) -> tuple[str, str]:
    """
    Get (week_start, week_end) as YYYY-MM-DD from the league scoreboard.
    Works even for 'date' roster leagues.
    """
    raw = yahoo_get(db, user_id, f"/league/{league_id}/scoreboard;week={week}")
    # try common keys
    ws = _find_first(raw, ["week_start", "week-start", "weekStart", "start", "week_start_date"])
    we = _find_first(raw, ["week_end", "week-end", "weekEnd", "end", "week_end_date"])
    if not ws or not we:
        # last resort: look for 'start'/'end' shaped dates anywhere
        ws = _find_first(raw, ["start_date", "startDate", "start"]) or ws
        we = _find_first(raw, ["end_date", "endDate", "end"]) or we
    if not ws or not we:
        raise ValueError(f"Could not resolve week {week} range from scoreboard for league {league_id}")
    # Normalize to YYYY-MM-DD (in case timestamps sneak in)
    ws = str(ws)[:10]
    we = str(we)[:10]
    return ws, we

def _sum_into(dst: dict[str, float], src: dict[str, float]) -> None:
    for k, v in (src or {}).items():
        dst[k] = dst.get(k, 0.0) + float(v or 0.0)
