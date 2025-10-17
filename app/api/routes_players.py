# app/api/routes_players.py

from typing import Annotated, List, Optional, Literal, Tuple, Dict
import heapq

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.deps import get_current_user
from app.schemas.player import Player, PlayerSearchResponse
from app.schemas.stats import PlayerStatLine, TeamWeeklyStats
from app.services.yahoo.players import (
    search_players,            # league-scoped search
    search_players_global,     # league-agnostic (game-scoped) search
    get_player,                # fetch single player
    get_player_stats,          # fetch single player stats (league-context for cats)
    get_team_weekly_totals,    # team weekly aggregation (league-context)
    get_players_stats_batch,   # batch stats
)

# cache utilities (your existing ones)
from app.services.cache import cache_route, key_tuple

# Routers
router = APIRouter(prefix="/players", tags=["players"])
league_router = APIRouter(prefix="/league", tags=["league-stats"])

# ------------------------------------------------------------
# Quick debug endpoints (optional)
# ------------------------------------------------------------
@router.get("/_ping", tags=["debug"])
def _ping():
    return {"ok": True, "who": "players-router"}

@router.get("/_echo", tags=["debug"])
def _echo(
    league_id: Annotated[str, Query(description="required")],
    q: Annotated[str | None, Query(description="optional")] = None,
):
    return {"league_id": league_id, "q": q}

# ------------------------------------------------------------
# STATIC ROUTES FIRST (avoid collisions with dynamic paths)
# ------------------------------------------------------------

@router.get(
    "/search",
    response_model=PlayerSearchResponse,
    summary="Search players (league-scoped)",
    description=(
        "Search the league's player universe (honors eligibility and allows FA/W/T filters). "
        "Useful for waiver views and roster tools bound to a league."
    ),
)
@cache_route(
    namespace="players_search",
    ttl_seconds=10 * 60,  # 10m
    key_builder=lambda *a, **k: key_tuple(
        "search",
        k["guid"],
        k["league_id"],
        k.get("q") or "",
        k.get("position") or "",
        k.get("status") or "",
        k.get("page") or 1,
        k.get("per_page") or 25,
    ),
)
def search_players_route(
    league_id: Annotated[str, Query(description="Yahoo league key, e.g. 466.l.17802")],
    q: Annotated[str | None, Query(description="Free-text search (name/team)")] = None,
    position: Annotated[str | None, Query(description="e.g. PG, SG, SF, PF, C (NBA) or LW, C, RW, D (NHL)")] = None,
    status: Annotated[str | None, Query(description="FA | W | T (free agent, waivers, taken)")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=50)] = 25,
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
):
    items, next_page = search_players(
        db, league_id, q=q, position=position, status=status, page=page, per_page=per_page
    )
    return PlayerSearchResponse(items=items, page=page, per_page=per_page, next_page=next_page)


@router.get(
    "/search-global",
    response_model=PlayerSearchResponse,
    summary="Search players (global/game-scoped, no league required)",
    description=(
        "Search a sport's global player pool (no league required). "
        "Pass either `game_key` (e.g., 466 for NBA 2025, 465 for NHL 2025) OR `sport` + optional `season`."
    ),
)
@cache_route(
    namespace="players_search_global",
    ttl_seconds=30 * 60,  # 30m
    key_builder=lambda *a, **k: key_tuple(
        "search_global",
        k["guid"],
        k.get("q") or "",
        k.get("position") or "",
        k.get("page") or 1,
        k.get("per_page") or 25,
        k.get("game_key") or "",
        k.get("sport") or "",
        k.get("season") or "",
    ),
)
def search_players_global_route(
    q: Annotated[str | None, Query(description="Free-text search (name/team)")] = None,
    position: Annotated[str | None, Query(description="e.g. PG, SG, C (NBA) / LW, C, RW, D (NHL)")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=50)] = 25,
    game_key: Annotated[str | None, Query(description="Yahoo game key (e.g., 466=NBA 2025, 465=NHL 2025)")] = None,
    sport: Annotated[str | None, Query(description="nba | nhl | mlb | nfl")] = None,
    season: Annotated[str | None, Query(description="e.g., 2025")] = None,
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
):
    items, next_page = search_players_global(
        db,
        q=q,
        position=position,
        page=page,
        per_page=per_page,
        sport=sport,
        season=season,
        game_key=game_key,
    )
    return PlayerSearchResponse(items=items, page=page, per_page=per_page, next_page=next_page)


# ------------------------------------------------------------
# RANKED SCAN (STATIC PATH): league-scoped search with stat sort/filters
# ------------------------------------------------------------
from typing import Tuple, Dict, List, Any
import heapq
from datetime import date, timedelta

# --- helpers ---
def _parse_sort_list(sort_by: List[str]) -> List[Tuple[str, int]]:
    out: List[Tuple[str, int]] = []
    for s in sort_by:
        s = (s or "").strip()
        if not s:
            continue
        if ":" in s:
            k, d = s.split(":", 1)
            out.append((k.strip().upper(), -1 if d.strip() == "-1" else 1))
        else:
            out.append((s.upper(), -1))
    return out

def _parse_thresholds(pairs: List[str]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for s in pairs:
        if not s or ":" not in s:
            continue
        k, v = s.split(":", 1)
        k = k.strip().upper()
        try:
            out[k] = float(v)
        except Exception:
            continue
    return out

_STAT_ALIASES = {"3PT": "3PTM", "3PM": "3PTM", "3PTM": "3PTM"}
def _normalize_key(k: str) -> str:
    k = (k or "").strip().upper()
    return _STAT_ALIASES.get(k, k)

def _get_val(vals: Dict[str, Any], k: str) -> float:
    nk = _normalize_key(k)
    v = vals.get(nk, vals.get(k, 0))
    try:
        return float(v)
    except Exception:
        return 0.0

def _passes_filters(vals: Dict[str, Any], gte_map: Dict[str, float], lte_map: Dict[str, float]) -> bool:
    for k, v in (gte_map or {}).items():
        if _get_val(vals, k) < float(v):
            return False
    for k, v in (lte_map or {}).items():
        if _get_val(vals, k) > float(v):
            return False
    return True

def _rank_tuple(vals: Dict[str, Any], sort_keys: List[Tuple[str, int]]) -> Tuple:
    key = []
    for cat, dirn in sort_keys:
        v = _get_val(vals, cat)
        key.append(-v if dirn == -1 else v)
    return tuple(key)

def _infer_sport_from_league(league_id: str) -> str:
    gk = (league_id or "").split(".l.", 1)[0]
    return "nhl" if gk == "465" else ("nba" if gk == "466" else "nba")

def _default_sort_for_league(league_id: str) -> List[Tuple[str, int]]:
    sport = _infer_sport_from_league(league_id)
    if sport == "nhl":
        return [("G", -1), ("SOG", -1), ("PTS", -1)]
    return [("PTS", -1), ("AST", -1), ("REB", -1)]

def _to_payload(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {}
    if hasattr(obj, "dict") and callable(getattr(obj, "dict")):
        try:
            return obj.dict()
        except Exception:
            pass
    if hasattr(obj, "model_dump") and callable(getattr(obj, "model_dump")):
        try:
            return obj.model_dump()
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        try:
            return dict(obj.__dict__)
        except Exception:
            pass
    if isinstance(obj, dict):
        return dict(obj)
    return {"value": obj}

def _get_pid_from_item_dict(d: Dict[str, Any]) -> str | None:
    pid = d.get("player_id") or d.get("player_key")
    return str(pid) if pid else None

def _iso(d: date) -> str:
    return d.isoformat()

def _resolve_league_meta_via_debug(db: Session, league_id: str) -> Dict[str, Any]:
    """
    Cheap way to grab league current_date & matchup_week using your existing raw proxy.
    Uses a 1-count players page (fast) and reads league fields from the payload.
    Fallbacks to today/week=1 if anything goes wrong.
    """
    try:
        from app.services.yahoo.client import yahoo_get
        raw = yahoo_get(db, get_current_user(db), f"/league/{league_id}/players;count=1")
        # raw is the raw Yahoo JSON. Extract league block safely:
        league = (raw or {}).get("fantasy_content", {}).get("league", [])
        if isinstance(league, list) and league:
            meta = league[0] if isinstance(league[0], dict) else {}
            current_date = meta.get("current_date")
            matchup_week = meta.get("matchup_week")
            return {
                "current_date": current_date,
                "matchup_week": int(matchup_week) if str(matchup_week or "").isdigit() else None,
            }
    except Exception:
        pass
    return {"current_date": date.today().isoformat(), "matchup_week": None}

def _resolve_window_to_dates(db: Session, league_id: str, kind: str,
                             date_from: str | None, date_to: str | None) -> Tuple[str | None, str | None]:
    """
    For last7/last14/last30 we translate to a date range anchored at league.current_date.
    If date_from/date_to already provided, we keep them.
    """
    if date_from and date_to:
        return date_from, date_to
    if kind not in {"last7", "last14", "last30"}:
        return date_from, date_to

    meta = _resolve_league_meta_via_debug(db, league_id)
    anchor_s = meta.get("current_date") or date.today().isoformat()
    try:
        anchor = date.fromisoformat(anchor_s)
    except Exception:
        anchor = date.today()

    days = 7 if kind == "last7" else (14 if kind == "last14" else 30)
    start = anchor - timedelta(days=days - 1)
    return _iso(start), _iso(anchor)

def _resolve_week(db: Session, league_id: str, requested_week: int | None) -> int | None:
    if requested_week and requested_week >= 1:
        return requested_week
    meta = _resolve_league_meta_via_debug(db, league_id)
    return meta.get("matchup_week") or 1

@router.get(
    "/search/ranked",
    response_model=PlayerSearchResponse,
    summary="Search players (league-scoped, ranked by stats or Yahoo default)",
    description=(
        "No sort/filters → returns the first `per_page` results in Yahoo's default order (fast path). "
        "With sort/filters → scans pages, batch-fetches stats (translating last7/14/30 to date ranges), "
        "applies thresholds and ranks. If a rolling window returns nothing, falls back to season."
    ),
)
@cache_route(
    namespace="players_search_ranked",
    ttl_seconds=60,
    key_builder=lambda *a, **k: key_tuple(
        "search_ranked",
        k["guid"],
        k["league_id"],
        k.get("q") or "",
        k.get("position") or "",
        k.get("status") or "",
        k.get("per_page") or 25,
        "|".join(k.get("sort_by") or []) if isinstance(k.get("sort_by"), list) else str(k.get("sort_by") or ""),
        "|".join(k.get("gte") or []) if isinstance(k.get("gte"), list) else str(k.get("gte") or ""),
        "|".join(k.get("lte") or []) if isinstance(k.get("lte"), list) else str(k.get("lte") or ""),
        k.get("kind") or "season",
        str(k.get("week") or ""),
        str(k.get("date_from") or ""),
        str(k.get("date_to") or ""),
        str(k.get("scan_pages") or 6),
        str(k.get("cursor_next_page") or 1),
    ),
)
def search_players_ranked_route(
    league_id: Annotated[str, Query(description="Yahoo league key, e.g. 466.l.17802")],
    q: Annotated[str | None, Query(description="Free-text search (name/team)")] = None,
    position: Annotated[str | None, Query(description="e.g. PG, SG, SF, PF, C (NBA) or LW, C, RW, D (NHL)")] = None,
    status: Annotated[str | None, Query(description="FA | W | T (free agent, waivers, taken)")] = None,
    per_page: Annotated[int, Query(ge=1, le=50)] = 25,

    # --- stat context ---
    kind: Annotated[Literal["season", "week", "last7", "last14", "last30", "date_range"] | None, Query()] = None,
    week: Annotated[int | None, Query(ge=1)] = None,
    date_from: Annotated[str | None, Query(description="YYYY-MM-DD")] = None,
    date_to: Annotated[str | None, Query(description="YYYY-MM-DD")] = None,

    # --- ranking + thresholds ---
    sort_by: Annotated[List[str], Query(description="Repeat: cat:dir. Ex: sort_by=PTS:-1&sort_by=SOG:-1")] = [],
    gte: Annotated[List[str], Query(description="Repeat: cat:value. Ex: gte=BLK:1&gte=SOG:3")] = [],
    lte: Annotated[List[str], Query(description="Repeat: cat:value. Ex: lte=TO:2")] = [],

    # --- scanning controls ---
    scan_pages: Annotated[int, Query(ge=1, le=40, description="How many Yahoo pages to scan server-side (×25)")] = 8,
    cursor_next_page: Annotated[int | None, Query(ge=1, description="Resume scan starting page (1-based)")] = None,

    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
):
    parsed_sort = _parse_sort_list(sort_by)
    no_filters = (not parsed_sort) and (not gte) and (not lte)

    # ---------- Fast path: Yahoo default ordering ----------
    if no_filters:
        collected: List[dict] = []
        current_page = cursor_next_page or 1
        page_size = 25
        scanned_pages = 0
        while len(collected) < per_page and scanned_pages < scan_pages:
            page_items, next_page = search_players(
                db, league_id, q=q, position=position, status=status, page=current_page, per_page=page_size
            )
            if not page_items:
                break
            for p in page_items:
                payload = _to_payload(p)
                collected.append(payload)
                if len(collected) >= per_page:
                    break
            scanned_pages += 1
            current_page += 1
            if next_page is None:
                break
        return PlayerSearchResponse(items=collected[:per_page], page=1, per_page=per_page, next_page=None)  # type: ignore

    # ---------- Ranked scan with stats ----------
    if not parsed_sort:
        parsed_sort = _default_sort_for_league(league_id)

    def run_scan(_kind: str, _sort_keys: List[Tuple[str, int]]):
        # translate rolling windows to date range
        _df, _dt = _resolve_window_to_dates(db, league_id, _kind, date_from, date_to)
        _week = _resolve_week(db, league_id, week) if _kind == "week" else None

        gte_map = _parse_thresholds(gte)
        lte_map = _parse_thresholds(lte)

        top_k: List[Tuple[Tuple, int, dict]] = []
        idx_counter = 0
        scanned_pages = 0
        current_page = cursor_next_page or 1
        page_size = 25

        while scanned_pages < scan_pages:
            page_items, next_page = search_players(
                db, league_id, q=q, position=position, status=status, page=current_page, per_page=page_size
            )
            if not page_items:
                break

            dict_items: List[Dict[str, Any]] = []
            ids: List[str] = []
            for p in page_items:
                d = _to_payload(p)
                dict_items.append(d)
                pid = _get_pid_from_item_dict(d)
                if pid:
                    ids.append(pid)

            by_id: Dict[str, Dict[str, Any]] = {}
            if ids:
                # Map our kind to the service call:
                svc_kind = "season"
                svc_kwargs: Dict[str, Any] = {}
                if _kind == "season":
                    svc_kind = "season"
                elif _kind == "week":
                    svc_kind = "week"
                    svc_kwargs["week"] = _week or 1
                elif _kind in {"last7", "last14", "last30", "date_range"}:
                    svc_kind = "date_range"
                    svc_kwargs["date_from"] = _df
                    svc_kwargs["date_to"] = _dt

                stat_lines: List[Dict[str, Any]] = get_players_stats_batch(
                    db,
                    ids,
                    league_id=league_id,
                    kind=svc_kind,
                    **svc_kwargs,
                ) or []
                for s in stat_lines:
                    if not isinstance(s, dict):
                        continue
                    sid = str(s.get("player_id") or "")
                    if not sid:
                        continue
                    by_id[sid] = (s.get("values") or {})

            for d in dict_items:
                pid = _get_pid_from_item_dict(d)
                if not pid:
                    continue
                vals = by_id.get(pid, {})
                if (gte or lte) and not _passes_filters(vals, gte_map, lte_map):
                    continue

                rk = _rank_tuple(vals, _sort_keys) if _sort_keys else (0,)
                payload = dict(d)
                payload["__rank_vals"] = vals
                heapq.heappush(top_k, (rk, idx_counter, payload))
                idx_counter += 1
                if len(top_k) > per_page * 2:
                    heapq.heappop(top_k)

            scanned_pages += 1
            current_page += 1
            if next_page is None:
                break

        best = [heapq.heappop(top_k)[2] for _ in range(len(top_k))]
        best.reverse()
        def final_key(obj: dict):
            rk = _rank_tuple(obj.get("__rank_vals", {}), _sort_keys) if _sort_keys else (0,)
            return (rk, obj.get("name") or "")
        best.sort(key=final_key)
        items = best[:per_page]
        for it in items:
            it.pop("__rank_vals", None)
        cursor = {"scanned": scanned_pages, "next_page": current_page}
        return items, cursor

    primary_kind = (kind or "season")
    items, cursor = run_scan(primary_kind, parsed_sort)

    if not items and primary_kind in {"last7", "last14", "last30", "week", "date_range"}:
        # graceful fallback
        items, cursor = run_scan("season", parsed_sort)

    return PlayerSearchResponse(items=items, page=1, per_page=per_page, next_page=None, cursor=cursor)  # type: ignore

# ------------------------------------------------------------
# DYNAMIC ROUTES — safe prefix to avoid collisions
# ------------------------------------------------------------

@router.get(
    "/by-id/{player_id}",
    response_model=Player,
    summary="Get player by ID (optional league context)",
    description=(
        "Fetch a single player profile by player_id. "
        "If `league_id` is provided, eligibility may be enriched with league context."
    ),
)
@cache_route(
    namespace="player_profile",
    ttl_seconds=12 * 60 * 60,  # 12h
    key_builder=lambda *a, **k: key_tuple(
        "player", k["guid"], k["player_id"], k.get("league_id") or ""
    ),
)
def get_player_by_id_route(
    player_id: str,
    league_id: Annotated[str | None, Query(description="Optional league key for eligibility context")] = None,
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
):
    return get_player(db, player_id, league_id=league_id)


@router.get(
    "/by-id/{player_id}/stats",
    response_model=List[PlayerStatLine],
    summary="Get player stats (league-category aware)",
    description=(
        "Return stat lines keyed to the league's active category display keys. "
        "Supports kind=season|week|last7|last14|last30|date_range."
    ),
)
@cache_route(
    namespace="player_stats",
    ttl_seconds=2 * 60,  # 2m
    key_builder=lambda *a, **k: key_tuple(
        "stats",
        k["guid"],
        k["player_id"],
        k["league_id"],
        k.get("kind") or "season",
        k.get("season") or "",
        k.get("week") or "",
        k.get("date_from") or "",
        k.get("date_to") or "",
    ),
)
def get_player_stats_by_id_route(
    player_id: str,
    league_id: Annotated[str, Query(description="League key determines category mapping/scoring context")],
    season: Annotated[str | None, Query(description="e.g., 2025")] = None,
    week: Annotated[int | None, Query(description="Matchup/week # for H2H weekly")] = None,
    date_from: Annotated[str | None, Query(description="YYYY-MM-DD")] = None,
    date_to: Annotated[str | None, Query(description="YYYY-MM-DD")] = None,
    kind: Annotated[Literal["season", "week", "last7", "last14", "last30", "date_range"], Query()] = "season",
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
):
    return get_player_stats(
        db,
        player_id,
        league_id=league_id,
        kind=kind,
        season=season,
        week=week,
        date_from=date_from,
        date_to=date_to,
    )


# --------------------------
# Batch stats endpoint
# --------------------------

@router.get(
    "/stats/batch",
    response_model=List[PlayerStatLine],
    summary="Batch player stats (league-category aware)",
    description=(
        "Fetch stats for many players at once. Supports kind=season|week|last7|last14|last30|date_range. "
        "Internally chunks to Yahoo limits and aggregates per-day when needed."
    ),
)
@cache_route(
    namespace="player_stats_batch",
    ttl_seconds=2 * 60,  # 2m
    key_builder=lambda *a, **k: key_tuple(
        "stats_batch",
        k["guid"],
        k["league_id"],
        ",".join(sorted(k["player_ids"])) if isinstance(k.get("player_ids"), list) else str(k.get("player_ids") or ""),
        k.get("kind") or "season",
        str(k.get("season") or ""),
        str(k.get("week") or ""),
        str(k.get("date_from") or ""),
        str(k.get("date_to") or ""),
        str(k.get("through_date") or ""),
    ),
)
def get_player_stats_batch_route(
    league_id: Annotated[str, Query(description="League key determines category mapping/scoring context")],
    player_ids: Annotated[List[str], Query(description="Repeatable param: ?player_ids=465.p.4240&player_ids=465.p.4064 ...")],
    season: Annotated[str | None, Query()] = None,
    week: Annotated[int | None, Query(ge=1)] = None,
    date_from: Annotated[str | None, Query(description="YYYY-MM-DD")] = None,
    date_to: Annotated[str | None, Query(description="YYYY-MM-DD")] = None,
    through_date: Annotated[str | None, Query(description="YYYY-MM-DD (anchor for lastN windows; defaults to league current_date)")] = None,
    kind: Annotated[Literal["season", "week", "last7", "last14", "last30", "date_range"], Query()] = "season",
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
):
    return get_players_stats_batch(
        db,
        player_ids,
        league_id=league_id,
        kind=kind,
        season=season,
        week=week,
        date_from=date_from,
        date_to=date_to,
        through_date=through_date,
    )


# ------------------------------------------------------------
# OPTIONAL BACK-COMPAT ALIASES (hidden from docs)
# ------------------------------------------------------------

@router.get("/{player_id}", response_model=Player, include_in_schema=False)
@cache_route(
    namespace="player_profile",
    ttl_seconds=12 * 60 * 60,  # 12h
    key_builder=lambda *a, **k: key_tuple(
        "player_alias", k["guid"], k["player_id"], k.get("league_id") or ""
    ),
)
def _alias_get_player_route(
    player_id: str,
    league_id: Annotated[str | None, Query()] = None,
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
):
    return get_player(db, player_id, league_id=league_id)


@router.get("/{player_id}/stats", response_model=List[PlayerStatLine], include_in_schema=False)
@cache_route(
    namespace="player_stats",
    ttl_seconds=2 * 60,  # 2m
    key_builder=lambda *a, **k: key_tuple(
        "stats_alias",
        k["guid"],
        k["player_id"],
        k["league_id"],
        k.get("kind") or "season",
        k.get("season") or "",
        k.get("week") or "",
        k.get("date_from") or "",
        k.get("date_to") or "",
    ),
)
def _alias_get_player_stats_route(
    player_id: str,
    league_id: Annotated[str, Query()],
    season: Annotated[str | None, Query()] = None,
    week: Annotated[int | None, Query()] = None,
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
    kind: Annotated[str, Query(pattern="^(season|week|last7|last14|last30|date_range)$")] = "season",
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
):
    return get_player_stats(
        db,
        player_id,
        league_id=league_id,
        kind=kind,
        season=season,
        week=week,
        date_from=date_from,
        date_to=date_to,
    )


# ------------------------------------------------------------
# LEAGUE STATS/AGGREGATIONS
# ------------------------------------------------------------

@league_router.get(
    "/team/{team_id}/weekly-stats",
    response_model=TeamWeeklyStats,
    summary="Team weekly totals (league categories)",
    description="Aggregate a team's weekly totals across the league's active categories.",
)
@cache_route(
    namespace="team_weekly_stats",
    ttl_seconds=2 * 60,  # 2m
    key_builder=lambda *a, **k: key_tuple(
        "team_weekly",
        k["guid"],
        k["league_id"],
        k["team_id"],
        k["week"],
    ),
)
def team_weekly_stats_route(
    team_id: str,
    league_id: Annotated[str, Query(description="League key that defines the categories")],
    week: Annotated[int, Query(ge=1, description="Matchup/week number")],
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
):
    return get_team_weekly_totals(db, league_id=league_id, team_id=team_id, week=week)


@router.get("/debug/raw", tags=["debug"])
def debug_raw_yahoo(
    path: Annotated[str, Query(description="Yahoo API path starting with '/' e.g. /game/466/players or /league/466.l.17802/players;player_keys=466.p.4244/stats;type=season")],
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
):
    """
    ⚠️ Debug only: returns raw Yahoo API response for any path.
    Example:
      /players/debug/raw?path=/game/466/players;start=0;count=5
    """
    from app.services.yahoo.client import yahoo_get
    from app.db.models import OAuthToken

    tok = db.query(OAuthToken).order_by(OAuthToken.created_at.desc()).first()
    user_id = getattr(tok, "user_id", None) or getattr(tok, "xoauth_yahoo_guid", None)
    if not user_id:
        return {"error": "no active Yahoo token found"}

    raw = yahoo_get(db, user_id, path)
    return raw
