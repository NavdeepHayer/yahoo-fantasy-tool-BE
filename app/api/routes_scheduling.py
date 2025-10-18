# app/api/routes_scheduling.py
from __future__ import annotations

from fastapi import APIRouter, Query, HTTPException, Body, Depends
from pydantic import BaseModel, Field
from typing import Literal, Dict, Any, List, Tuple, Optional
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# deps for Yahoo context (db + current user)
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.deps import get_current_user

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
_CFG = {
    "nhl": {"sport": "hockey", "league": "nhl"},
    "nba": {"sport": "basketball", "league": "nba"},
}

def _datestr(dt: date) -> str:
    return dt.isoformat()

def _yyyymmdd(dt: date) -> str:
    return dt.strftime("%Y%m%d")

# ----------------------------
# In-memory caches (daily invalidation)
# ----------------------------
_CACHE_DAY: Optional[str] = None  # yyyy-mm-dd (UTC)
_TEAM_MAP: Dict[str, Dict[str, Any]] = {}  # sport -> { "abbr_to_id":{NYR:3}, "id_to_abbr":{3:NYR} }
_SCHED_CACHE: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}      # (sport, teamId) -> events list
_SCHED_SPAN: Dict[Tuple[str, str], Tuple[date, date]] = {}           # (sport, teamId) -> (min_date, max_date)

def _maybe_roll_daily_cache():
    global _CACHE_DAY, _TEAM_MAP, _SCHED_CACHE, _SCHED_SPAN
    today = datetime.utcnow().date().isoformat()
    if _CACHE_DAY != today:
        _CACHE_DAY = today
        _TEAM_MAP.clear()
        _SCHED_CACHE.clear()
        _SCHED_SPAN.clear()

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

def _parse_event_date_iso(e: Dict[str, Any]) -> Optional[date]:
    try:
        return datetime.fromisoformat(str(e["start_utc"]).replace("Z", "+00:00")).date()
    except Exception:
        return None

def _fetch_team_schedule_raw(sport: str, team_id: str, start: date, end: date) -> List[Dict[str, Any]]:
    cfg = _CFG[sport]
    window = f"{_yyyymmdd(start)}-{_yyyymmdd(end)}"
    data = _espn_get(
        f"/sports/{cfg['sport']}/{cfg['league']}/teams/{team_id}/schedule",
        params={"dates": window},
    )
    events = data.get("events") or []
    out = []
    for ev in events:
        start_iso = ev.get("date")  # ISO8601 (UTC)
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

def _merge_events_unique(existing: List[Dict[str, Any]], fresh: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    for e in existing:
        eid = str(e.get("id") or "")
        if eid:
            by_id[eid] = e
    for e in fresh:
        eid = str(e.get("id") or "")
        if eid and eid not in by_id:
            by_id[eid] = e
    return list(by_id.values())

def _span_of_events(evs: List[Dict[str, Any]]) -> Optional[Tuple[date, date]]:
    ds = [d for d in (_parse_event_date_iso(e) for e in evs) if d]
    if not ds:
        return None
    return (min(ds), max(ds))

def _get_team_schedule_cached(sport: str, team_id: str, start: date, end: date) -> List[Dict[str, Any]]:
    """
    Cache policy:
      - First fetch → store list and its [min,max] span.
      - Later requests outside the cached span → re-fetch requested window and merge.
      - ESPN often returns season-long; this keeps it robust if it doesn't.
    """
    _maybe_roll_daily_cache()
    key = (sport, team_id)
    cached = _SCHED_CACHE.get(key)
    span = _SCHED_SPAN.get(key)

    if cached is None:
        evs = _fetch_team_schedule_raw(sport, team_id, start, end)
        _SCHED_CACHE[key] = evs
        span = _span_of_events(evs) or (start, end)
        _SCHED_SPAN[key] = span
        return evs

    # If we don't cover the requested window, fetch and merge
    if span is None or start < span[0] or end > span[1]:
        fresh = _fetch_team_schedule_raw(sport, team_id, start, end)
        merged = _merge_events_unique(cached, fresh)
        _SCHED_CACHE[key] = merged
        span2 = _span_of_events(merged) or (start, end)
        _SCHED_SPAN[key] = span2
        return merged

    return cached

# ----------------------------
# Week detection & team inference
# ----------------------------
def _detect_current_week_window_from_yahoo(db: Session, guid: str, league_id: Optional[str]) -> Optional[tuple[date, date]]:
    if not league_id:
        return None
    try:
        from app.services.yahoo.client import yahoo_get
        raw = yahoo_get(db, guid, f"/league/{league_id}/scoreboard") or {}
        found: Dict[str, str] = {}
        def scan(x: Any):
            if isinstance(x, dict):
                for k, v in x.items():
                    if k in {"week_start", "week_end"} and isinstance(v, str):
                        found[k] = v
                    scan(v)
            elif isinstance(x, list):
                for it in x:
                    scan(it)
        scan(raw)
        if "week_start" in found and "week_end" in found:
            return date.fromisoformat(found["week_start"]), date.fromisoformat(found["week_end"])
    except Exception:
        pass
    return None

def _detect_current_week_window(db: Session, guid: str, league_id: Optional[str]) -> tuple[date, date]:
    # prefer Yahoo scoreboard; else fallback to Mon–Sun of today
    yahoo = _detect_current_week_window_from_yahoo(db, guid, league_id)
    if yahoo:
        return yahoo
    today = date.today()
    ws = today - timedelta(days=today.weekday())  # Monday
    we = ws + timedelta(days=6)                   # Sunday
    return ws, we

def _infer_team_from_yahoo(db: Session, guid: str, player_id: str, league_id: Optional[str]) -> Optional[str]:
    try:
        # Try both profile endpoints you likely have; wrap to avoid hard dependency
        try:
            from app.services.yahoo.players import get_player as yahoo_get_player
            prof = yahoo_get_player(db, player_id, league_id=league_id)
        except Exception:
            prof = None
        if not prof:
            try:
                from app.services.yahoo.client import yahoo_get
                prof = yahoo_get(db, guid, f"/players/by-id/{player_id}", params={"league_id": league_id})
            except Exception:
                prof = None
        if isinstance(prof, dict):
            team = prof.get("team") or prof.get("editorial_team_abbr") or None
            if isinstance(team, str) and team.strip():
                return team.strip().upper()
    except Exception:
        pass
    return None

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
        "errors": errors[:10],
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

    def within(e):
        d = _parse_event_date_iso(e)
        return bool(d and (start <= d <= end))

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
    team: Optional[str] = Field(
        default=None,
        description="Team abbreviation, e.g., NYR / BOS / LAL. Optional if league_id provided (will attempt inference)."
    )

class SummaryReq(BaseModel):
    sport: Literal["nhl", "nba"]
    players: List[PlayerTeam]
    start: Optional[date] = None
    end: Optional[date] = None
    league_id: Optional[str] = Field(default=None, description="Yahoo league id for week detection and team inference")
    tz: str = "America/Toronto"

class SummaryLine(BaseModel):
    player_id: str
    team: Optional[str] = None
    games_this_week: int
    has_game_today: bool
    next_start_local: Optional[str] = None  # preferred
    next_game_local: Optional[str] = None   # back-compat
    dates: List[str] = []

class SummaryResp(BaseModel):
    sport: str
    window: Dict[str, str]
    players: List[SummaryLine]
    meta: Dict[str, Any] = {}

def _local_iso(dt_utc_str: Optional[str], tz: ZoneInfo) -> Optional[str]:
    if not dt_utc_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_utc_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(tz).isoformat()
    except Exception:
        return None

@router.post("/summary", response_model=SummaryResp)
def schedule_summary(
    body: SummaryReq = Body(...),
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
):
    # Resolve window (allow omitted start/end)
    if body.start and body.end and body.end < body.start:
        raise HTTPException(status_code=422, detail="end must be >= start")

    if not (body.start and body.end):
        ws, we = _detect_current_week_window(db, guid, body.league_id)
    else:
        ws, we = body.start, body.end

    tm = _ensure_team_map(body.sport)
    tz = ZoneInfo(body.tz)
    today_local = datetime.now(tz).date()

    # Infer missing teams if league_id provided
    resolved: List[PlayerTeam] = []
    missing: List[str] = []
    for pt in body.players:
        abbr = (pt.team or "").strip().upper()
        if not abbr and body.league_id:
            abbr = _infer_team_from_yahoo(db, guid, pt.player_id, body.league_id) or ""
        if not abbr:
            missing.append(pt.player_id)
            continue
        resolved.append(PlayerTeam(player_id=pt.player_id, team=abbr))

    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Missing team for player_ids (provide team or league_id for inference): {', '.join(missing)}"
        )

    # Build per-team schedule (cached; auto-extends when needed)
    team_to_events: Dict[str, List[Dict[str, Any]]] = {}
    for rp in resolved:
        abbr = (rp.team or "").upper()
        tid = tm["abbr_to_id"].get(abbr)
        if not tid:
            raise HTTPException(status_code=400, detail=f"Unknown {body.sport} team abbr: {abbr}")
        if tid not in team_to_events:
            team_to_events[tid] = _get_team_schedule_cached(body.sport, tid, ws, we)

    resp_players: List[SummaryLine] = []
    for rp in resolved:
        abbr = (rp.team or "").upper()
        tid = tm["abbr_to_id"][abbr]
        evs = team_to_events.get(tid, [])

        # Filter to [ws,we] and collect dates
        ds: List[date] = []
        for e in evs:
            d = _parse_event_date_iso(e)
            if d and ws <= d <= we:
                ds.append(d)
        ds.sort()

        has_today = any(d == today_local for d in ds)

        # Next start local (first event on or after 'today' in window)
        next_local_iso: Optional[str] = None
        if ds:
            next_d = next((d for d in ds if d >= today_local), None)
            if next_d:
                for e in evs:
                    d = _parse_event_date_iso(e)
                    if d == next_d:
                        next_local_iso = _local_iso(e.get("start_utc"), tz)
                        if next_local_iso:
                            break

        resp_players.append(
            SummaryLine(
                player_id=rp.player_id,
                team=abbr,
                games_this_week=len(ds),
                has_game_today=has_today,
                next_start_local=next_local_iso,
                next_game_local=next_local_iso,  # back-compat
                dates=[_datestr(d) for d in ds],
            )
        )

    meta: Dict[str, Any] = {
        "source": "espn",
        "sport": body.sport,
        "auto_week": not (body.start and body.end),
    }

    return SummaryResp(
        sport=body.sport,
        window={"start": _datestr(ws), "end": _datestr(we)},
        players=resp_players,
        meta=meta,
    )

# --- GET shim for quick browser testing (optional) ---
@router.get("/summary", response_model=SummaryResp)
def schedule_summary_get(
    sport: Literal["nhl", "nba"] = Query(...),
    players: List[str] = Query([], description="Repeat: pid:TEAM, e.g. 465.p.4240:NYR (TEAM optional if league_id provided)"),
    start: Optional[date] = Query(None),
    end: Optional[date] = Query(None),
    tz: str = Query("America/Toronto"),
    league_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    guid: str = Depends(get_current_user),
):
    body_players: List[PlayerTeam] = []
    for token in players:
        # token form: "<player_id>:<TEAM>" or just "<player_id>"
        if ":" in token:
            pid, _, team = token.partition(":")
            body_players.append(PlayerTeam(player_id=pid.strip(), team=team.strip().upper()))
        else:
            body_players.append(PlayerTeam(player_id=token.strip(), team=None))

    body = SummaryReq(
        sport=sport,
        players=body_players,
        start=start,
        end=end,
        tz=tz,
        league_id=league_id,
    )
    return schedule_summary(body, db, guid)
