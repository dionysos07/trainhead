"""Train and bus connections via Transitous (a community MOTIS instance built on
open timetable data). Low volume, identified by user agent, paced requests.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)

BASE = "https://api.transitous.org"
USER_AGENT = "trainhead/0.1 (personal weekend hiking planner, a few dozen requests per week)"

REGIONAL_MODES = ["REGIONAL_RAIL", "REGIONAL_FAST_RAIL", "RAIL", "SUBURBAN", "SUBWAY", "METRO", "TRAM", "BUS"]
LONG_DISTANCE_MODES = {"HIGHSPEED_RAIL", "LONG_DISTANCE", "NIGHT_RAIL", "COACH", "AIRPLANE"}
NOT_COVERED_MODES = {"FUNICULAR", "AERIAL_LIFT", "AREAL_LIFT", "CABLE_CAR", "FERRY"}
# Operators whose tickets the Deutschlandticket does not cover.
NOT_COVERED_WORDS = ("zugspitzbahn", "wendelsteinbahn", "bergbahn", "seilbahn", "schifffahrt",
                     "chiemsee-bahn", "flixbus", "flixtrain")


class TransitError(RuntimeError):
    pass


class Client:
    def __init__(self, base: str = BASE, pause_s: float = 1.2, session: requests.Session | None = None):
        self.base = base.rstrip("/")
        self.pause_s = pause_s
        self.session = session or requests.Session()
        self._last = 0.0

    def _get(self, path: str, params: dict) -> object:
        for attempt in range(4):
            wait = self.pause_s - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            resp = self.session.get(
                self.base + path, params=params, timeout=60,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(10 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise TransitError(f"{resp.status_code} for {path}: {resp.text[:200]}")
            return resp.json()
        raise TransitError(f"giving up on {path}")

    def geocode_stop(self, text: str, near: tuple[float, float] | None = None) -> dict | None:
        params = {"text": text, "type": "STOP"}
        if near:
            params["place"] = f"{near[0]},{near[1]}"
        matches = self._get("/api/v1/geocode", params) or []
        for m in matches:
            if m.get("type") == "STOP":
                return {"name": m.get("name"), "id": m.get("id"), "lat": m.get("lat"), "lon": m.get("lon")}
        return None

    def plan(self, from_place: str, to_place: str, when: datetime, arrive_by: bool = False,
             num: int = 3, regional_only: bool = True, max_transfers: int | None = None) -> dict:
        params = {
            "fromPlace": from_place,
            "toPlace": to_place,
            "time": when.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "arriveBy": "true" if arrive_by else "false",
            "numItineraries": num,
            "detailedLegs": "false",
            "language": "de",
        }
        if regional_only:
            params["transitModes"] = ",".join(REGIONAL_MODES)
        if max_transfers is not None:
            params["maxTransfers"] = max_transfers
        return self._get("/api/v6/plan", params)


def place(lat: float, lon: float) -> str:
    return f"{lat:.6f},{lon:.6f}"


def _parse_time(value: str | None, tz: ZoneInfo) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(tz)


def _leg_line(leg: dict) -> str:
    for key in ("displayName", "routeShortName", "tripShortName"):
        if leg.get(key):
            return str(leg[key])
    cat = leg.get("category") or {}
    return cat.get("shortName") or leg.get("mode", "")


LONG_DISTANCE_LINE = re.compile(r"^(ICE|IC|ECE|EC|RJX|RJ|NJ|EN|FLX)\d")


def _leg_covered(leg: dict) -> bool:
    mode = leg.get("mode", "")
    if mode in LONG_DISTANCE_MODES or mode in NOT_COVERED_MODES:
        return False
    line = _leg_line(leg).upper().replace(" ", "")
    if LONG_DISTANCE_LINE.match(line):
        return False
    text = " ".join(str(leg.get(k, "")) for k in ("agencyName", "routeLongName", "displayName")).lower()
    return not any(word in text for word in NOT_COVERED_WORDS)


def summarize(it: dict, tz: ZoneInfo) -> dict:
    legs = []
    issues = []
    covered = True
    for leg in it.get("legs", []):
        mode = leg.get("mode", "")
        if mode == "WALK":
            continue
        dep = _parse_time(leg.get("startTime"), tz)
        arr = _parse_time(leg.get("endTime"), tz)
        alerts = [a.get("headerText") for a in (leg.get("alerts") or []) if a.get("headerText")]
        for p in (leg.get("from") or {}, leg.get("to") or {}):
            alerts += [a.get("headerText") for a in (p.get("alerts") or []) if a.get("headerText")]
        ok = _leg_covered(leg)
        covered = covered and ok
        if leg.get("cancelled"):
            issues.append(f"{_leg_line(leg)} cancelled")
        if not ok:
            issues.append(f"{_leg_line(leg)} not covered by the Deutschlandticket")
        for a in alerts:
            issues.append(a)
        legs.append({
            "mode": mode,
            "line": _leg_line(leg),
            "from": (leg.get("from") or {}).get("name"),
            "to": (leg.get("to") or {}).get("name"),
            "dep": dep.strftime("%H:%M") if dep else None,
            "arr": arr.strftime("%H:%M") if arr else None,
            "headsign": leg.get("headsign"),
            "agency": leg.get("agencyName"),
            "realtime": bool(leg.get("realTime")),
            "cancelled": bool(leg.get("cancelled")),
            "alerts": sorted(set(alerts)),
        })
    start = _parse_time(it.get("startTime"), tz)
    end = _parse_time(it.get("endTime"), tz)
    return {
        "dep": start.isoformat() if start else None,
        "arr": end.isoformat() if end else None,
        "duration_min": round((it.get("duration") or 0) / 60),
        "transfers": it.get("transfers", 0),
        "covered_by_dticket": covered,
        "issues": sorted(set(issues)),
        "legs": legs,
    }


def best_outbound(resp: dict, earliest: datetime, tz: ZoneInfo, max_min: int) -> list[dict]:
    """Connections leaving at or after `earliest`, earliest arrival first."""
    out = []
    for it in resp.get("itineraries", []) or []:
        s = summarize(it, tz)
        if not s["dep"] or datetime.fromisoformat(s["dep"]) < earliest - timedelta(minutes=1):
            continue
        if s["duration_min"] > max_min:
            continue
        out.append(s)
    out.sort(key=lambda s: (s["arr"], s["duration_min"]))
    return out


def latest_return(resp: dict, tz: ZoneInfo, max_min: int) -> dict | None:
    """From an arrive-by query: the connection that leaves the end station last."""
    best = None
    for it in resp.get("itineraries", []) or []:
        s = summarize(it, tz)
        if s["duration_min"] > max_min or not s["dep"]:
            continue
        if best is None or s["dep"] > best["dep"]:
            best = s
    return best


def first_after(resp: dict, after: datetime, tz: ZoneInfo, max_min: int) -> dict | None:
    cands = [s for s in best_outbound(resp, after, tz, max_min)]
    return cands[0] if cands else None
