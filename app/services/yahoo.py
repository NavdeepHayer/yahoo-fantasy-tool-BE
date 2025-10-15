from typing import Any, List, Optional, Tuple
from sqlalchemy.orm import Session

# Public OAuth pieces (unchanged)
from app.services.yahoo_oauth import (
    AUTH_SCOPE,
    build_oauth,
    get_authorization_url,
    exchange_token,
    get_latest_token,
    refresh_token,
)

# Low-level HTTP client (unchanged)
from app.services.yahoo_client import yahoo_get as yahoo_raw_get  # keep name used by code paths

# Parsers (re-export underscored for debug back-compat)
from app.services.yahoo_parsers import (
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

# Re-route to split modules
from app.services.leagues import get_leagues, _fetch_league_settings, _get, _as_list
from app.services.teams import get_teams_for_user, _find_my_team_key_from_teams_payload
from app.services.roster import get_roster_for_user
from app.services.matchups import (
    get_my_weekly_matchups,
    _get_my_guid,
    _get_my_team_key_for_league,
    _get_league_settings_meta,
    _get_stat_id_map,
    _find_my_team_key_from_scoreboard_payload,
)
from app.services.users import get_current_user_profile

# Tiny passthrough to keep the old yahoo_raw_get(db, user_id, path, params) behavior if code calls it.
def yahoo_raw_get(db: Session, user_id: str, path: str, params: Optional[dict] = None) -> dict:
    return yahoo_get(db, user_id, path, params or {})

# Keep direct import of client under original name for callers that used it
from app.services.yahoo_client import yahoo_get  # noqa: E402  (after alias above)
