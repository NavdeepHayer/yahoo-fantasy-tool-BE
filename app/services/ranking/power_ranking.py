# app/services/ranking/power_ranking.py
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from math import sqrt
from statistics import mean, pstdev
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from app.services.yahoo.client import yahoo_get
from app.services.yahoo.players import get_players_stats_batch


# ========= Sport config (extend as needed) =========

LOWER_IS_BETTER: Dict[str, Set[str]] = {
    "nba": {"TO"},
    "nhl": {"PIM", "GAA"},
}

# Percent/ratio categories expressed as (numerator, denominator)
PERCENT_TRIPLETS: Dict[str, Dict[str, Tuple[str, str]]] = {
    "nba": {
        "FG%": ("FGM", "FGA"),
        "FT%": ("FTM", "FTA"),
        "3PT%": ("3PTM", "3PTA"),
    },
    "nhl": {
        "SV%": ("SV", "SA"),
    },
}

# Common stat aliases → canonical
ALIASES: Dict[str, Dict[str, str]] = {
    "nba": {
        "3P": "3PTM", "3PT": "3PTM", "3PM": "3PTM",
        "3PA": "3PTA",
        "STL": "ST",
        "BLK": "BLK", "TOV": "TO",
        "FGM / FGA": "", "FTM / FTA": "", "3PTM / 3PTA": "",  # skip helpers
    },
    "nhl": {
        "PLUS/MINUS": "+/-", "PLUSMINUS": "+/-",
        "PP PTS": "PPP",
        "SV / SA": "",  # skip helpers if ever present
    },
}


# ========= Public helpers =========

def lower_is_better_for(sport: str) -> Set[str]:
    return LOWER_IS_BETTER.get((sport or "").lower(), set())


def percent_triplets_for(sport: str) -> Dict[str, Tuple[str, str]]:
    return PERCENT_TRIPLETS.get((sport or "").lower(), {})


def normalize_cat(sport: str, raw: str) -> str:
    key = (raw or "").strip()
    if not key:
        return key
    canon = ALIASES.get((sport or "").lower(), {})
    mapped = canon.get(key, key)
    # empty string means “skip from display”
    return mapped


# ========= Internal parse utilities =========

def _find_game_key(node: dict) -> str:
    def rec(n):
        if isinstance(n, dict):
            if "league_key" in n:
                return str(n["league_key"]).split(".")[0]
            for v in n.values():
                g = rec(v)
                if g:
                    return g
        elif isinstance(n, list):
            for x in n:
                g = rec(x)
                if g:
                    return g
        return ""
    return rec(node)


def _detect_sport_from_gamekey(game_key: str | None) -> str:
    if not game_key:
        return "nba"
    if str(game_key).startswith("466"):  # NBA 2025
        return "nba"
    if str(game_key).startswith("465"):  # NHL 2025
        return "nhl"
    return "nba"


def _extract_categories(settings: dict, sport: str) -> List[str]:
    raw: List[str] = []

    def rec(n):
        if isinstance(n, dict):
            if "stat_categories" in n:
                container = n["stat_categories"]
                stats = []
                if isinstance(container, list):
                    for c in container:
                        if isinstance(c, dict) and isinstance(c.get("stats"), list):
                            stats.extend(c["stats"])
                elif isinstance(container, dict) and isinstance(container.get("stats"), list):
                    stats = container["stats"]
                for it in stats:
                    node = it.get("stat") if isinstance(it.get("stat"), dict) else it
                    if not isinstance(node, dict):
                        continue
                    abbr = node.get("abbr") or node.get("stat_abbr")
                    disp = node.get("display_name") or node.get("displayName")
                    name = node.get("name")
                    key = (abbr or disp or name)
                    if key:
                        raw.append(str(key))
            for v in n.values():
                rec(v)
        elif isinstance(n, list):
            for x in n:
                rec(x)

    rec(settings)

    seen = set()
    out: List[str] = []
    for c in raw:
        nc = normalize_cat(sport, c)
        if nc and nc not in seen:
            out.append(nc)
            seen.add(nc)
    return out

def _build_stat_id_map(settings: dict) -> Dict[str, str]:
    """
    From /league/{id}/settings, build {stat_id(str): abbr_or_display(str)}.
    We prefer 'abbr', then 'display_name', then 'name'.
    """
    out: Dict[str, str] = {}

    def rec(n):
        if isinstance(n, dict):
            if "stat_categories" in n:
                container = n["stat_categories"]
                stats = []
                if isinstance(container, list):
                    for c in container:
                        if isinstance(c, dict) and isinstance(c.get("stats"), list):
                            stats.extend(c["stats"])
                elif isinstance(container, dict) and isinstance(container.get("stats"), list):
                    stats = container["stats"]
                for it in stats:
                    node = it.get("stat") if isinstance(it.get("stat"), dict) else it
                    if not isinstance(node, dict):
                        continue
                    sid = node.get("stat_id")
                    abbr = node.get("abbr") or node.get("stat_abbr")
                    disp = node.get("display_name") or node.get("displayName")
                    name = node.get("name")
                    key = (abbr or disp or name)
                    if sid is not None and key:
                        out[str(sid)] = str(key)
            for v in n.values():
                rec(v)
        elif isinstance(n, list):
            for x in n:
                rec(x)

    rec(settings)
    return out



# -- team extraction: even more forgiving
def _parse_teams_payload(data) -> List[dict]:
    """
    Extract teams as dicts: {"team_key", "team_id", "name"}
    Handles:
      - nodes where "team" is a list (canonical)
      - nodes where "team" is a dict (seen sometimes)
      - any dict that itself has team_key/name at the same level
    """
    results: List[dict] = []

    def maybe_add(d: dict):
        tkey = d.get("team_key")
        tname = d.get("name")
        tid = str(d.get("team_id")) if d.get("team_id") is not None else None
        if tkey and tname:
            results.append({"team_key": tkey, "team_id": tid or tkey, "name": tname})

    def rec(n):
        if isinstance(n, dict):
            # direct dict with team properties
            if "team_key" in n and "name" in n:
                maybe_add(n)

            # canonical list form
            if "team" in n:
                t = n["team"]
                if isinstance(t, list):
                    flat: dict = {}
                    for part in t:
                        if isinstance(part, dict):
                            flat.update(part)
                    maybe_add(flat)
                elif isinstance(t, dict):
                    maybe_add(t)

            for v in n.values():
                rec(v)
        elif isinstance(n, list):
            for x in n:
                rec(x)

    rec(data)
    # De-dupe by team_key
    uniq = {}
    for t in results:
        uniq[t["team_key"]] = t
    return list(uniq.values())


def _get_teams(db: Session, user_id: str, league_id: str) -> List[dict]:
    """
    Return list of {team_key, team_id, name}. Tries /teams, falls back to /standings.
    """
    def parse_teams_payload(data) -> List[dict]:
        teams: List[dict] = []
        def rec(n):
            if isinstance(n, dict):
                if "team" in n and isinstance(n["team"], list):
                    tkey, tid, tname = None, None, None
                    for part in n["team"]:
                        if isinstance(part, dict):
                            if "team_key" in part: tkey = part["team_key"]
                            if "team_id" in part:  tid  = str(part["team_id"])
                            if "name" in part:     tname = part["name"]
                    if tkey and (tid or tkey) and tname:
                        teams.append({"team_key": tkey, "team_id": tid or tkey, "name": tname})
                for v in n.values(): rec(v)
            elif isinstance(n, list):
                for x in n: rec(x)
        rec(data)
        uniq = {}
        for t in teams: uniq[t["team_key"]] = t
        return list(uniq.values())

    data = yahoo_get(db, user_id, f"/league/{league_id}/teams")
    teams = parse_teams_payload(data)
    if teams: return teams
    data2 = yahoo_get(db, user_id, f"/league/{league_id}/standings")
    return parse_teams_payload(data2)


def _resolve_week_mid_date(db: Session, user_id: str, league_id: str, week: int) -> str | None:
    import re
    data = yahoo_get(db, user_id, f"/league/{league_id}/scoreboard;week={week}")

    dates: List[str] = []

    def rec(n):
        if isinstance(n, dict):
            for v in n.values():
                if isinstance(v, str):
                    dates.extend(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", v))
                else:
                    rec(v)
        elif isinstance(n, list):
            for x in n:
                rec(x)

    rec(data)
    if not dates:
        return None
    uniq = sorted(set(dates))
    if len(uniq) == 1:
        return uniq[0]
    try:
        sd = datetime.fromisoformat(uniq[0])
        ed = datetime.fromisoformat(uniq[-1])
        md = sd + (ed - sd) / 2
        return md.date().isoformat()
    except Exception:
        return uniq[0]


def _get_week_roster_player_keys(
    db: Session, user_id: str, team_key: str, week: int, league_id_for_fallback: str
) -> List[str]:
    """
    Try ;week= first, then ;date=<mid_of_week>, then current roster.
    """
    data = yahoo_get(db, user_id, f"/team/{team_key}/roster;week={week}")
    keys = _parse_player_keys_from_roster_payload(data)
    if keys:
        return keys

    mid = _resolve_week_mid_date(db, user_id, league_id_for_fallback, week)
    if mid:
        data2 = yahoo_get(db, user_id, f"/team/{team_key}/roster;date={mid}")
        keys2 = _parse_player_keys_from_roster_payload(data2)
        if keys2:
            return keys2

    data3 = yahoo_get(db, user_id, f"/team/{team_key}/roster")
    return _parse_player_keys_from_roster_payload(data3)


# ========= Core: compute team totals =========

def compute_team_category_totals_week(
    db: Session,
    user_id: str,
    league_id: str,
    week: int,
    *,
    normalize: str = "totals",  # totals | per_game
) -> Tuple[str, List[str], Dict[str, Dict[str, float]]]:
    """
    Aggregate weekly category values per team for the league.
    NHL: pull team totals directly from /scoreboard;week=…
    NBA: aggregate per-player (existing path via get_players_stats_batch).
    """
    # League → detect sport
    raw_league = yahoo_get(db, user_id, f"/league/{league_id}")
    fc = raw_league.get("fantasy_content", {})
    game_key = _find_game_key(fc)
    sport = _detect_sport_from_gamekey(game_key) or "nba"

    # Settings → categories & stat_id map
    settings = yahoo_get(db, user_id, f"/league/{league_id}/settings")
    categories = _extract_categories(settings, sport)
    percent_triplets = percent_triplets_for(sport)
    percent_cats = set(percent_triplets.keys())
    stat_id_to_abbr = _build_stat_id_map(settings)

    # ---- NHL fast-path: scoreboard already contains week team totals
    if sport == "nhl":
        scoreboard = yahoo_get(db, user_id, f"/league/{league_id}/scoreboard;week={week}")
        totals = _nhl_team_totals_from_scoreboard(scoreboard, stat_id_to_abbr, sport="nhl")

        # Ensure percent cats present, compute from team numerators/denominators if needed
        out: Dict[str, Dict[str, float]] = {}
        for tid, sums in totals.items():
            final: Dict[str, float] = {}
            # percents (SV% can be on the wire; recompute when possible)
            for pcat, (num, den) in percent_triplets.items():
                num_v = float(sums.get(num, 0.0))
                den_v = float(sums.get(den, 0.0))
                if den_v > 0:
                    final[pcat] = num_v / den_v
                else:
                    # fall back if wire provided the percent (e.g., SV%)
                    if pcat in sums:
                        final[pcat] = float(sums.get(pcat, 0.0))
                    else:
                        final[pcat] = 0.0

            # counting categories (limit to league cats)
            for c in categories:
                if c in percent_cats:
                    continue
                if c in sums:
                    final[c] = float(sums[c])

            # NHL per_game not supported reliably (no GP); keep totals
            out[tid] = final

        return sport, categories, out

    # ---- NBA (and other sports): original per-player path
    teams = _get_teams(db, user_id, league_id)
    if not teams:
        return sport, categories, {}

    # Week rosters
    team_players: Dict[str, List[str]] = {}
    union_players: Set[str] = set()
    for t in teams:
        plist = _get_week_roster_player_keys(db, user_id, t["team_key"], week, league_id)
        team_players[t["team_id"]] = plist
        union_players.update(plist)

    if not union_players:
        return sport, categories, {}

    # Batch fetch player weekly stats (your existing player API)
    stat_lines = get_players_stats_batch(
        db,
        player_ids=sorted(list(union_players)),
        league_id=league_id,
        kind="week",
        week=week,
    )
    player_stats: Dict[str, Dict[str, float]] = {
        row.get("player_id"): {k: float(v or 0) for k, v in (row.get("values") or {}).items()}
        for row in (stat_lines or [])
    }

    # Aggregate → sums
    from collections import defaultdict
    team_sums: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    team_gp: Dict[str, float] = defaultdict(float)
    GP_KEYS = {"GP"}

    for team_id, plist in team_players.items():
        for pk in plist:
            row = player_stats.get(pk, {})
            gp_val = sum(float(row.get(gp, 0.0) or 0.0) for gp in GP_KEYS)
            if gp_val:
                team_gp[team_id] += gp_val
            for raw_key, val in row.items():
                cat = normalize_cat(sport, raw_key)
                if not cat:
                    continue
                team_sums[team_id][cat] += float(val or 0.0)

    # Build final values
    out: Dict[str, Dict[str, float]] = {}
    for t in teams:
        tid = t["team_id"]
        sums = team_sums[tid]
        final: Dict[str, float] = {}

        for pcat, (num, den) in percent_triplets.items():
            num_v = sums.get(num, 0.0)
            den_v = sums.get(den, 0.0)
            final[pcat] = (num_v / den_v) if den_v else 0.0

        for c in categories:
            if c in percent_cats:
                continue
            if c in sums:
                final[c] = sums[c]

        if normalize == "per_game":
            gp = team_gp.get(tid, 0.0)
            if gp > 0:
                for c in list(final.keys()):
                    if c in percent_cats:
                        continue
                    final[c] = final[c] / gp

        out[tid] = final



# ========= Ranking & power score =========

def rank_and_score(
    team_totals: Dict[str, Dict[str, float]],
    sport: str,
    *,
    punt: Optional[List[str]] = None,
    weights: Optional[Dict[str, float]] = None,
) -> Tuple[Dict[str, Dict[str, Dict[str, float]]], Dict[str, float]]:
    """
    For each category, compute z-scores and ranks.
    Return:
      ( per_team_details, power_scores )
    Where per_team_details[team_id][cat] = {"value", "z", "rank"}.
    """
    punt_set = set(punt or [])
    weights = weights or {}
    lower = lower_is_better_for(sport)

    categories = sorted({
        cat for totals in team_totals.values()
        for cat in totals.keys()
        if cat not in punt_set
    })

    cat_vals: Dict[str, List[float]] = {cat: [] for cat in categories}
    for cat in categories:
        for tid, totals in team_totals.items():
            v = float(totals.get(cat, 0.0))
            cat_vals[cat].append(-v if cat in lower else v)

    cat_stats: Dict[str, Tuple[float, float]] = {}
    for cat, vals in cat_vals.items():
        mean = sum(vals) / len(vals) if vals else 0.0
        var = sum((x - mean) ** 2 for x in vals) / len(vals) if vals else 0.0
        std = sqrt(var) if var > 0 else 1.0
        cat_stats[cat] = (mean, std)

    per_team: Dict[str, Dict[str, Dict[str, float]]] = defaultdict(dict)
    for cat in categories:
        mean, std = cat_stats[cat]
        pairs = []
        for tid, totals in team_totals.items():
            raw = float(totals.get(cat, 0.0))
            adj = -raw if cat in lower else raw
            z = (adj - mean) / std if std else 0.0
            per_team[tid][cat] = {"value": raw, "z": z}
            pairs.append((tid, adj))

        pairs.sort(key=lambda x: x[1], reverse=True)
        rank_map: Dict[str, int] = {}
        rank = 1
        prev_val = None
        for tid, val in pairs:
            if prev_val is None or val != prev_val:
                rank_map[tid] = rank
            else:
                rank_map[tid] = rank
            prev_val = val
            rank += 1

        for tid in rank_map:
            per_team[tid][cat]["rank"] = float(rank_map[tid])

    scores: Dict[str, float] = {}
    for tid in per_team.keys():
        s = 0.0
        for cat in categories:
            w = float(weights.get(cat, 1.0))
            s += per_team[tid][cat]["z"] * w
        scores[tid] = s

    return per_team, scores


# ========= Convenience facade for routes =========

def build_week_power_table_and_scores(
    db: Session,
    user_id: str,
    league_id: str,
    week: int,
    *,
    normalize: str = "totals",
    punt_csv: str = "",
) -> dict:
    sport, cats, team_totals = compute_team_category_totals_week(
        db, user_id, league_id, week, normalize=normalize
    )
    punt = [c.strip() for c in (punt_csv or "").split(",") if c.strip()]
    details, scores = rank_and_score(team_totals, sport, punt=punt)

    return {
        "league_id": league_id,
        "week": week,
        "sport": sport,
        "normalize": normalize,
        "categories": cats,
        "lower_is_better": sorted(list(lower_is_better_for(sport))),
        "percent_triplets": percent_triplets_for(sport),
        "team_totals": team_totals,
        "per_team": details,
        "power_scores": scores,
    }


# ---------- helpers: generic deep traversal ----------

def _walk(node):
    """Yield all dicts/lists in a Yahoo payload (they love nesting)."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for x in node:
            yield from _walk(x)


def _first(node, key) -> Optional[Any]:
    """Find the first dict in the structure that has 'key'."""
    for n in _walk(node):
        if isinstance(n, dict) and key in n:
            return n[key]
    return None


def _as_list(node) -> List[Any]:
    """Yahoo often stores objects under numeric keys '0','1','2' or as lists; normalize to list."""
    if isinstance(node, list):
        return node
    if isinstance(node, dict):
        # numeric-key dictionary => sort by int(key)
        numeric_keys = []
        for k in node.keys():
            try:
                numeric_keys.append(int(k))
            except Exception:
                return [node]  # not numeric-key dict
        return [node[str(i)] for i in sorted(numeric_keys) if str(i) in node]
    return [node]

# ========= Debug helpers (optional) =========

def debug_probe_week(
    db: Session, user_id: str, league_id: str, week: int
) -> dict:
    raw_league = yahoo_get(db, user_id, f"/league/{league_id}")
    fc = raw_league.get("fantasy_content", {})
    sport = _detect_sport_from_gamekey(_find_game_key(fc)) or "nba"
    teams = _get_teams(db, user_id, league_id)
    mid = _resolve_week_mid_date(db, user_id, league_id, week)
    rows = []
    for t in teams:
        players = _get_week_roster_player_keys(db, user_id, t["team_key"], week, league_id)
        rows.append({"team_id": t["team_id"], "team_key": t["team_key"], "count": len(players)})
    return {
        "league_id": league_id,
        "sport": sport,
        "week": week,
        "mid_date_resolved": mid,
        "teams_found": len(teams),
        "rows": rows,
    }


def _nhl_team_totals_from_scoreboard(
    scoreboard: dict,
    stat_id_to_abbr: Dict[str, str],
    sport: str = "nhl",
) -> Dict[str, Dict[str, float]]:
    """
    Parse /league/{id}/scoreboard;week=W payload to {team_id: {cat: value}} for NHL.
    """
    results: Dict[str, Dict[str, float]] = {}

    def add_stat(team_id: str, sid: str, value):
        try:
            v = float(value)
        except Exception:
            # Strings like ".925" are fine; others skip
            try:
                v = float(str(value).replace(",", ""))
            except Exception:
                return
        abbr = stat_id_to_abbr.get(str(sid))
        if not abbr:
            return
        cat = normalize_cat(sport, abbr)
        if not cat:
            return
        results.setdefault(team_id, {})
        results[team_id][cat] = v

    def rec(n):
        if isinstance(n, dict):
            # detect a single team node with team_id and team_stats
            if "team" in n and isinstance(n["team"], list):
                tkey, tid = None, None
                team_stats = None
                # flatten list parts
                flat: dict = {}
                for part in n["team"]:
                    if isinstance(part, list):
                        for p in part:
                            if isinstance(p, dict):
                                flat.update(p)
                    elif isinstance(part, dict):
                        flat.update(part)
                tkey = flat.get("team_key")
                tid = str(flat.get("team_id") or tkey or "")
                # team_stats is sibling of team list on the same parent
                if "team_stats" in flat:
                    team_stats = flat["team_stats"]
                elif "team_stats" in n:
                    team_stats = n["team_stats"]

                if tid and isinstance(team_stats, dict):
                    stats = team_stats.get("stats", [])
                    for s in stats:
                        node = s.get("stat") if isinstance(s, dict) else None
                        if isinstance(node, dict) and "stat_id" in node and "value" in node:
                            add_stat(tid, str(node["stat_id"]), node["value"])

            # generic recurse
            for v in n.values():
                rec(v)
        elif isinstance(n, list):
            for x in n:
                rec(x)

    rec(scoreboard)
    return results


# ---------- sport + stat mapping ----------

# Minimal, practical mapping for your NHL sample (week scoreboard). Extend as needed.
NHL_ID_TO_CAT = {
    "1": "G",
    "2": "A",
    "4": "+/-",
    "8": "PPP",
    "14": "SOG",
    "31": "HIT",
    "19": "W",
    "22": "GA",
    "23": "GAA",
    "25": "SV",
    "24": "SA",
    "26": "SV%",
    "27": "SHO",
}
# For NBA we’ll defer until games populate; leaving a typical H2H-9 template for future:
NBA_ID_TO_CAT = {
    # These IDs vary by league rules; we’ll fill dynamically later if needed.
    # Keeping empty lets the function still work (it will just yield empty when no games yet).
}

SPORT_LOWER_IS_BETTER = {
    "nhl": {"GAA", "PIM"},  # PIM not in sample week; harmless
    "nba": {"TO"},
}

SPORT_PERCENT_TRIPLETS = {
    # category -> (made, att) keys
    "nhl": {
        "SV%": ("SV", "SA"),
    },
    "nba": {
        "FG%": ("FGM", "FGA"),
        "FT%": ("FTM", "FTA"),
        "3PT%": ("3PTM", "3PTA"),
    }
}
def _sport_from_league_obj(league_obj: dict) -> str:
    game_code = league_obj.get("game_code")
    if game_code == "nhl":
        return "nhl"
    if game_code == "nba":
        return "nba"
    return str(game_code or "").lower() or "unknown"


def _cat_map_for_sport(sport: str) -> Dict[str, str]:
    if sport == "nhl":
        return NHL_ID_TO_CAT
    if sport == "nba":
        return NBA_ID_TO_CAT
    return {}


# ---------- math helpers ----------

def _zscore_series(values: List[float]) -> List[float]:
    if not values:
        return []
    if len(values) == 1:
        return [0.0]
    m = mean(values)
    s = pstdev(values)
    if s == 0:
        return [0.0 for _ in values]
    return [(v - m) / s for v in values]
def compute_week_power_ranking(
    db: Session,
    user_id: str,
    league_id: str,
    week: int,
    normalize_mode: str = "totals",     # currently only 'totals' supported; hook for future per-game
    punt: Optional[str] = None,         # one or more cat names, comma-separated
    include_names: bool = True,
) -> Dict[str, Any]:
    """
    Build weekly power ranking from Yahoo scoreboard.
    Returns:
      {
        league_id, week, sport, normalize, categories, lower_is_better,
        percent_triplets, team_totals, per_team, power_scores,
        (optional) teams, (optional) ranked
      }
    """
    # 1) fetch scoreboard
    sb = yahoo_get(db, user_id, f"/league/{league_id}/scoreboard;week={week}")

    league_obj = None
    # scoreboard response has league metadata at the top; pull first "league" object-ish
    for n in _walk(sb):
        if isinstance(n, dict) and "league_key" in n and "game_code" in n:
            league_obj = n
            break
    sport = _sport_from_league_obj(league_obj or {})

    id_to_cat = _cat_map_for_sport(sport)

    # 2) collect matchups, teams, and team_stats
    # Navigate to the "scoreboard" then "matchups"
    raw_scoreboard = _first(sb, "scoreboard") or {}
    raw_matchups = _first(raw_scoreboard, "matchups") or {}

    matchups = _as_list(raw_matchups)
    team_totals: Dict[str, Dict[str, float]] = {}      # team_id -> {cat: value}
    team_ids_seen: set[str] = set()

    for m in matchups:
        matchup = m.get("matchup") if isinstance(m, dict) else None
        if matchup is None:
            continue
        # matchup["0"] or list-like; normalize
        matchup_block = None
        if isinstance(matchup, dict):
            # pick the nested item that has "teams"
            for v in matchup.values():
                if isinstance(v, dict) and "teams" in v:
                    matchup_block = v
                    break
        elif isinstance(matchup, list):
            for v in matchup:
                if isinstance(v, dict) and "teams" in v:
                    matchup_block = v
                    break
        if not matchup_block:
            continue

        teams = _first(matchup_block, "teams") or {}
        teams_list = _as_list(teams)
        for t in teams_list:
            team = t.get("team") if isinstance(t, dict) else None
            if not team or not isinstance(team, list):
                continue

            # extract team_id and stats
            team_id: Optional[str] = None
            for part in team:
                if isinstance(part, dict) and "team_id" in part:
                    team_id = str(part["team_id"])
                    break
            if not team_id:
                # try team_key fallback
                for part in team:
                    if isinstance(part, dict) and "team_key" in part:
                        team_id = str(part["team_key"]).split(".")[-1].replace("t", "").replace(".", "")
                        break
            if not team_id:
                continue

            team_ids_seen.add(team_id)

            # find team_stats block
            team_stats_block = None
            for part in team:
                if isinstance(part, dict) and "team_stats" in part:
                    team_stats_block = part["team_stats"]
                    break
            if not team_stats_block:
                continue

            stats_arr = _first(team_stats_block, "stats") or []
            stats_arr = _as_list(stats_arr)

            # init
            accum = team_totals.setdefault(team_id, {})

            for stat_item in stats_arr:
                if not isinstance(stat_item, dict):
                    continue
                stat = stat_item.get("stat")
                if not isinstance(stat, dict):
                    continue
                sid = str(stat.get("stat_id"))
                val = stat.get("value")
                if val in (None, ""):
                    continue
                # coerce numeric; Yahoo might return strings for ratios
                try:
                    v = float(val)
                except Exception:
                    continue

                cat = id_to_cat.get(sid)  # if unknown id, we skip
                if not cat:
                    continue
                accum[cat] = v

    # 3) categories present this week
    # union of keys across all teams
    categories = sorted({cat for per in team_totals.values() for cat in per.keys()})

    # Remove punted categories (supports comma-separated list)
    punt_set = set()
    if punt:
        punt_set = {p.strip() for p in punt.split(",") if p.strip()}
        categories = [c for c in categories if c not in punt_set]

    # 4) lower_is_better filtered to cats that exist this week
    lower_all = SPORT_LOWER_IS_BETTER.get(sport, set())
    lower_is_better = [c for c in categories if c in lower_all]

    # 5) percent triplets (present for sport) – only keep the ones whose base cats exist this week
    percent_triplets_full = SPORT_PERCENT_TRIPLETS.get(sport, {})
    percent_triplets: Dict[str, Tuple[str, str]] = {}
    for pct_cat, (made, att) in percent_triplets_full.items():
        if pct_cat in categories or (made in categories and att in categories):
            percent_triplets[pct_cat] = (made, att)

    # 6) build z-scores + ranks per category
    per_team: Dict[str, Dict[str, Dict[str, float | int]]] = {}  # team_id -> cat -> {value,z,rank}
    for cat in categories:
        values: List[Tuple[str, float]] = []  # (team_id, value)
        for tid in team_ids_seen:
            v = team_totals.get(tid, {}).get(cat)
            if v is not None:
                values.append((tid, float(v)))

        if not values:
            # no data at all for this category this week
            continue

        # compute z
        ordered_tids, vec = zip(*values)
        zs = _zscore_series(list(vec))

        # invert sign for lower_is_better categories so that "better" is always higher
        invert = cat in lower_is_better
        if invert:
            zs = [-z for z in zs]

        # ranks (1 = best)
        # sort by (z desc, value desc) so ties are deterministic
        rank_sorted = sorted(
            [(i, ordered_tids[i], zs[i], vec[i]) for i in range(len(zs))],
            key=lambda t: (t[2], t[3]),
            reverse=True,
        )
        ranks: Dict[str, int] = {}
        for rank_idx, (_, tid, _, _) in enumerate(rank_sorted, start=1):
            ranks[tid] = rank_idx

        # write per_team entries
        for i, tid in enumerate(ordered_tids):
            per_team.setdefault(tid, {})
            per_team[tid][cat] = {
                "value": vec[i],
                "z": zs[i],
                "rank": ranks[tid],
            }

    # 7) power score = sum of z across categories you actually computed for the team
    power_scores: Dict[str, float] = {}
    for tid in team_ids_seen:
        total = 0.0
        for cat in categories:
            z = per_team.get(tid, {}).get(cat, {}).get("z")
            if isinstance(z, (int, float)):
                total += float(z)
        power_scores[tid] = total

    # 8) optional: team names + ranked array
    id_to_name: Dict[str, str] = {}
    ranked: List[Dict[str, Any]] = []
    if include_names:
        try:
            teams_dir = _get_teams(db, user_id, league_id)
        except Exception:
            teams_dir = []
        id_to_name = {t["team_id"]: t["name"] for t in teams_dir}
        ranking = sorted(power_scores.items(), key=lambda kv: kv[1], reverse=True)
        ranked = [
            {"rank": i + 1, "team_id": tid, "team": id_to_name.get(tid, tid), "score": score}
            for i, (tid, score) in enumerate(ranking)
        ]

    # 9) response
    return {
        "league_id": league_id,
        "week": week,
        "sport": sport,
        "normalize": normalize_mode,
        "categories": categories,
        "lower_is_better": lower_is_better,
        "percent_triplets": percent_triplets,
        "team_totals": team_totals,
        "per_team": per_team,
        "power_scores": power_scores,
        **({"teams": id_to_name, "ranked": ranked} if include_names else {}),
    }