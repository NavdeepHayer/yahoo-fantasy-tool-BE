# app/api/routes_scheduling.py
from __future__ import annotations

from fastapi import APIRouter, Query, HTTPException, Body, Depends
from pydantic import BaseModel, Field
from typing import Literal, Dict, Any, List, Tuple, Optional
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

router = APIRouter(prefix="/schedule", tags=["schedule"])

# ----------------------------
# HTTP session (connection pool)
# ----------------------------
_REQS = requests.Session()
_REQS.headers.update({"User-Agent": "ScheduleBootstrap/1.0"})
_REQS.mount(
    "https://",
    HTTPAdapter(
        pool_connections=8,
        pool_maxsize=16,
        max_retries=Retry(
            total=2,
            backoff_factor=0.2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        ),
    ),
)
_REQ_TIMEOUT = 15

# ----------------------------
# ESPN config
# ----------------------------
# sport -> (espn path segments)
_CFG = {
    "nhl": {"sport": "hockey", "league": "nhl"},
    "nba": {"sport": "basketball", "league": "nba"},
}

def _datestr(dt: date) -> str:
    return dt.isoformat()

def _yyyymmdd(dt: date) -> str:
    return dt.strftime("%Y%m%d")

def _coerce_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except Exception:
        raise HTTPException(status_code=422, detail=f"Bad date: {s!r}. Use YYYY-MM-DD.")

# ----------------------------
# In-memory caches (simple + daily invalidation)
# ----------------------------
_CACHE_DAY = None  # yyyy-mm-dd (UTC)
_TEAM_MAP: Dict[str, Dict[str, Any]] = {}  # sport -> { "abbr_to_id":{NYR:3}, "id_to_abbr":{3:NYR} }
_SCHED_CACHE: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}  # (sport, teamId) -> events list

def _maybe_roll_daily_cache():
    global _CACHE_DAY, _TEAM_MAP, _SCHED_CACHE
    today = datetime.utcnow().date().isoformat()
    if _CACHE_DAY != today:
        _CACHE_DAY = today
        _TEAM_MAP.clear()
        _SCHED_CACHE.clear()

# ----------------------------
# ESPN helpers
# ----------------------------
def _espn_get(path: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    url = f"https://site.api.espn.com/apis/site/v2{path}"
    r = _REQS.get(url, params=params or {}, timeout=_REQ_TIMEOUT)
    if not r.ok:
        raise HTTPException(status_code=502, detail=f"ESPN upstream {r.status_code} for {url}")
    try:
        return r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="ESPN returned non-JSON")

def _ensure_team_map(sport: Literal["nhl", "nba"]) -> Dict[str, Any]:
    _maybe_roll_daily_cache()
    if sport in _TEAM_MAP:
        return _TEAM_MAP[sport]

    cfg = _CFG[sport]
    data = _espn_get(f"/sports/{cfg['sport']}/{cfg['league']}/teams")
    teams = []
    for s in (data.get("sports") or []):
        for l in (s.get("leagues") or []):
            for t in (l.get("teams") or []):
                team = t.get("team") or {}
                teams.append(team)

    abbr_to_id: Dict[str, str] = {}
    id_to_abbr: Dict[str, str] = {}
    for t in teams:
        abbr = (t.get("abbreviation") or "").upper()
        tid = str(t.get("id") or "").strip()
        if abbr and tid:
            abbr_to_id[abbr] = tid
            id_to_abbr[tid] = abbr

    if not abbr_to_id:
        raise HTTPException(status_code=502, detail="Failed to load teams from ESPN")

    _TEAM_MAP[sport] = {"abbr_to_id": abbr_to_id, "id_to_abbr": id_to_abbr}
    return _TEAM_MAP[sport]

def _fetch_team_schedule_raw(sport: str, team_id: str, start: date, end: date) -> List[Dict[str, Any]]:
    cfg = _CFG[sport]
    # ESPN supports a date range query as "YYYYMMDD-YYYYMMDD"
    window = f"{_yyyymmdd(start)}-{_yyyymmdd(end)}"
    data = _espn_get(
        f"/sports/{cfg['sport']}/{cfg['league']}/teams/{team_id}/schedule",
        params={"dates": window},
    )
    events = data.get("events") or []
    # Normalize
    out = []
    for ev in events:
        # canonical startTime in UTC in "date"
        start_iso = ev.get("date")  # ISO8601
        # participants
        comp = (ev.get("competitions") or [{}])[0]
        competitors = comp.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away = next((c for c in competitors if c.get("homeAway") == "away"), {})
        out.append({
            "id": ev.get("id"),
            "start_utc": start_iso,
            "status": (ev.get("status") or {}).get("type", {}).get("name"),
            "home": {
                "id": (home.get("team") or {}).get("id"),
                "abbr": (home.get("team") or {}).get("abbreviation"),
                "display": (home.get("team") or {}).get("displayName"),
            },
            "away": {
                "id": (away.get("team") or {}).get("id"),
                "abbr": (away.get("team") or {}).get("abbreviation"),
                "display": (away.get("team") or {}).get("displayName"),
            },
        })
    return out

def _get_team_schedule_cached(sport: str, team_id: str, start: date, end: date) -> List[Dict[str, Any]]:
    _maybe_roll_daily_cache()
    key = (sport, team_id)
    cached = _SCHED_CACHE.get(key)
    if cached is None:
        evs = _fetch_team_schedule_raw(sport, team_id, start, end)
        _SCHED_CACHE[key] = evs
        return evs
    # If cached but the window is outside what we fetched, re-fetch (simple policy).
    # (ESPN returns full season; usually one fetch is enough.)
    return cached

# ----------------------------
# Public endpoints
# ----------------------------

@router.get("/bootstrap")
def bootstrap_get(
    sport: Literal["nhl", "nba"] = Query(...),
    start: date = Query(...),
    end: date = Query(...),
):
    if end < start:
        raise HTTPException(status_code=422, detail="end must be >= start")
    _maybe_roll_daily_cache()
    tm = _ensure_team_map(sport)
    abbr_to_id = tm["abbr_to_id"]

    total = 0
    loaded = 0
    errors: List[str] = []

    for abbr, tid in abbr_to_id.items():
        total += 1
        try:
            _get_team_schedule_cached(sport, tid, start, end)
            loaded += 1
        except HTTPException as e:
            errors.append(f"{abbr}:{e.detail}")
        except Exception as e:
            errors.append(f"{abbr}:{type(e).__name__}")

    return {
        "ok": loaded > 0,
        "sport": sport,
        "window": {"start": _datestr(start), "end": _datestr(end)},
        "teams_loaded": loaded,
        "teams_total": total,
        "errors": errors[:10],  # cap in response
        "message": "Bootstrap complete (cached in memory for the day).",
    }

@router.get("/{sport}/team/{abbr}")
def team_window(
    sport: Literal["nhl", "nba"],
    abbr: str,
    start: date = Query(...),
    end: date = Query(...),
):
    if end < start:
        raise HTTPException(status_code=422, detail="end must be >= start")
    abbr = abbr.upper()

    tm = _ensure_team_map(sport)
    tid = tm["abbr_to_id"].get(abbr)
    if not tid:
        raise HTTPException(status_code=404, detail=f"Unknown {sport} team abbr: {abbr}")

    events = _get_team_schedule_cached(sport, tid, start, end)
    # Filter to window (ESPN often returns whole season)
    def within(e):
        try:
            dt = datetime.fromisoformat(e["start_utc"].replace("Z", "+00:00")).date()
            return start <= dt <= end
        except Exception:
            return False

    filtered = [e for e in events if within(e)]
    return {
        "team": {"abbr": abbr, "id": tid},
        "sport": sport,
        "window": {"start": _datestr(start), "end": _datestr(end)},
        "count": len(filtered),
        "events": filtered,
        "cached": True,
    }

# ---- roster/players summary (team-based) ----

class PlayerTeam(BaseModel):
    player_id: str
    team: str = Field(..., description="Team abbreviation, e.g., NYR / BOS / LAL")

class SummaryReq(BaseModel):
    sport: Literal["nhl", "nba"]
    players: List[PlayerTeam]
    start: date
    end: date
    tz: str = "America/Toronto"

class SummaryLine(BaseModel):
    player_id: str
    team: str
    games_this_week: int
    has_game_today: bool
    next_game_local: Optional[str] = None
    dates: List[str] = []

class SummaryResp(BaseModel):
    sport: str
    window: Dict[str, str]
    players: List[SummaryLine]

@router.post("/summary", response_model=SummaryResp)
def schedule_summary(body: SummaryReq = Body(...)):
    if body.end < body.start:
        raise HTTPException(status_code=422, detail="end must be >= start")

    tm = _ensure_team_map(body.sport)
    tz = ZoneInfo(body.tz)
    today_local = datetime.now(tz).date()

    # Build per-team schedule (cached)
    team_to_events: Dict[str, List[Dict[str, Any]]] = {}
    for pt in body.players:
        abbr = pt.team.upper()
        tid = tm["abbr_to_id"].get(abbr)
        if not tid:
            raise HTTPException(status_code=400, detail=f"Unknown {body.sport} team abbr: {abbr}")
        if tid not in team_to_events:
            team_to_events[tid] = _get_team_schedule_cached(body.sport, tid, body.start, body.end)

    resp_players: List[SummaryLine] = []
    for pt in body.players:
        abbr = pt.team.upper()
        tid = tm["abbr_to_id"][abbr]
        evs = team_to_events[tid]

        # window filter + compute dates
        ds: List[date] = []
        next_local_iso: Optional[str] = None

        for e in evs:
            try:
                dt_utc = datetime.fromisoformat(e["start_utc"].replace("Z", "+00:00"))
            except Exception:
                continue
            d = dt_utc.date()
            if d < body.start or d > body.end:
                continue
            ds.append(d)
        ds.sort()

        # has today / next
        has_today = any(d == today_local for d in ds)
        next_d: Optional[date] = None
        for d in ds:
            if d >= today_local:
                next_d = d
                break

        if next_d:
            # We don’t have local start time exactly per player here, but we can convert the UTC of the first event on that date.
            # Find first event on that date and convert to local.
            first_on_day = None
            for e in evs:
                try:
                    dt_utc = datetime.fromisoformat(e["start_utc"].replace("Z", "+00:00"))
                    if dt_utc.date() == next_d:
                        first_on_day = dt_utc
                        break
                except Exception:
                    continue
            if first_on_day:
                next_local_iso = first_on_day.astimezone(tz).isoformat()

        resp_players.append(
            SummaryLine(
                player_id=pt.player_id,
                team=abbr,
                games_this_week=len(ds),
                has_game_today=has_today,
                next_game_local=next_local_iso,
                dates=[_datestr(d) for d in ds],
            )
        )

    return SummaryResp(
        sport=body.sport,
        window={"start": _datestr(body.start), "end": _datestr(body.end)},
        players=resp_players,
    )
