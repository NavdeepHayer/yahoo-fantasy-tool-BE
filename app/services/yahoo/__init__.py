"""
Yahoo service package: drop-in replacement for the old `app.services.yahoo` module.
"""

from typing import Optional
from sqlalchemy.orm import Session

# --- OAuth (your renamed module paths) ---
from app.services.yahoo.oauth import (
    AUTH_SCOPE,
    build_oauth,
    get_authorization_url,
    exchange_token,
    get_latest_token,
    refresh_token,
)

# --- Low-level HTTP client (your renamed module path) ---
from app.services.yahoo.client import yahoo_get as _yahoo_get

def yahoo_raw_get(db: Session, user_id: str, path: str, params: Optional[dict] = None) -> dict:
    """Back-compat helper some debug routes expect."""
    return _yahoo_get(db, user_id, path, params or {})

# --- Parsers (your renamed module path) ---
from app.services.yahoo.parsers import (
    parse_leagues,
    parse_teams,
    parse_roster,
    parse_scoreboard_min,
    select_matchup_for_team,
    parse_scoreboard_enriched,
)
_parse_leagues = parse_leagues
_parse_teams = parse_teams
_parse_roster = parse_roster

# --- Public API re-exports from submodules (all siblings in this package) ---
from .leagues import get_leagues, _fetch_league_settings, _get, _as_list
from .teams import get_teams_for_user, _find_my_team_key_from_teams_payload
from .roster import get_roster_for_user
from .matchups import (
    get_my_weekly_matchups,
    _get_my_guid,
    _get_my_team_key_for_league,
    _get_league_settings_meta,
    _get_stat_id_map,
    _find_my_team_key_from_scoreboard_payload,
)
from .users import get_current_user_profile
from .free_agents import search_free_agents
from .scoreboard import get_scoreboard

# Back-compat alias if older code imports this name:
upsert_user_from_yahoo = get_current_user_profile

from .client import yahoo_get, yahoo_raw_get 

from .players import (
    search_players,
    get_player,
    get_player_stats,
    get_team_weekly_totals,
)
