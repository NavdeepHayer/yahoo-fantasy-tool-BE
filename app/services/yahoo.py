import json
import requests
from typing import Any, Dict, List, Tuple, Optional
from requests_oauthlib import OAuth2Session
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import OAuthToken

# ---- OAuth / Yahoo config ----
# Use read-only scope while developing (write usually requires extra approval)
AUTH_SCOPE = ["fspt-r"]


# ---- Small utils ----
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


# ---- OAuth helpers ----
def build_oauth(token: dict | None = None) -> OAuth2Session:
    return OAuth2Session(
        client_id=settings.YAHOO_CLIENT_ID,
        redirect_uri=settings.YAHOO_REDIRECT_URI,
        scope=AUTH_SCOPE,
        token=token,
        auto_refresh_url=settings.YAHOO_TOKEN_URL,
        auto_refresh_kwargs={
            "client_id": settings.YAHOO_CLIENT_ID,
            "client_secret": settings.YAHOO_CLIENT_SECRET,
        },
        token_updater=lambda t: None,  # we persist manually
    )

def get_authorization_url(state: str) -> str:
    oauth = build_oauth()
    # You can also build this manually in /auth/login if you prefer
    auth_url, _ = oauth.authorization_url(settings.YAHOO_AUTH_URL, state=state)
    return auth_url

def exchange_token(db: Session, user_id: str, code: str) -> OAuthToken:
    oauth = build_oauth()
    token = oauth.fetch_token(
        token_url=settings.YAHOO_TOKEN_URL,
        code=code,
        client_secret=settings.YAHOO_CLIENT_SECRET,
    )
    return _persist_token(db, user_id, token)

def _persist_token(db: Session, user_id: str, token: dict) -> OAuthToken:
    rec = OAuthToken(
        user_id=user_id,
        access_token=token.get("access_token", ""),
        refresh_token=token.get("refresh_token"),
        expires_in=token.get("expires_in"),
        token_type=token.get("token_type"),
        scope=token.get("scope"),
        raw=json.dumps(token),
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec

def get_latest_token(db: Session, user_id: str) -> Optional[OAuthToken]:
    return (
        db.query(OAuthToken)
        .filter(OAuthToken.user_id == user_id)
        .order_by(OAuthToken.id.desc())
        .first()
    )

def _auth_headers(access_token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}

def _refresh_token(db: Session, user_id: str, tok: OAuthToken) -> OAuthToken:
    if not tok.refresh_token:
        raise RuntimeError("Yahoo token expired and no refresh_token is available.")
    data = {
        "grant_type": "refresh_token",
        "refresh_token": tok.refresh_token,
        "redirect_uri": settings.YAHOO_REDIRECT_URI,
    }
    r = requests.post(
        settings.YAHOO_TOKEN_URL,
        data=data,
        auth=(settings.YAHOO_CLIENT_ID, settings.YAHOO_CLIENT_SECRET),
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Yahoo refresh failed: {r.status_code} {r.text}")
    new_token = r.json()
    return _persist_token(db, user_id, new_token)


# ---- Core Yahoo GET with auto-refresh ----
def _yahoo_get(
    db: Session,
    user_id: str,
    path: str,                 # e.g. "/users;use_login=1/games;game_keys=466/leagues"
    params: Optional[dict] = None,
) -> dict:
    tok = get_latest_token(db, user_id)
    if not tok:
        raise RuntimeError("No Yahoo OAuth token on file. Call /auth/login and complete the flow first.")

    url = f"{settings.YAHOO_API_BASE.rstrip('/')}{path}"
    q = dict(params or {})
    q.setdefault("format", "json")

    resp = requests.get(url, headers=_auth_headers(tok.access_token), params=q, timeout=30)
    if resp.status_code == 401:
        tok = _refresh_token(db, user_id, tok)
        resp = requests.get(url, headers=_auth_headers(tok.access_token), params=q, timeout=30)

    resp.raise_for_status()
    return resp.json()


# ---- Robust parsers ----
def _parse_leagues(payload: dict) -> List[dict]:
    """Parse leagues whether Yahoo nests under users→games or at top-level, and scan all indices (0..count-1)."""
    fc = payload.get("fantasy_content", {})
    out: List[dict] = []

    def _collect_league_dicts(leagues_node: Any) -> List[dict]:
        """Return a flat list of league dicts from a `leagues` node that may look like:
           {"0": {"league": [...]}, "1": {"league": [...]}, "count": 3} or directly a list/dict."""
        leagues_flat: List[dict] = []
        if isinstance(leagues_node, dict):
            # iterate all numeric keys
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
            # categories may not be present in this call; handle gracefully
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

    # Path B: top-level leagues
    top = _get(fc, "leagues")
    if top is not None:
        _extract_from_leagues(top)

    # Path A: nested under users → games → game → {leagues}
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
                    # In your payload the second element is {"leagues": {...}}
                    if isinstance(g, dict) and "leagues" in g:
                        _extract_from_leagues(g["leagues"])

    return out



# ---- Public service API ----
def _fetch_league_settings(db: Session, user_id: str, league_keys: List[str]) -> dict[str, List[str]]:
    """
    Fetch stat category display_names for given league keys.
    Handles Yahoo's 'league' = [ {league-fields}, { "settings": [ { ... } ] } ] shape.
    Returns mapping league_key -> [categories...].
    """
    if not league_keys:
        return {}

    keys_param = ",".join(league_keys)
    payload = _yahoo_get(db, user_id, f"/leagues;league_keys={keys_param}/settings")
    fc = payload.get("fantasy_content", {})
    leagues_node = _get(fc, "leagues")

    out: dict[str, List[str]] = {}

    if isinstance(leagues_node, dict):
        # iterate numeric indices: "0", "1", ...
        for k, v in leagues_node.items():
            if not str(k).isdigit() or not isinstance(v, dict):
                continue
            league_list = v.get("league")
            # league_list is usually a list like: [ {league_fields...}, { "settings": [ {...} ] } ]
            if not isinstance(league_list, list) or len(league_list) < 2:
                continue

            league_fields = league_list[0] if isinstance(league_list[0], dict) else {}
            settings_wrapper = league_list[1] if isinstance(league_list[1], dict) else {}

            league_key = _get(league_fields, "league_key") or _get(league_fields, "league_id")
            if not league_key:
                continue

            # settings itself is a LIST with a single dict element
            settings_list = settings_wrapper.get("settings")
            if not (isinstance(settings_list, list) and settings_list and isinstance(settings_list[0], dict)):
                continue
            settings = settings_list[0]

            cats: List[str] = []
            # stats is a LIST of { "stat": { ... display_name ... } }
            stats_arr = settings.get("stat_categories", {}).get("stats")
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
        payload = _yahoo_get(db, user_id, f"/users;use_login=1/games;game_keys={','.join(keys)}/leagues")
        return _parse_leagues(payload)

    # ---- build list of game_keys (handles both explicit + discovery paths) ----
    keys: List[str] = []

    if game_key:
        keys = [game_key]
    else:
        # Discover games then filter (robustly locate the 'games' node)
        games_payload = _yahoo_get(db, user_id, "/users;use_login=1/games")
        fc = games_payload.get("fantasy_content", {})
        user_variants = _as_list(_get(fc, "users", "0", "user"))
        games_node = None
        for item in user_variants:
            if isinstance(item, dict) and "games" in item:
                games_node = item.get("games")
                break
        if not isinstance(games_node, dict):
            return []  # no games for this account

        entries: List[Tuple[int, str, str]] = []  # (season_int, code, game_key)
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

        # optional filters
        if sport:
            sport = sport.lower().strip()
            entries = [e for e in entries if e[1] == sport]
        if season is not None:
            try:
                s = int(season)
                entries = [e for e in entries if e[0] == s]
            except Exception:
                pass

        # newest first, unique keys, cap
        entries.sort(key=lambda t: t[0], reverse=True)
        seen: set[str] = set()
        for _, _, gk in entries:
            if gk in seen:
                continue
            seen.add(gk)
            keys.append(gk)
            if len(keys) >= 6:
                break

    # ---- fetch leagues then enrich categories via settings ----
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




def _parse_teams(payload: dict, league_id: str) -> List[dict]:
    """
    Ultra-robust teams parser for /leagues;league_keys=<league_id>/teams.
    Recursively searches for any 'team' nodes and extracts {id, name, manager}.
    Works across NHL/NBA and various Yahoo list/dict shapes.
    """
    out: List[dict] = []

    def extract_name(team: dict) -> str | None:
        nm = team.get("name")
        if isinstance(nm, str):
            return nm
        if isinstance(nm, dict):
            full = nm.get("full")
            if isinstance(full, str):
                return full
        return None

    def extract_manager(team: dict) -> str | None:
        mgrs = _as_list(_get(team, "managers", "0", "manager"))
        if not mgrs:
            return None
        m0 = mgrs[0] if isinstance(mgrs[0], dict) else {}
        return m0.get("guid") or m0.get("nickname")

    def maybe_take(team_obj: dict):
        team_key = _get(team_obj, "team_key")
        name = extract_name(team_obj)
        mgr = extract_manager(team_obj)
        if team_key and name:
            out.append({"id": str(team_key), "name": str(name), "manager": mgr})

    # recursive walk to find any 'team' lists/dicts
    def walk(node: Any):
        if isinstance(node, dict):
            # direct team dict
            if "team_key" in node and ("name" in node or _get(node, "name", "full")):
                maybe_take(node)

            # team containers: {"team": {...}} or {"team": [..]}
            if "team" in node:
                t = node["team"]
                if isinstance(t, dict):
                    maybe_take(t)
                elif isinstance(t, list):
                    for item in t:
                        if isinstance(item, dict):
                            maybe_take(item)

            # keep walking nested dicts/lists
            for v in node.values():
                walk(v)

        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload.get("fantasy_content", {}))
    return out



def _parse_roster(payload: dict, team_id: str) -> Tuple[str, List[dict]]:
    """
    Robust parser for /teams;team_keys=<team_id>/roster[;date=YYYY-MM-DD]
    Returns (date, players[ {player_id, name, positions, status} ]).
    """
    fc = payload.get("fantasy_content", {})
    teams_node = _get(fc, "teams")
    if not isinstance(teams_node, dict):
        return "", []

    team_entry = _get(teams_node, "0", "team")
    # team_entry is usually a list: [ {team_fields...}, {"roster": {...}} ]
    roster_node = None
    if isinstance(team_entry, list):
        for item in team_entry:
            if isinstance(item, dict) and "roster" in item:
                roster_node = item["roster"]
                break
    elif isinstance(team_entry, dict):
        roster_node = team_entry.get("roster")

    if not isinstance(roster_node, dict):
        return "", []

    # date can be at roster["date"] or sometimes inside the "0" child
    date = roster_node.get("date") or _get(roster_node, "0", "date") or ""

    # players typically at roster["0"]["players"]
    players_node = _get(roster_node, "0", "players")
    players_list: List[dict] = []
    if isinstance(players_node, dict):
        for k, v in players_node.items():
            if str(k).isdigit() and isinstance(v, dict):
                p = v.get("player")
                if isinstance(p, dict):
                    players_list.append(p)
                elif isinstance(p, list):
                    players_list.extend([i for i in p if isinstance(i, dict)])
    elif isinstance(players_node, list):
        players_list.extend([i for i in players_node if isinstance(i, dict)])

    out: List[dict] = []
    for p in players_list:
        pid = _get(p, "player_id")
        # NHL sometimes nests name under {"name": {"full": "..."}}
        pname = _get(p, "name", "full") or p.get("name")
        # positions may be a list of dicts under "eligible_positions" or a single "position"
        positions: List[str] = []
        pos_raw = _get(p, "eligible_positions", "position")
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

        status = _get(p, "status") or None
        if pid and pname:
            out.append({
                "player_id": str(pid),
                "name": str(pname),
                "positions": positions,
                "status": status,
            })

    return (str(date), out)


# Public wrappers used by routes (explicit user_id so we don't hide it)
def get_teams_for_user(db: Session, user_id: str, league_id: str) -> List[dict]:
    if settings.YAHOO_FAKE_MODE:
        return [
            {"id": f"{league_id}.t.1", "name": "Nav’s Team", "manager": "Nav"},
            {"id": f"{league_id}.t.2", "name": "Rival Squad", "manager": "Alex"},
        ]
    payload = _yahoo_get(db, user_id, f"/leagues;league_keys={league_id}/teams")
    return _parse_teams(payload, league_id)

def get_roster_for_user(db: Session, user_id: str, team_id: str, date: Optional[str] = None) -> dict:
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
    payload = _yahoo_get(db, user_id, f"/teams;team_keys={team_id}/roster{date_part}")
    r_date, players = _parse_roster(payload, team_id)
    return {"team_id": team_id, "date": r_date or (date or ""), "players": players}


# ---- Debug helper (used by /debug/yahoo/raw) ----
def yahoo_raw_get(db: Session, user_id: str, path: str, params: Optional[dict] = None) -> dict:
    return _yahoo_get(db, user_id, path, params or {})
