from fastapi import HTTPException
import requests
from typing import Optional, Dict, Any, Tuple
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import decrypt_value
from app.db.models import OAuthToken
from app.services.yahoo.oauth import get_latest_token, refresh_token
from urllib.parse import parse_qsl

def _auth_headers(access_token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}

def _raise_with_yahoo_body(resp: requests.Response) -> None:
    # Include upstream body to see *why* Yahoo said 400/401/… in FastAPI logs & response
    try:
        msg = resp.text[:2000]  # keep it sane
    except Exception:
        msg = "<no-body>"
    raise HTTPException(status_code=resp.status_code, detail=f"Yahoo error {resp.status_code} on {resp.url} :: {msg}")

def yahoo_get(
    db: Session,
    user_id: str,
    path: str,                 # e.g. "/users;use_login=1/games;game_keys=466/leagues"
    params: Optional[dict] = None,
) -> dict:
    """
    Core Yahoo GET with auto-refresh on 401. Mirrors original behavior,
    but decrypts stored tokens before use and surfaces upstream error bodies.
    """
    uid = (user_id or "").strip()
    tok = get_latest_token(db, uid)
    if not tok:
        count = db.query(OAuthToken).filter(OAuthToken.user_id == uid).count()
        raise HTTPException(
            status_code=400,
            detail=f"No Yahoo OAuth token on file for user_id={uid!r} (rows={count}). Call /auth/login and complete the flow first."
        )

    access_token = decrypt_value(tok.access_token)
    base = settings.YAHOO_API_BASE.rstrip("/")      # https://fantasysports.yahooapis.com/fantasy/v2
    rel  = path.lstrip("/")                         # e.g., league/465.l.34067/players;.../stats;type=week;week=2
    url  = f"{base}/{rel}"
    q = dict(params or {})
    q.setdefault("format", "json")

    resp = requests.get(url, headers=_auth_headers(access_token), params=q, timeout=30)
    if resp.status_code == 401:
        new_tok = refresh_token(db, uid, tok)
        access_token = decrypt_value(new_tok.access_token)
        resp = requests.get(url, headers=_auth_headers(access_token), params=q, timeout=30)

    if not resp.ok:
        _raise_with_yahoo_body(resp)

    try:
        return resp.json()
    except Exception:
        _raise_with_yahoo_body(resp)

def yahoo_raw_get(
    db: Session,
    user_id: str,
    path: str,                                   # may include its own query, e.g. "/league/.../players;status=FA;count=25?foo=bar"
    params: Optional[Dict[str, Any]] = None,
) -> dict:
    """
    Raw Yahoo GET with safe URL join and query merging.
    """
    rel = path.lstrip("/")
    merged: Dict[str, Any] = {}
    if "?" in rel:
        rel, embedded_qs = rel.split("?", 1)
        merged.update(dict(parse_qsl(embedded_qs, keep_blank_values=True)))
    if params:
        merged.update(params)
    merged.setdefault("format", "json")
    return yahoo_get(db=db, user_id=user_id, path="/" + rel, params=merged)

# ------------------------
# Helpers for player stats
# ------------------------

def build_player_stats_path(
    league_key: str,
    player_key: str,
    kind: str,
    *,
    week: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    season: Optional[str] = None,
) -> str:
    """
    Constructs the *league-scoped* players stats path with Yahoo's picky rules:
      - week: /league/{league_key}/players;player_keys={player_key}/stats;type=week;week=N[;season=YYYY]
      - date single-day: .../stats;type=date;date=YYYY-MM-DD
      - date range:      .../stats;type=date;start=YYYY-MM-DD;end=YYYY-MM-DD
    """
    base = f"/league/{league_key}/players;player_keys={player_key}/stats"
    if kind == "week":
        if week is None:
            raise HTTPException(status_code=400, detail="Missing 'week' for kind=week")
        tail = f";type=week;week={int(week)}"
        if season:
            tail += f";season={season}"
        return base + tail

    if kind == "date_range":
        if not date_from:
            raise HTTPException(status_code=400, detail="Missing 'date_from' for kind=date_range")
        if date_to and date_to != date_from:
            return f"{base};type=date;start={date_from};end={date_to}"
        # Yahoo throws 400 for start=end; use single-day form
        return f"{base};type=date;date={date_from}"

    if kind == "season":
        # Season aggregate
        tail = ";type=season"
        if season:
            tail += f";season={season}"
        return base + tail

    raise HTTPException(status_code=400, detail=f"Unknown kind={kind!r}")

def fetch_player_stats_with_fallback(
    db: Session,
    user_id: str,
    league_key: str,
    player_key: str,
    kind: str,
    *,
    week: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    season: Optional[str] = None,
) -> dict:
    """
    First try league-scoped path. If it returns a payload without 'player_stats',
    retry using the global player path (outside the league) which is more reliable
    for some players/games.
    """
    # 1) league-scoped
    league_path = build_player_stats_path(
        league_key, player_key, kind, week=week, date_from=date_from, date_to=date_to, season=season
    )
    data = yahoo_get(db, user_id, league_path)

    def has_stats(d: dict) -> bool:
        try:
            players = d["fantasy_content"]["league"][1]["players"]
            # players.<idx>.player is list; stats node appears after identity fields
            for k, v in players.items():
                if k == "count":
                    continue
                player_items = v["player"][0]
                # stats node can be at index 1 or later depending on game; scan
                for item in v["player"]:
                    if isinstance(item, dict) and "player_stats" in item:
                        return True
                    if isinstance(item, list):
                        for sub in item:
                            if isinstance(sub, dict) and "player_stats" in sub:
                                return True
            return False
        except Exception:
            return False

    if has_stats(data):
        return data

    # 2) global player fallback
    #   /player/{player_key}/stats;type=...
    if kind == "week":
        tail = f";type=week;week={int(week)}"
        if season:
            tail += f";season={season}"
        player_path = f"/player/{player_key}/stats{tail}"
    elif kind == "date_range":
        if date_to and date_to != date_from:
            player_path = f"/player/{player_key}/stats;type=date;start={date_from};end={date_to}"
        else:
            player_path = f"/player/{player_key}/stats;type=date;date={date_from}"
    elif kind == "season":
        tail = ";type=season"
        if season:
            tail += f";season={season}"
        player_path = f"/player/{player_key}/stats{tail}"
    else:
        raise HTTPException(status_code=400, detail=f"Unknown kind={kind!r}")

    return yahoo_get(db, user_id, player_path)
