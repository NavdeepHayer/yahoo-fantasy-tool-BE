from __future__ import annotations
from typing import Any, List, Tuple
from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.yahoo_client import yahoo_get

def get_teams_for_user(db: Session, user_id: str, league_id: str) -> List[dict]:
    if settings.YAHOO_FAKE_MODE:
        return [
            {"id": f"{league_id}.t.1", "name": "Nav’s Team", "manager": "Nav", "manager_name": "Nav"},
            {"id": f"{league_id}.t.2", "name": "Rival Squad", "manager": "Alex", "manager_name": "Alex"},
        ]

    payload = yahoo_get(db, user_id, f"/league/{league_id}/teams")

    fc = payload.get("fantasy_content", {})
    league_node = fc.get("league", {})
    if isinstance(league_node, list):
        teams_container = league_node[1].get("teams", {}) if len(league_node) >= 2 and isinstance(league_node[1], dict) else {}
    elif isinstance(league_node, dict):
        teams_container = league_node.get("teams", {})
    else:
        teams_container = {}

    out: List[dict] = []

    for k, v in teams_container.items():
        if not str(k).isdigit():
            continue
        if not isinstance(v, dict):
            continue

        team_block = v.get("team")
        if team_block is None:
            continue

        agg = {}

        def merge_dict(d: dict):
            for kk, vv in d.items():
                agg[kk] = vv

        if isinstance(team_block, dict):
            merge_dict(team_block)
        elif isinstance(team_block, list):
            first = team_block[0] if team_block else None
            if isinstance(first, list):
                for part in first:
                    if isinstance(part, dict):
                        merge_dict(part)
            else:
                for part in team_block:
                    if isinstance(part, dict):
                        merge_dict(part)

        team_key = agg.get("team_key")
        name = agg.get("name")
        if isinstance(name, dict):
            name = name.get("full") or name.get("name")

        manager_guid = None
        manager_name = None
        managers = agg.get("managers")
        if isinstance(managers, list) and managers:
            m = managers[0].get("manager", {}) if isinstance(managers[0], dict) else {}
            manager_guid = m.get("guid")
            manager_name = m.get("nickname") or m.get("name")
        elif isinstance(managers, dict):
            for kk, vv in managers.items():
                if str(kk).isdigit() and isinstance(vv, dict):
                    m = vv.get("manager", {})
                    if isinstance(m, dict):
                        manager_guid = m.get("guid") or manager_guid
                        manager_name = m.get("nickname") or manager_name

        out.append({
            "id": team_key,
            "name": name,
            "manager": manager_guid or manager_name,
            "manager_name": manager_name,
        })

    return out

def _find_my_team_key_from_teams_payload(teams_payload: dict, my_guid: str | None = None) -> tuple[str | None, str | None]:
    fc = teams_payload.get("fantasy_content", {})
    found_key = None
    found_name = None

    def name_of(team_obj: dict) -> str | None:
        nm = team_obj.get("name")
        if isinstance(nm, str):
            return nm
        if isinstance(nm, dict):
            return nm.get("full") or nm.get("name")
        return None

    def is_me_team(team_obj: dict) -> bool:
        if str(team_obj.get("is_current_login", "0")) == "1":
            return True
        if str(team_obj.get("is_owned_by_current_login", "0")) == "1":
            return True
        if my_guid:
            mgrs = team_obj.get("managers")
            if isinstance(mgrs, list):
                for it in mgrs:
                    if isinstance(it, dict):
                        m = it.get("manager")
                        if isinstance(m, dict) and m.get("guid") == my_guid:
                            return True
            if isinstance(mgrs, dict):
                for k, v in mgrs.items():
                    if str(k).isdigit() and isinstance(v, dict):
                        m = v.get("manager")
                        if isinstance(m, dict) and m.get("guid") == my_guid:
                            return True
        return False

    def walk(node: Any):
        nonlocal found_key, found_name
        if found_key:
            return
        if isinstance(node, dict):
            t = node.get("team")
            if t is not None:
                if isinstance(t, list):
                    agg = {}
                    for part in t:
                        if isinstance(part, dict):
                            agg.update(part)
                    t = agg
                if isinstance(t, dict) and is_me_team(t):
                    found_key = t.get("team_key")
                    nm = t.get("name")
                    if isinstance(nm, dict):
                        nm = nm.get("full") or nm.get("name")
                    found_name = nm if isinstance(nm, str) else None
                    return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for itm in node:
                walk(itm)

    walk(fc)
    return (found_key, found_name)
