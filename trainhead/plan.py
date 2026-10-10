"""Weekly candidate list: catalog routes filtered by the rules, scored on
weather, and checked against real train connections.

The Thursday Claude run picks from this list; nothing here is the final plan.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from . import transit as T
from . import weather as W

log = logging.getLogger(__name__)

DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def weekend_dates(today: date, days: list[str]) -> list[date]:
    """The next occurrence of each requested weekday, counting today."""
    out = []
    for name in days:
        wd = DAY_NAMES.index(name.lower())
        delta = (wd - today.weekday()) % 7
        out.append(today + timedelta(days=delta))
    return sorted(out)


def _hhmm(day: date, hhmm: str, tz: ZoneInfo) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=tz)


def recent_route_ids(history: list[dict], today: date, weeks: int) -> set[str]:
    cutoff = today - timedelta(weeks=weeks)
    return {
        h["route_id"] for h in history
        if h.get("rating") not in (None, "skipped") and date.fromisoformat(h["date"]) >= cutoff
    }


def too_hard_ids(history: list[dict]) -> set[str]:
    return {h["route_id"] for h in history if h.get("rating") == "too_hard"}


def prefilter(routes: list[dict], settings: dict, history: list[dict], today: date) -> dict[str, list[dict]]:
    hk = settings["hiking"]
    rn = settings["running"]
    skip = recent_route_ids(history, today, settings["picks"]["avoid_repeat_weeks"]) | too_hard_ids(history)
    hikes, runs, fallbacks = [], [], []
    for r in routes:
        if r["id"] in skip:
            continue
        if r["max_sac"] > hk["max_sac"] or r.get("busy_road_km", 0) > 1.5:
            continue
        din_h = r["din_min"] / 60
        if (hk["din_hours"][0] <= din_h <= hk["din_hours"][1] and r["ascent_m"] <= hk["max_ascent_m"]
                and r["t3_km"] <= hk["max_t3_km"]):
            hikes.append(r)
        if (rn.get("enabled") and r["max_sac"] <= rn["max_sac"]
                and rn["distance_km"][0] <= r["distance_km"] <= rn["distance_km"][1]
                and r["ascent_m"] <= rn["max_ascent_m"]):
            runs.append(r)
        if r["max_ele_m"] <= 1100 and 2.0 <= din_h <= 4.5 and r["max_sac"] <= 2:
            fallbacks.append(r)
    return {"hike": hikes, "run": runs, "fallback": fallbacks}


WX_STEP_DEG = 0.04   # ~4 km cells; ICON-D2 runs at 2.2 km, so neighbouring routes share a cell
WX_REF_ELE = 1000    # every point is fetched at this height; temperatures are moved to the real height
LAPSE_C_PER_M = 0.0065


def _q(value: float, step: float) -> float:
    return round(round(value / step) * step, 6)


def _wx_key(lat: float, lon: float) -> str:
    return f"{_q(lat, WX_STEP_DEG):.2f},{_q(lon, WX_STEP_DEG):.2f}"


def weather_points(routes: list[dict]) -> list[dict]:
    """One forecast point per ~4 km cell that holds a route's start or destination.
    Keeps the weekly request to a few hundred points, inside Open-Meteo's
    free per-minute limit."""
    pts = {}
    for r in routes:
        for lat, lon in ((r["start"]["lat"], r["start"]["lon"]), (r["target"]["lat"], r["target"]["lon"])):
            k = _wx_key(lat, lon)
            pts[k] = {"key": k, "lat": _q(lat, WX_STEP_DEG), "lon": _q(lon, WX_STEP_DEG), "ele": WX_REF_ELE}
    return list(pts.values())


def duration_min(route: dict, kind: str, settings: dict) -> float:
    if kind == "run":
        return route["run_min"] * 1.1
    return route["din_min"] * settings["hiking"]["break_factor"]


def route_weather(route: dict, wx: dict, start: datetime, end: datetime, rules: dict) -> dict | None:
    valley = wx.get(_wx_key(route["start"]["lat"], route["start"]["lon"]))
    top = wx.get(_wx_key(route["target"]["lat"], route["target"]["lon"]))
    if not valley or not top:
        return None
    a = W.assess(valley, top, start.replace(tzinfo=None), end.replace(tzinfo=None), route["max_ele_m"], rules,
                 valley_ele=route["min_ele_m"], ref_ele=WX_REF_ELE)
    ss = W.sunset(valley, start.date().isoformat())
    if ss and end.replace(tzinfo=None) > ss - timedelta(minutes=30):
        a["flags"].append(f"finishes after sunset ({ss.strftime('%H:%M')})")
        a["score"] = round(max(0.0, a["score"] - 0.4), 3)
    return a


def _brief(r: dict) -> dict:
    keys = ("id", "name", "type", "distance_km", "ascent_m", "descent_m", "max_ele_m", "din_min",
            "run_min", "max_sac", "t3_km", "marked_share", "unpaved_share", "road_km", "score")
    out = {k: r.get(k) for k in keys}
    out["start"] = r["start"]["name"]
    out["start_mode"] = r["start"].get("mode", "rail")
    out["end"] = r["end"]["name"]
    out["end_mode"] = r["end"].get("mode", "rail")
    out["target"] = {k: r["target"].get(k) for k in ("name", "kind", "ele")}
    return out


QUOTAS = {"hike": 8, "run": 3, "fallback": 2}
MAX_ATTEMPTS_FACTOR = 4   # look this far past the quota when connections fail
PER_STATION = 2
PER_AREA = 2              # candidates starting in the same ~10 km cell
AREA_DEG = 0.1


def _area(r: dict) -> tuple[int, int]:
    return round(r["start"]["lat"] / AREA_DEG), round(r["start"]["lon"] / AREA_DEG)


def run_weekly(settings: dict, routes: list[dict], history: list[dict], today: date,
               fetch_weather: Callable[..., dict], client, now: datetime | None = None) -> dict:
    tz = ZoneInfo(settings["schedule"]["timezone"])
    sched = settings["schedule"]
    rules = settings["weather"]
    days = weekend_dates(today, sched["days"])
    pools = prefilter(routes, settings, history, today)
    log.info("pool sizes: %s", {k: len(v) for k, v in pools.items()})

    all_routes = {r["id"]: r for pool in pools.values() for r in pool}
    pts = weather_points(list(all_routes.values()))
    log.info("%d weather points", len(pts))
    wx = fetch_weather(pts, days[0].isoformat(), days[-1].isoformat(), sched["timezone"])

    home = None
    try:
        home = client.geocode_stop(settings["home"]["stop"], near=(settings["home"]["lat"], settings["home"]["lon"]))
    except Exception as exc:  # the fallback coordinates still work
        log.warning("geocoding home failed: %s", exc)
    if not home:
        home = {"name": settings["home"]["stop"], "lat": settings["home"]["lat"], "lon": settings["home"]["lon"]}
    home_place = T.place(home["lat"], home["lon"])

    ctx = {"out": {}, "ret": {}, "errors": 0, "calls": 0}
    result = {
        "generated_at": (now or datetime.now(tz)).isoformat(timespec="minutes"),
        "home": home,
        "days": {},
        "notes": [],
    }

    for day in days:
        earliest = _hhmm(day, sched["earliest_departure"], tz)
        latest_home = _hhmm(day, sched["latest_home"], tz)
        day_out = {"date": day.isoformat(), "weekday": DAY_NAMES[day.weekday()]}
        for kind, pool in pools.items():
            prelim = []
            for r in pool:
                start = _hhmm(day, "09:00", tz)
                end = start + timedelta(minutes=duration_min(r, kind, settings))
                a = route_weather(r, wx, start, end, rules)
                if a is None:
                    continue
                weight = a["score"] if kind != "fallback" else (1.0 - a["precip_mm"] / 10)
                prelim.append((r["score"] * (0.35 + 0.65 * max(weight, 0)), r))
            prelim.sort(key=lambda x: -x[0])
            chosen, attempts, per_station, per_area, targets = [], 0, {}, {}, set()
            quota = QUOTAS[kind]
            for _, r in prelim:
                if len(chosen) >= quota or attempts >= quota * MAX_ATTEMPTS_FACTOR:
                    break
                st, tg, ar = r["start"]["name"], r["target"]["name"], _area(r)
                if per_station.get(st, 0) >= PER_STATION or per_area.get(ar, 0) >= PER_AREA or tg in targets:
                    continue
                attempts += 1
                entry = _with_transit(r, kind, day, earliest, latest_home, home_place, client, settings,
                                      wx, rules, ctx, tz)
                if entry:
                    entry["_route"] = r
                    chosen.append(entry)
                    per_station[st] = per_station.get(st, 0) + 1
                    per_area[ar] = per_area.get(ar, 0) + 1
                    targets.add(tg)
            # The return after the walk is looked up only for what made the list.
            for entry in chosen:
                r = entry.pop("_route")
                if entry.get("transit_checked"):
                    _add_suggested_return(entry, r, home_place, client, settings, ctx, tz)
            chosen.sort(key=lambda e: -e["rank_score"])
            day_out[kind + "s"] = chosen
        result["days"][day.isoformat()] = day_out

    log.info("%d connection lookups, %d failed", ctx["calls"], ctx["errors"])
    if ctx["calls"] and ctx["errors"] >= max(3, ctx["calls"] // 2):
        result["notes"].append(
            f"Connection checks failed for {ctx['errors']} of {ctx['calls']} requests; "
            "candidates without a checked connection are marked transit_checked=false."
        )
    return result


def _plan(client, ctx, *args, **kwargs):
    ctx["calls"] += 1
    try:
        return client.plan(*args, **kwargs)
    except T.TransitError as exc:
        ctx["errors"] += 1
        log.warning("connection lookup failed: %s", exc)
        return None


def _with_transit(r, kind, day, earliest, latest_home, home_place, client, settings, wx, rules, ctx, tz):
    """Adds connections to a candidate. Returns None when there is no usable
    connection; keeps the candidate unchecked when the lookup itself failed."""
    tr = settings["transit"]
    sched = settings["schedule"]
    kw = {"regional_only": tr["regional_only"], "max_transfers": tr["max_transfers"]}

    key_o = (r["start"]["name"], r["start"]["lat"], day)
    if key_o not in ctx["out"]:
        resp = _plan(client, ctx, home_place, T.place(r["start"]["lat"], r["start"]["lon"]), earliest, num=4, **kw)
        ctx["out"][key_o] = None if resp is None else T.best_outbound(resp, earliest, tz, tr["max_one_way_min"])
    outbound = ctx["out"][key_o]

    key_r = (r["end"]["name"], r["end"]["lat"], day)
    if key_r not in ctx["ret"]:
        resp = _plan(client, ctx, T.place(r["end"]["lat"], r["end"]["lon"]), home_place, latest_home,
                     arrive_by=True, num=3, **kw)
        ctx["ret"][key_r] = "ERR" if resp is None else T.latest_return(resp, tz, tr["max_one_way_min"])
    last = ctx["ret"][key_r]

    if outbound is None or last == "ERR":
        return _unchecked(r, kind, day, settings, wx, rules, tz)
    if not outbound or not last:
        return None

    first = outbound[0]
    walk_start = datetime.fromisoformat(first["arr"]) + timedelta(minutes=10)
    finish = walk_start + timedelta(minutes=duration_min(r, kind, settings))
    last_dep = datetime.fromisoformat(last["dep"])
    buffer_min = (last_dep - finish).total_seconds() / 60
    if buffer_min < sched["last_train_buffer_min"]:
        return None

    a = route_weather(r, wx, walk_start, finish, rules) or {"score": 0, "flags": ["no weather data"]}
    issues = sorted(set(first["issues"] + last["issues"]))
    covered = first["covered_by_dticket"] and last["covered_by_dticket"]
    travel_min = first["duration_min"] + last["duration_min"]
    rank = r["score"] * (0.35 + 0.65 * a["score"]) - min(travel_min / 600, 0.25) - (0.15 if not covered else 0)
    return {
        "route": _brief(r),
        "kind": kind,
        "transit_checked": True,
        "weather": a,
        "outbound": first,
        "outbound_alternatives": outbound[1:3],
        "walk_start": walk_start.strftime("%H:%M"),
        "expected_finish": finish.strftime("%H:%M"),
        "suggested_return": None,
        "last_sensible_return": last,
        "buffer_min": int(buffer_min),
        "covered_by_dticket": covered,
        "transit_issues": issues,
        "travel_min_total": travel_min,
        "rank_score": round(rank, 4),
    }


def _add_suggested_return(entry, r, home_place, client, settings, ctx, tz):
    """The first connection home after the walk, 15 minutes after the expected finish."""
    tr = settings["transit"]
    last = entry["last_sensible_return"]
    finish = datetime.fromisoformat(last["dep"]).replace(
        hour=int(entry["expected_finish"][:2]), minute=int(entry["expected_finish"][3:5]))
    after = finish + timedelta(minutes=15)
    resp = _plan(client, ctx, T.place(r["end"]["lat"], r["end"]["lon"]), home_place, after, num=2,
                 regional_only=tr["regional_only"], max_transfers=tr["max_transfers"])
    suggested = T.first_after(resp, after, tz, tr["max_one_way_min"]) if resp else None
    if suggested and suggested["dep"] > last["dep"]:
        suggested = None  # the only later option is past the last sensible one
    if suggested:
        entry["suggested_return"] = suggested
        entry["transit_issues"] = sorted(set(entry["transit_issues"] + suggested["issues"]))
        entry["covered_by_dticket"] = entry["covered_by_dticket"] and suggested["covered_by_dticket"]
        entry["travel_min_total"] = entry["outbound"]["duration_min"] + suggested["duration_min"]


def _unchecked(r, kind, day, settings, wx, rules, tz):
    walk_start = _hhmm(day, "09:30", tz)
    finish = walk_start + timedelta(minutes=duration_min(r, kind, settings))
    a = route_weather(r, wx, walk_start, finish, rules) or {"score": 0, "flags": ["no weather data"]}
    return {
        "route": _brief(r),
        "kind": kind,
        "transit_checked": False,
        "weather": a,
        "walk_start": walk_start.strftime("%H:%M"),
        "expected_finish": finish.strftime("%H:%M"),
        "transit_issues": ["connections could not be checked; confirm in DB Navigator before leaving"],
        "rank_score": round(r["score"] * (0.35 + 0.65 * a["score"]) - 0.3, 4),
    }


def run_recheck(settings: dict, routes_by_id: dict, plan: dict, fetch_weather, client) -> dict:
    """Compare the chosen picks against fresh weather and connections."""
    tz = ZoneInfo(settings["schedule"]["timezone"])
    rules = settings["weather"]
    tr = settings["transit"]
    picks = plan.get("picks", [])
    routes = [routes_by_id[p["route_id"]] for p in picks if p["route_id"] in routes_by_id]
    if not routes:
        return {"changed": False, "checks": [], "note": "no picks to check"}
    days = sorted({p["date"] for p in picks})
    wx = fetch_weather(weather_points(routes), days[0], days[-1], settings["schedule"]["timezone"])
    home = plan.get("home") or settings["home"]
    home_place = T.place(home["lat"], home["lon"])
    checks = []
    changed = False
    for p in picks:
        r = routes_by_id.get(p["route_id"])
        if not r:
            continue
        day = date.fromisoformat(p["date"])
        start = _hhmm(day, p.get("walk_start", "09:00"), tz)
        finish = _hhmm(day, p.get("expected_finish", "16:00"), tz)
        a = route_weather(r, wx, start, finish, rules) or {"score": 0, "flags": ["no weather data"], "storm": False}
        reasons = []
        before = p.get("weather_score")
        if before is not None and a["score"] < before - 0.2:
            reasons.append(f"weather score down from {before:.2f} to {a['score']:.2f}")
        if a.get("storm") and not p.get("storm"):
            reasons.append("thunderstorm risk now in the forecast")
        planned_dep = p.get("outbound_dep")
        try:
            earliest = _hhmm(day, settings["schedule"]["earliest_departure"], tz)
            resp = client.plan(home_place, T.place(r["start"]["lat"], r["start"]["lon"]), earliest, num=4,
                               regional_only=tr["regional_only"], max_transfers=tr["max_transfers"])
            outs = T.best_outbound(resp, earliest, tz, tr["max_one_way_min"])
            same = [o for o in outs if planned_dep and o["dep"][11:16] == planned_dep]
            if planned_dep and not same:
                reasons.append(f"the {planned_dep} train is no longer in the timetable")
            for o in same:
                if o["issues"]:
                    reasons.append("outbound: " + "; ".join(o["issues"]))
        except T.TransitError as exc:
            log.warning("recheck outbound failed: %s", exc)
        if reasons:
            changed = True
        checks.append({"route_id": r["id"], "date": p["date"], "weather": a, "reasons": reasons})
    return {"changed": changed, "checks": checks}
