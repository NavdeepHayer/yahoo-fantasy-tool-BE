import json
import requests
from typing import Any, Dict, List, Tuple, Optional
from requests_oauthlib import OAuth2Session
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import OAuthToken

from typing import Any, List, Tuple, Optional

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
    Robust teams parser for Yahoo's NHL/NBA responses.

    Handles shapes like:
      fantasy_content.league[1].teams["0"].team == [
        [ { "team_key": ... }, { "team_id": ... }, { "name": ... }, [], { "url": ... }, ..., { "managers": [ { "manager": {...} } ] } ]
      ]

    Also tolerates:
      - singular vs plural endpoints
      - 'team' as dict or list
      - managers as list or numeric-keyed dict
    """
    out: List[dict] = []

    def flatten_team_node(node: Any) -> Optional[dict]:
        """
        Normalize any 'team' node into a single dict of fields.
        Accepts:
          - dict → return as-is
          - list → flatten dicts inside; if node[0] is a list, flatten node[0]
        """
        if isinstance(node, dict):
            return node
        if isinstance(node, list):
            # If first element is itself a list, that's the real payload.
            items = node[0] if node and isinstance(node[0], list) else node
            agg: dict = {}
            for part in items:
                if isinstance(part, dict):
                    # Merge shallow dicts like {"team_key": "..."} into one object
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
        # Common new style: list of {"manager": {...}}
        if isinstance(mgrs, list):
            for item in mgrs:
                if isinstance(item, dict):
                    m = item.get("manager")
                    if isinstance(m, dict):
                        nick = m.get("nickname")
                        guid = m.get("guid")
                        if nick or guid:
                            return nick or guid
        # Older style: {"0": {"manager": {...}}, "count": ...}
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
            # Direct 'team' container
            if "team" in node:
                maybe_take(node["team"])
            # Keep walking
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload.get("fantasy_content", {}))

    # Deduplicate by team id (order-preserving)
    seen: set[str] = set()
    deduped: List[dict] = []
    for t in out:
        if t["id"] in seen:
            continue
        seen.add(t["id"])
        deduped.append(t)

    return deduped



from typing import Any, List, Tuple, Optional

def _parse_roster(payload: dict, team_id: str) -> Tuple[str, List[dict]]:
    """
    Robust NHL-friendly roster parser.

    Handles shapes like:
      fantasy_content.team == [
        [ { team fields... } ],
        { "roster": {
            "coverage_type": "date",
            "date": "YYYY-MM-DD",
            "0": {
              "players": {
                 "0": { "player": [
                          [ { "player_key": ... }, { "player_id": ... }, { "name": {...} }, ... ],
                          { "selected_position": [...] },
                          { "is_editable": 1 }
                       ] },
                 "1": { "player": [ ... ] },
                 ...
              }
            }
        } }
      ]

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
        """
        Normalize any 'player' node into a single dict of fields.
        Accepts:
          - dict → return as-is
          - list → if first element is a list, flatten that; otherwise flatten the list
        """
        if isinstance(pnode, dict):
            return pnode
        if isinstance(pnode, list):
            core = pnode[0] if pnode and isinstance(pnode[0], list) else pnode
            agg: dict = {}
            for part in core:
                if isinstance(part, dict):
                    # shallow merge of tiny dicts ({"player_id": "..."}, {"name": {...}}, etc.)
                    for k, v in part.items():
                        agg[k] = v
            return agg or None
        return None

    def extract_positions(obj: dict) -> List[str]:
        # Eligible positions typically as a list of {"position": "C"} dicts
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
        # Some responses also include "display_position": "C,LW" — you can choose to include it if you want:
        # dp = obj.get("display_position"); if isinstance(dp, str): positions.extend([p.strip() for p in dp.split(",") if p.strip()])
        return positions

    fc = payload.get("fantasy_content", {})
    roster = find_roster(fc)
    if not isinstance(roster, dict):
        return "", []

    # Date can be at roster["date"] or roster["0"]["date"]
    date = roster.get("date") or (isinstance(roster.get("0"), dict) and roster["0"].get("date")) or ""

    # Players normally at roster["0"]["players"]
    players_container = None
    r0 = roster.get("0")
    if isinstance(r0, dict):
        players_container = r0.get("players")
    if players_container is None:
        # Fallback: search anywhere under roster for a "players" dict
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

    # Collect each player's "player" node and flatten it
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

    # Normalize final fields
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



def get_teams_for_user(db: Session, user_id: str, league_id: str) -> List[dict]:
    """
    Public wrapper with automatic endpoint fallback.
    Tries singular (/league/<key>/teams) first; if empty, tries plural (/leagues;league_keys=<key>/teams).
    """
    if settings.YAHOO_FAKE_MODE:
        return [
            {"id": f"{league_id}.t.1", "name": "Nav’s Team", "manager": "Nav"},
            {"id": f"{league_id}.t.2", "name": "Rival Squad", "manager": "Alex"},
        ]

    # Try singular path first (usually cleaner)
    payload = _yahoo_get(db, user_id, f"/league/{league_id}/teams")
    teams = _parse_teams(payload, league_id)
    if teams:
        return teams

    # Fallback: plural/semicolon style
    payload2 = _yahoo_get(db, user_id, f"/leagues;league_keys={league_id}/teams")
    teams2 = _parse_teams(payload2, league_id)
    return teams2


def get_roster_for_user(db: Session, user_id: str, team_id: str, date: Optional[str] = None) -> dict:
    """
    Public wrapper with automatic endpoint fallback.
    Tries singular (/team/<key>/roster) first; if empty, tries plural (/teams;team_keys=<key>/roster).
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

    # Try singular first
    payload = _yahoo_get(db, user_id, f"/team/{team_id}/roster{date_part}")
    r_date, players = _parse_roster(payload, team_id)
    if players:
        return {"team_id": team_id, "date": r_date or (date or ""), "players": players}

    # Fallback to plural
    payload2 = _yahoo_get(db, user_id, f"/teams;team_keys={team_id}/roster{date_part}")
    r_date2, players2 = _parse_roster(payload2, team_id)
    return {"team_id": team_id, "date": r_date2 or (date or ""), "players": players2}



# ---- Debug helper (used by /debug/yahoo/raw) ----
def yahoo_raw_get(db: Session, user_id: str, path: str, params: Optional[dict] = None) -> dict:
    return _yahoo_get(db, user_id, path, params or {})
