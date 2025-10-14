from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from urllib.parse import urlparse, parse_qs, quote_plus
from typing import List, Tuple, Optional, Any

from app.db.session import get_db
from app.deps import get_user_id
from app.core.config import settings
from app.core.security import gen_state
from app.services.yahoo import yahoo_raw_get
# import the parser directly to test exactly what /me/leagues uses
from app.services.yahoo import _parse_leagues as __parse_leagues  # type: ignore

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.db.models import User, OAuthToken

router = APIRouter(prefix="/debug", tags=["debug"])


@router.get("/ping")
def ping():
    return {"ok": True}


@router.get("/auth-url")
def debug_auth_url():
    # Shows what we’re sending to Yahoo (client_id, redirect_uri, scope)
    state = gen_state()
    params = (
        f"response_type=code"
        f"&client_id={quote_plus(settings.YAHOO_CLIENT_ID)}"
        f"&redirect_uri={quote_plus(settings.YAHOO_REDIRECT_URI)}"
        f"&scope={quote_plus('fspt-r')}"
        f"&state={quote_plus(state)}"
    )
    url = f"{settings.YAHOO_AUTH_URL}?{params}"
    qs = parse_qs(urlparse(url).query)
    return {"authorization_url": url, "params": {k: (v[0] if len(v) == 1 else v) for k, v in qs.items()}}


@router.get("/yahoo/raw")
def yahoo_raw(
    path: str = Query(..., description="Yahoo path starting with /, e.g. /users;use_login=1/games;game_keys=466/leagues"),
    limit: int = Query(20, description="Limit number of lines or items returned for preview"),
    keys_only: bool = Query(False, description="If true, return only top-level keys of the data"),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """
    Proxy any Yahoo Fantasy path (adds format=json), but trims the output for readability.
    Use `?keys_only=true` to just see top-level keys.
    Use `?limit=20` to restrict number of items printed.
    """
    try:
        data = yahoo_raw_get(db, user_id, path, params={"format": "json"})
        if keys_only and isinstance(data, dict):
            return {"path": path, "ok": True, "top_level_keys": list(data.keys())}
        # Trim deeply nested structures for readability
        import json
        snippet = json.dumps(data, indent=2)[:limit * 1000]  # about limit KB
        return {"path": path, "ok": True, "preview": snippet + ("..." if len(snippet) >= limit * 1000 else "")}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/parse/leagues-by-key")
def parse_leagues_by_key(
    game_key: str = Query(..., description="Yahoo game_key, e.g. 466 for NBA 2025"),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Fetch raw leagues for a specific game_key and run them through the same parser as /me/leagues."""
    try:
        raw = yahoo_raw_get(
            db, user_id,
            f"/users;use_login=1/games;game_keys={game_key}/leagues",
            params={"format": "json"},
        )
        parsed = __parse_leagues(raw)
        return {"ok": True, "count": len(parsed), "parsed": parsed}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/parse/teams")
def parse_teams_debug(
    league_id: str = Query(..., description="Yahoo league key, e.g. 466.l.17802"),
    path_style: str = Query(
        "singular",
        regex="^(singular|plural)$",
        description="Which Yahoo path style to use",
    ),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """
    Fetch raw teams for league and run through the same parser used by /league/{league_id}/teams.
    """
    try:
        path = (
            f"/league/{league_id}/teams"
            if path_style == "singular"
            else f"/leagues;league_keys={league_id}/teams"
        )
        raw = yahoo_raw_get(db, user_id, path, params={"format": "json"})
        from app.services.yahoo import _parse_teams as __parse_teams  # type: ignore
        parsed = __parse_teams(raw, league_id)
        return {"ok": True, "path": path, "count": len(parsed), "parsed": parsed}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/parse/roster")
def parse_roster_debug(
    team_id: str = Query(..., description="Yahoo team key, e.g. 466.l.17802.t.3"),
    date: Optional[str] = Query(default=None, description="Optional YYYY-MM-DD"),
    path_style: str = Query(
        "singular",
        regex="^(singular|plural)$",
        description="Which Yahoo path style to use",
    ),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """
    Fetch raw roster for team and run through the same parser used by /league/team/{team_id}/roster.
    """
    try:
        date_part = f";date={date}" if date else ""
        path = (
            f"/team/{team_id}/roster{date_part}"
            if path_style == "singular"
            else f"/teams;team_keys={team_id}/roster{date_part}"
        )
        raw = yahoo_raw_get(db, user_id, path, params={"format": "json"})
        from app.services.yahoo import _parse_roster as __parse_roster  # type: ignore
        r_date, players = __parse_roster(raw, team_id)
        return {
            "ok": True,
            "path": path,
            "date": r_date or (date or ""),
            "count": len(players),
            "players": players,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/me/games")
def debug_me_games(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """
    Returns the raw games plus the (season, code, game_key) tuples we use for discovery.
    """
    try:
        raw = yahoo_raw_get(db, user_id, "/users;use_login=1/games", params={"format": "json"})
        fc = raw.get("fantasy_content", {})
        users = fc.get("users", {})
        user0 = users.get("0", {}).get("user", [])
        games_node = user0[1].get("games") if isinstance(user0, list) and len(user0) > 1 else None

        entries: List[Tuple[int, str, str]] = []
        if isinstance(games_node, dict):
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

        entries.sort(key=lambda t: t[0], reverse=True)
        return {"ok": True, "entries": entries, "raw_snippet": {"users.0.user[0]": user0[0] if user0 else None}}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/me/leagues")
def debug_me_leagues(
    sport: Optional[str] = Query(default=None),
    season: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """
    Re-implements discovery inline so we can see which game_keys are chosen,
    then fetches leagues and returns parsed result + the chosen keys.
    """
    try:
        raw_games = yahoo_raw_get(db, user_id, "/users;use_login=1/games", params={"format": "json"})
        fc = raw_games.get("fantasy_content", {})
        users = fc.get("users", {})
        user0 = users.get("0", {}).get("user", [])
        games_node = user0[1].get("games") if isinstance(user0, list) and len(user0) > 1 else None

        entries: List[Tuple[int, str, str]] = []
        if isinstance(games_node, dict):
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
            entries = [e for e in entries if e[1] == sport.lower().strip()]
        if season is not None:
            try:
                s = int(season)
                entries = [e for e in entries if e[0] == s]
            except Exception:
                pass

        entries.sort(key=lambda t: t[0], reverse=True)

        # choose up to 6 unique game_keys
        seen = set()
        keys: List[str] = []
        for _, _, gk in entries:
            if gk in seen:
                continue
            seen.add(gk)
            keys.append(gk)
            if len(keys) >= 6:
                break

        # fetch and parse leagues for those keys
        if not keys:
            return {"ok": True, "keys": [], "parsed": []}
        raw_leagues = yahoo_raw_get(
            db, user_id,
            f"/users;use_login=1/games;game_keys={','.join(keys)}/leagues",
            params={"format": "json"},
        )
        parsed = __parse_leagues(raw_leagues)
        return {"ok": True, "keys": keys, "count": len(parsed), "parsed": parsed}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    
@router.get("/users")
def list_users(db: Session = Depends(get_db)):
    rows = db.query(User).order_by(User.created_at.desc()).all()
    return [{"guid": r.guid, "nickname": r.nickname, "created_at": r.created_at} for r in rows]

@router.get("/tokens")
def tokens_for_user(user_id: str, db: Session = Depends(get_db)):
    rows = (
        db.query(OAuthToken)
        .filter(OAuthToken.user_id == user_id)
        .order_by(OAuthToken.id.desc())
        .all()
    )
    return {
        "user_id": user_id,
        "count": len(rows),
        "latest_created_at": rows[0].created_at if rows else None,
    }



@router.get("/db")
def db_info(db: Session = Depends(get_db)):
    # Dialect & DSN (sanitized)
    url = settings.DATABASE_URL
    sanitized = url.replace(settings.YAHOO_CLIENT_SECRET or "", "***") if url else url
    dialect = db.bind.dialect.name if db.bind else "unknown"

    server_ver = None
    current_schema = None
    try:
        server_ver = db.execute(text("select version()")).scalar()
        current_schema = db.execute(text("select current_schema()")).scalar()
    except Exception as _:
        pass

    return {
        "dialect": dialect,
        "database_url": sanitized,
        "server_version": server_ver,
        "current_schema": current_schema,
    }
