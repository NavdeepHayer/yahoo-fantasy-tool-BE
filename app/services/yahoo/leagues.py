from __future__ import annotations
from typing import Any, List, Tuple, Optional, Dict
from sqlalchemy.orm import Session

from app.db.models import User  # not used here but kept for symmetry if needed later
from app.core.config import settings
from app.services.yahoo.client import yahoo_get
from app.services.yahoo.parsers import parse_leagues


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


def _fetch_league_settings(db: Session, user_id: str, league_keys: List[str]) -> dict[str, List[str]]:
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


# NEW: lightweight fetch of current_week per league (no settings call)
def _fetch_league_current_week(
    db: Session,
    user_id: str,
    league_keys: List[str],
) -> Dict[str, Optional[int]]:
    """
    Returns mapping { league_key: current_week } for the provided league_keys.
    Uses /leagues;league_keys=... (without /settings) which includes meta like current_week.
    """
    result: Dict[str, Optional[int]] = {}
    if not league_keys:
        return result

    keys_param = ",".join(league_keys)
    payload = yahoo_get(db, user_id, f"/leagues;league_keys={keys_param}")
    fc = payload.get("fantasy_content", {})
    leagues_node = fc.get("leagues")

    if not isinstance(leagues_node, dict):
        return result

    for idx, node in leagues_node.items():
        if not str(idx).isdigit() or not isinstance(node, dict):
            continue

        league_list = node.get("league")
        meta = None
        if isinstance(league_list, list) and league_list:
            # first element typically has meta fields (league_key, name, current_week, etc.)
            first = league_list[0]
            if isinstance(first, dict):
                meta = first
        elif isinstance(league_list, dict):
            meta = league_list

        if not isinstance(meta, dict):
            continue

        league_key = meta.get("league_key") or meta.get("league_id")
        if not league_key:
            continue

        cw = meta.get("current_week")
        try:
            cw_int = int(str(cw)) if cw is not None else None
        except Exception:
            cw_int = None

        result[str(league_key)] = cw_int

    return result


def get_leagues(
    db: Session,
    user_id: str,
    sport: Optional[str] = None,
    season: Optional[int] = None,
    game_key: Optional[str] = None,
) -> List[dict]:
    if settings.YAHOO_FAKE_MODE:
        return [{
            "id": "123.l.4567",
            "name": "Nav’s H2H",
            "season": "2024",
            "scoring_type": "h2h",
            "categories": ["PTS", "REB", "AST", "3PTM", "ST", "BLK", "FG%", "FT%"],
            # keep FE consistent in dev
            "current_week": None,
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
            return []

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

            # 1) categories enrichment (existing)
            mapping = _fetch_league_settings(db, user_id, [L["id"] for L in chunk if "id" in L])
            for L in chunk:
                if L.get("id") in mapping:
                    L["categories"] = mapping[L["id"]]

            # 2) ✅ current_week enrichment (new)
            cw_map = _fetch_league_current_week(db, user_id, [L["id"] for L in chunk if "id" in L])
            for L in chunk:
                lid = L.get("id")
                if not lid:
                    continue
                if lid in cw_map:
                    L["current_week"] = cw_map[lid]
                else:
                    # ensure key exists even if not provided by Yahoo
                    L.setdefault("current_week", None)

    return leagues
