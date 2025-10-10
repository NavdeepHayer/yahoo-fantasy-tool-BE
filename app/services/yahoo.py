from typing import Any, Dict, List, Tuple, Optional
from sqlalchemy.orm import Session

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
)

# Back-compat: some debug routes import these with underscores
_parse_leagues = parse_leagues       # noqa
_parse_teams = parse_teams           # noqa
_parse_roster = parse_roster         # noqa


# ---- Public service API (same signatures/behavior as before) ----
def _fetch_league_settings(db: Session, user_id: str, league_keys: List[str]) -> dict[str, List[str]]:
    """
    Fetch stat category display_names for given league keys.
    Returns mapping league_key -> [categories...].
    """
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
    """
    Return leagues for the authed user, filterable by sport/season or specific game_key.
    Always enriches categories via /leagues/.../settings.
    """
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
            return []  # no games for this account

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


def get_teams_for_user(db: Session, user_id: str, league_id: str) -> List[dict]:
    """
    Public wrapper with automatic endpoint fallback.
    """
    if settings.YAHOO_FAKE_MODE:
        return [
            {"id": f"{league_id}.t.1", "name": "Nav’s Team", "manager": "Nav"},
            {"id": f"{league_id}.t.2", "name": "Rival Squad", "manager": "Alex"},
        ]

    payload = yahoo_get(db, user_id, f"/league/{league_id}/teams")
    teams = parse_teams(payload, league_id)
    if teams:
        return teams

    payload2 = yahoo_get(db, user_id, f"/leagues;league_keys={league_id}/teams")
    teams2 = parse_teams(payload2, league_id)
    return teams2


def get_roster_for_user(db: Session, user_id: str, team_id: str, date: Optional[str] = None) -> dict:
    """
    Public wrapper with automatic endpoint fallback.
    Returns the same shape as before: { team_id, date, players: [ {player_id, name, positions, status} ] }.
    """
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


# ---- tiny local helpers reused by get_leagues (copied from old file to avoid circular import) ----
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
