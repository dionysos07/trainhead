"""Files for the route page: plan.json plus a zipped GPX per pick.

The Thursday run writes a short `plans/latest.json` (which routes, which day,
why). `enrich_plan` fills in times, connections and weather from
`weekly/latest.json`, so nobody retypes timetable data, and `write_site`
produces what gets published next to the page.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime
from pathlib import Path

from .geo import decode_polyline, fmt_minutes, to_gpx

SAC_NAMES = {
    0: "no difficulty tag",
    1: "T1 path",
    2: "T2 mountain path",
    3: "T3 demanding mountain path",
    4: "T4 alpine route",
    5: "T5 demanding alpine route",
    6: "T6 difficult alpine route",
}
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]


def day_label(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]}"


def _find_entry(weekly: dict, day: str, route_id: str, kind: str | None) -> dict | None:
    d = (weekly.get("days") or {}).get(day) or {}
    for k in ("hikes", "runs", "fallbacks"):
        for e in d.get(k, []):
            if e["route"]["id"] == route_id and (kind is None or e["kind"] == kind or k == "fallbacks"):
                return e
    return None


def _legs(itin: dict | None) -> list[dict]:
    if not itin:
        return []
    return [
        {k: leg.get(k) for k in ("mode", "line", "from", "to", "dep", "arr", "headsign", "cancelled")}
        for leg in itin.get("legs", [])
    ]


def enrich_pick(pick: dict, weekly: dict) -> dict:
    """Copy the weekly data for this pick into it. Fields the run already set win."""
    e = _find_entry(weekly, pick["date"], pick["route_id"], pick.get("kind"))
    if not e:
        pick.setdefault("notes", []).append("not found in this week's candidates; times not checked")
        return pick
    out = dict(pick)
    out.setdefault("kind", e["kind"])
    out.setdefault("name", e["route"]["name"])
    out.setdefault("walk_start", e.get("walk_start"))
    out.setdefault("expected_finish", e.get("expected_finish"))
    out["transit_checked"] = e.get("transit_checked", True)
    out["weather_score"] = e["weather"].get("score")
    out["storm"] = bool(e["weather"].get("storm"))
    out["weather"] = e["weather"]
    if e.get("outbound"):
        out["outbound_dep"] = (e["outbound"].get("dep") or "")[11:16]
        out["outbound"] = e["outbound"]
    if e.get("suggested_return"):
        out["return"] = e["suggested_return"]
    if e.get("last_sensible_return"):
        out["last_sensible_return"] = e["last_sensible_return"]
    out["transit_issues"] = e.get("transit_issues", [])
    out["covered_by_dticket"] = e.get("covered_by_dticket")
    return out


def enrich_plan(plan: dict, weekly: dict) -> dict:
    plan = dict(plan)
    plan["home"] = plan.get("home") or weekly.get("home")
    plan["picks"] = [enrich_pick(p, weekly) for p in plan.get("picks", [])]
    if plan.get("fallback"):
        fb = dict(plan["fallback"])
        fb.setdefault("date", min(p["date"] for p in plan["picks"]) if plan["picks"] else None)
        fb.setdefault("kind", "hike")
        plan["fallback"] = enrich_pick(fb, weekly) if fb.get("date") else fb
    plan.setdefault("weekend", sorted({p["date"] for p in plan["picks"]}))
    return plan


def _card(pick: dict, route: dict) -> dict:
    ret = pick.get("return") or {}
    last = pick.get("last_sensible_return") or {}
    return {
        "key": f"{pick['date']}__{route['id']}",
        "date": pick["date"],
        "kind": pick.get("kind", "hike"),
        "why": pick.get("why", ""),
        "notes": pick.get("notes", []) + [i for i in pick.get("transit_issues", []) if i],
        "route": {
            "id": route["id"],
            "name": route["name"],
            "type": route["type"],
            "start": route["start"]["name"],
            "start_mode": route["start"].get("mode", "rail"),
            "end": route["end"]["name"],
            "end_mode": route["end"].get("mode", "rail"),
            "target": {k: route["target"].get(k) for k in ("name", "kind", "ele", "lat", "lon")},
            "distance_km": route["distance_km"],
            "ascent_m": route["ascent_m"],
            "descent_m": route["descent_m"],
            "max_ele_m": route["max_ele_m"],
            "din": fmt_minutes(route["din_min"]),
            "run": fmt_minutes(route["run_min"]),
            "max_sac": route["max_sac"],
            "sac_name": SAC_NAMES.get(route["max_sac"], ""),
            "t3_km": route.get("t3_km", 0),
            "profile": route["profile"],
            "polyline": route["polyline"],
        },
        "times": {"walk_start": pick.get("walk_start"), "finish": pick.get("expected_finish")},
        "transit_checked": pick.get("transit_checked", True),
        "there": _legs(pick.get("outbound")),
        "back": _legs(ret),
        "last": {"dep": (last.get("dep") or "")[11:16], "arr": (last.get("arr") or "")[11:16],
                 "from": route["end"]["name"]} if last else None,
        "dticket": pick.get("covered_by_dticket"),
        "weather": {k: (pick.get("weather") or {}).get(k) for k in
                    ("score", "flags", "temp_min", "temp_max", "precip_mm", "gust_max_kmh", "cloud_mean",
                     "freezing_level_min_m")},
        "gpx": f"gpx/{route['id']}.zip",
    }


def rules_summary(settings: dict) -> list[list[str]]:
    hk, sch, rn = settings["hiking"], settings["schedule"], settings["running"]
    return [
        ["Home stop", settings["home"]["stop"]],
        ["Hardest trail", f"T{hk['max_sac']} ({SAC_NAMES[hk['max_sac']].split(' ', 1)[1]}), "
                          f"at most {hk['max_t3_km']:g} km of T3"],
        ["Walking time", f"{hk['din_hours'][0]:g} to {hk['din_hours'][1]:g} h by DIN 33466, plus breaks"],
        ["Ascent", f"up to {hk['max_ascent_m']:,} m".replace(",", " ")],
        ["Trail runs", f"{rn['distance_km'][0]}–{rn['distance_km'][1]} km, up to T{rn['max_sac']}"
                       if rn.get("enabled") else "off"],
        ["Trains", f"regional only, leave after {sch['earliest_departure']}, home by {sch['latest_home']}"],
        ["Last train", f"back at the station {sch['last_train_buffer_min']} min before the last sensible one"],
    ]


def site_data(plan: dict, routes_by_id: dict, settings: dict, previous: dict | None = None,
              notice: str | None = None, next_plan: str | None = None) -> dict:
    picks = [p for p in plan.get("picks", []) if p.get("route_id") in routes_by_id]
    days = []
    for d in plan.get("weekend", []):
        days.append({
            "date": d,
            "label": day_label(d),
            "summary": (plan.get("day_summaries") or {}).get(d, ""),
            "cards": [_card(p, routes_by_id[p["route_id"]]) for p in picks if p["date"] == d],
        })
    fb = plan.get("fallback")
    prev_items = []
    if previous:
        for p in previous.get("picks", []):
            prev_items.append({"key": f"{p['date']}__{p['route_id']}", "date": p["date"],
                               "route_id": p["route_id"], "kind": p.get("kind", "hike"),
                               "name": p.get("name") or routes_by_id.get(p["route_id"], {}).get("name", p["route_id"]),
                               "day": day_label(p["date"])})
    return {
        "updated": plan.get("updated"),
        "notice": notice if notice is not None else plan.get("notice"),
        "next_plan": next_plan,
        "home": (plan.get("home") or {}).get("name") or settings["home"]["stop"],
        "days": days,
        "fallback": _card(fb, routes_by_id[fb["route_id"]]) if fb and fb.get("route_id") in routes_by_id else None,
        "previous": prev_items,
        "rules": rules_summary(settings),
    }


def gpx_zip(route: dict) -> bytes:
    lats, lons = decode_polyline(route["polyline"])
    desc = (f"{route['distance_km']} km, {route['ascent_m']} m up, {route['descent_m']} m down, "
            f"T{route['max_sac']}. Data: OpenStreetMap contributors (ODbL).")
    gpx = to_gpx(route["name"], lats, lons, description=desc)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{route['id']}.gpx", gpx)
    return buf.getvalue()


def write_site(data: dict, routes_by_id: dict, out_dir: str | Path) -> list[str]:
    out = Path(out_dir)
    (out / "gpx").mkdir(parents=True, exist_ok=True)
    (out / "plan.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    written = ["plan.json"]
    cards = [c for d in data["days"] for c in d["cards"]] + ([data["fallback"]] if data["fallback"] else [])
    for c in cards:
        rid = c["route"]["id"]
        path = out / "gpx" / f"{rid}.zip"
        path.write_bytes(gpx_zip(routes_by_id[rid]))
        written.append(f"gpx/{rid}.zip")
    return written


def now_label(tz) -> str:
    return datetime.now(tz).isoformat(timespec="minutes")
