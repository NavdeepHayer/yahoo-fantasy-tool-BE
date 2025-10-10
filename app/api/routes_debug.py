from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from urllib.parse import urlparse, parse_qs, quote_plus

from app.db.session import get_db
from app.deps import get_user_id
from app.core.config import settings
from app.core.security import gen_state
from app.services.yahoo import yahoo_raw_get
from typing import List, Tuple
# import the parser directly to test exactly what /me/leagues uses
from app.services.yahoo import _parse_leagues as __parse_leagues  # type: ignore

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
    return {"authorization_url": url, "params": {k: (v[0] if len(v)==1 else v) for k,v in qs.items()}}

@router.get("/yahoo/raw")
def yahoo_raw(
    path: str = Query(..., description="Yahoo path starting with /, e.g. /users;use_login=1/games;game_keys=466/leagues"),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    try:
        data = yahoo_raw_get(db, user_id, path, params={"format": "json"})
        return {"path": path, "ok": True, "data": data}
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
        raw = yahoo_raw_get(db, user_id, f"/users;use_login=1/games;game_keys={game_key}/leagues", params={"format":"json"})
        parsed = __parse_leagues(raw)
        return {"ok": True, "count": len(parsed), "parsed": parsed}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    
@router.get("/parse/teams")
def parse_teams_debug(
    league_id: str = Query(..., description="Yahoo league key, e.g. 466.l.17802"),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """
    Fetch raw teams for league and run through the same parser used by /league/{league_id}/teams.
    """
    try:
        raw = yahoo_raw_get(db, user_id, f"/leagues;league_keys={league_id}/teams", params={"format":"json"})
        from app.services.yahoo import _parse_teams as __parse_teams  # type: ignore
        parsed = __parse_teams(raw, league_id)
        return {"ok": True, "count": len(parsed), "parsed": parsed}
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
        raw = yahoo_raw_get(db, user_id, "/users;use_login=1/games", params={"format":"json"})
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
    sport: str | None = Query(default=None),
    season: int | None = Query(default=None),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """
    Re-implements discovery inline so we can see which game_keys are chosen,
    then fetches leagues and returns parsed result + the chosen keys.
    """
    try:
        raw_games = yahoo_raw_get(db, user_id, "/users;use_login=1/games", params={"format":"json"})
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
                if isinstance(gitems, dict): gitems = [gitems]
                if not isinstance(gitems, list): continue
                for g in gitems:
                    if not isinstance(g, dict): continue
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
            if gk in seen: continue
            seen.add(gk); keys.append(gk)
            if len(keys) >= 6: break

        # fetch and parse leagues for those keys
        if not keys:
            return {"ok": True, "keys": [], "parsed": []}
        raw_leagues = yahoo_raw_get(db, user_id, f"/users;use_login=1/games;game_keys={','.join(keys)}/leagues", params={"format":"json"})
        parsed = __parse_leagues(raw_leagues)
        return {"ok": True, "keys": keys, "count": len(parsed), "parsed": parsed}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
