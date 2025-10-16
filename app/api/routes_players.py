from __future__ import annotations

from typing import List, Optional, Literal
from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.deps import get_current_user  # <-- ADD
from app.schemas.player import Player, PlayerSearchResponse
from app.schemas.stats import PlayerStatLine, TeamWeeklyStats
from app.services.yahoo.players import (
    search_players,            # league-scoped search
    search_players_global,     # league-agnostic (game-scoped) search
    get_player,                # fetch single player
    get_player_stats,          # fetch single player stats (league-context for cats)
    get_team_weekly_totals,    # team weekly aggregation (league-context)
)

# 🆕 cache utilities (USE YOUR EXISTING ONES)
from app.services.cache import cache_route, key_tuple

# One router for player-centric endpoints
router = APIRouter(prefix="/players", tags=["players"])
# Separate router for league metrics/aggregation
league_router = APIRouter(prefix="/league", tags=["league-stats"])


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
        "search",                      # namespace key
        k["guid"],                     # per-user cache
        k["league_id"],                # league universe
        k.get("q") or "",
        k.get("position") or "",
        k.get("status") or "",
        k.get("page") or 1,
        k.get("per_page") or 25,
    ),
)
def search_players_route(
    league_id: str = Query(..., description="Yahoo league key, e.g. 466.l.17802"),
    q: Optional[str] = Query(None, description="Free-text search (name/team)"),
    position: Optional[str] = Query(None, description="e.g. PG, SG, SF, PF, C (NBA) or LW, C, RW, D (NHL)"),
    status: Optional[str] = Query(None, description="FA | W | T (free agent, waivers, taken)"),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=50),
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
    response: Response = None,
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
    q: Optional[str] = Query(None, description="Free-text search (name/team)"),
    position: Optional[str] = Query(None, description="e.g. PG, SG, C (NBA) / LW, C, RW, D (NHL)"),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=50),
    game_key: Optional[str] = Query(None, description="Yahoo game key (e.g., 466=NBA 2025, 465=NHL 2025)"),
    sport: Optional[str] = Query(None, description="nba | nhl | mlb | nfl"),
    season: Optional[str] = Query(None, description="e.g., 2025"),
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
    response: Response = None,
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
# DYNAMIC ROUTES — use a safe prefix to avoid collisions
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
    league_id: Optional[str] = Query(None, description="Optional league key for eligibility context"),
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
    response: Response = None,
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
    ttl_seconds=2 * 60,  # 2m (tune if you want: see note below)
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
    league_id: str = Query(..., description="League key determines category mapping/scoring context"),
    season: Optional[str] = Query(None, description="e.g., 2025"),
    week: Optional[int] = Query(None, description="Matchup/week # for H2H weekly"),
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD"),
    kind: Literal["season", "week", "last7", "last14", "last30", "date_range"] = Query("season", 
                                                                                   pattern="^(season|week|last7|last14|last30|date_range)$"),
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
):
    """
    Handles fetching stats for a player. You can specify the 'kind' (season, week, last7, etc.), and also optionally specify
    the 'week', 'season', or 'date_from' and 'date_to' ranges.
    """
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
# OPTIONAL BACK-COMPAT ALIASES (hidden from docs)
# These call the same service functions; caching on primary routes
# handles most FE paths. If FE still hits these, we can add caching too.
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
    league_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
    response: Response = None,
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
    league_id: str = Query(...),
    season: Optional[str] = Query(None),
    week: Optional[int] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    kind: str = Query("season", pattern="^(season|week|last7|last14|last30|date_range)$"),
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
    response: Response = None,
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
    league_id: str = Query(..., description="League key that defines the categories"),
    week: int = Query(..., ge=1, description="Matchup/week number"),
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
    response: Response = None,
):
    return get_team_weekly_totals(db, league_id=league_id, team_id=team_id, week=week)


@router.get("/debug/raw", tags=["debug"])
def debug_raw_yahoo(
    path: str = Query(..., description="Yahoo API path starting with '/' e.g. /game/466/players or /league/466.l.17802/players;player_keys=466.p.4244/stats;type=season"),
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
