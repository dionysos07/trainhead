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


WX_STEP_DEG = 0.02   # ~2 km, the resolution of the ICON-D2 model
WX_STEP_ELE = 50


def _q(value: float, step: float) -> float:
    return round(round(value / step) * step, 6)


def _wx_point(lat: float, lon: float, ele: float) -> tuple[float, float, int]:
    return _q(lat, WX_STEP_DEG), _q(lon, WX_STEP_DEG), int(_q(ele, WX_STEP_ELE))


def _wx_key(lat: float, lon: float, ele: float) -> str:
    qlat, qlon, qele = _wx_point(lat, lon, ele)
    return f"{qlat:.2f},{qlon:.2f},{qele}"


def weather_points(routes: list[dict]) -> list[dict]:
    """Two forecast points per route: the start (lowest point) and the
    destination (highest point), snapped to a ~2 km / 50 m grid."""
    pts = {}
    for r in routes:
        for lat, lon, ele in (
            (r["start"]["lat"], r["start"]["lon"], r["min_ele_m"]),
            (r["target"]["lat"], r["target"]["lon"], r["max_ele_m"]),
        ):
            k = _wx_key(lat, lon, ele)
            qlat, qlon, qele = _wx_point(lat, lon, ele)
            pts[k] = {"key": k, "lat": qlat, "lon": qlon, "ele": qele}
    return list(pts.values())


def duration_min(route: dict, kind: str, settings: dict) -> float:
    if kind == "run":
        return route["run_min"] * 1.1
    return route["din_min"] * settings["hiking"]["break_factor"]


def route_weather(route: dict, wx: dict, start: datetime, end: datetime, rules: dict) -> dict | None:
    valley = wx.get(_wx_key(route["start"]["lat"], route["start"]["lon"], route["min_ele_m"]))
    top = wx.get(_wx_key(route["target"]["lat"], route["target"]["lon"], route["max_ele_m"]))
    if not valley or not top:
        return None
    a = W.assess(valley, top, start.replace(tzinfo=None), end.replace(tzinfo=None), route["max_ele_m"], rules)
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


QUOTAS = {"hike": 10, "run": 4, "fallback": 3}
MAX_ATTEMPTS_FACTOR = 3   # look this far past the quota when connections fail
PER_STATION = 2


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
            chosen, attempts, per_station, targets = [], 0, {}, set()
            quota = QUOTAS[kind]
            for _, r in prelim:
                if len(chosen) >= quota or attempts >= quota * MAX_ATTEMPTS_FACTOR:
                    break
                st, tg = r["start"]["name"], r["target"]["name"]
                if per_station.get(st, 0) >= PER_STATION or tg in targets:
                    continue
                attempts += 1
                entry = _with_transit(r, kind, day, earliest, latest_home, home_place, client, settings,
                                      wx, rules, ctx, tz)
                if entry:
                    chosen.append(entry)
                    per_station[st] = per_station.get(st, 0) + 1
                    targets.add(tg)
            chosen.sort(key=lambda e: -e["rank_score"])
            day_out[kind + "s"] = chosen
        result["days"][day.isoformat()] = day_out

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

    after = finish + timedelta(minutes=15)
    resp = _plan(client, ctx, T.place(r["end"]["lat"], r["end"]["lon"]), home_place, after, num=2, **kw)
    suggested = T.first_after(resp, after, tz, tr["max_one_way_min"]) if resp else None
    if suggested and suggested["dep"] > last["dep"]:
        suggested = None  # the only later option is past the last sensible one

    a = route_weather(r, wx, walk_start, finish, rules) or {"score": 0, "flags": ["no weather data"]}
    issues = sorted(set(first["issues"] + last["issues"] + (suggested["issues"] if suggested else [])))
    covered = first["covered_by_dticket"] and last["covered_by_dticket"]
    travel_min = first["duration_min"] + (suggested or last)["duration_min"]
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
        "suggested_return": suggested,
        "last_sensible_return": last,
        "buffer_min": int(buffer_min),
        "covered_by_dticket": covered,
        "transit_issues": issues,
        "travel_min_total": travel_min,
        "rank_score": round(rank, 4),
    }


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
