"""Weekend weather from Open-Meteo and how it bears on a route."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

import requests

log = logging.getLogger(__name__)

URL = "https://api.open-meteo.com/v1/forecast"
USER_AGENT = "trainhead/0.1 (personal hiking planner)"

HOURLY_FULL = [
    "temperature_2m", "precipitation", "precipitation_probability", "weather_code",
    "cloud_cover", "wind_gusts_10m", "freezing_level_height", "snow_depth", "cape",
    "lightning_potential", "visibility",
]
HOURLY_CORE = [
    "temperature_2m", "precipitation", "precipitation_probability", "weather_code",
    "cloud_cover", "wind_gusts_10m",
]
DAILY = ["sunrise", "sunset"]
THUNDER_CODES = {95, 96, 99}


def fetch(points: list[dict], start_date: str, end_date: str, timezone: str = "Europe/Berlin",
          batch: int = 50, session: requests.Session | None = None, pause_s: float = 0.5) -> dict:
    """points: [{"key", "lat", "lon", "ele"}]. Returns key -> location forecast dict."""
    session = session or requests.Session()
    out: dict = {}
    hourly = list(HOURLY_FULL)
    for i in range(0, len(points), batch):
        chunk = points[i:i + batch]
        for attempt in range(4):
            params = {
                "latitude": ",".join(f"{p['lat']:.4f}" for p in chunk),
                "longitude": ",".join(f"{p['lon']:.4f}" for p in chunk),
                "elevation": ",".join(str(int(p["ele"])) if p.get("ele") is not None else "nan" for p in chunk),
                "hourly": ",".join(hourly),
                "daily": ",".join(DAILY),
                "timezone": timezone,
                "start_date": start_date,
                "end_date": end_date,
                "wind_speed_unit": "kmh",
            }
            resp = session.get(URL, params=params, timeout=60, headers={"User-Agent": USER_AGENT})
            if resp.status_code == 400 and hourly != HOURLY_CORE:
                log.warning("Open-Meteo rejected the full variable list (%s); using core variables", resp.text[:200])
                hourly = list(HOURLY_CORE)
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(5 * (attempt + 1))
                continue
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                data = [data]
            for p, loc in zip(chunk, data):
                out[p["key"]] = loc
            break
        else:
            raise RuntimeError("Open-Meteo request kept failing")
        time.sleep(pause_s)
    return out


def _series(loc: dict, name: str):
    return (loc.get("hourly") or {}).get(name)


def window(loc: dict, start: datetime, end: datetime) -> list[int]:
    """Indices of hourly values that overlap [start, end)."""
    times = (loc.get("hourly") or {}).get("time") or []
    idx = []
    for i, t in enumerate(times):
        ts = datetime.fromisoformat(t)
        if ts + timedelta(hours=1) > start and ts < end:
            idx.append(i)
    return idx


def _vals(loc, name, idx):
    s = _series(loc, name)
    if not s:
        return []
    return [s[i] for i in idx if i < len(s) and s[i] is not None]


def sunset(loc: dict, day: str) -> datetime | None:
    daily = loc.get("daily") or {}
    for d, ss in zip(daily.get("time") or [], daily.get("sunset") or []):
        if d == day and ss:
            return datetime.fromisoformat(ss)
    return None


def assess(valley: dict, top: dict, start: datetime, end: datetime, top_ele: float, rules: dict) -> dict:
    """Weather over the walking window at the start and at the highest point."""
    iv = window(valley, start, end)
    it = window(top, start, end)
    precip = max(sum(_vals(valley, "precipitation", iv)), sum(_vals(top, "precipitation", it)))
    prob = max(_vals(valley, "precipitation_probability", iv) + _vals(top, "precipitation_probability", it) or [0])
    codes = set(_vals(valley, "weather_code", iv) + _vals(top, "weather_code", it))
    cape = max(_vals(valley, "cape", iv) + _vals(top, "cape", it) or [0])
    lpi = max(_vals(valley, "lightning_potential", iv) + _vals(top, "lightning_potential", it) or [0])
    gust = max(_vals(top, "wind_gusts_10m", it) or [0])
    cloud = _vals(top, "cloud_cover", it)
    cloud_mean = sum(cloud) / len(cloud) if cloud else None
    temps = _vals(top, "temperature_2m", it) + _vals(valley, "temperature_2m", iv)
    fz = _vals(top, "freezing_level_height", it)
    snow = max(_vals(top, "snow_depth", it) or [0])
    vis = _vals(top, "visibility", it)

    storm = bool(codes & THUNDER_CODES) or (cape >= rules["storm_cape_jkg"] and (lpi > 0 or prob >= 40))
    flags = []
    s = 1.0
    if storm:
        flags.append("thunderstorm risk")
        s -= 0.7
    if precip > rules["max_precip_mm"]:
        flags.append(f"{precip:.1f} mm rain")
    s -= min(precip / (2 * rules["max_precip_mm"]), 1.0) * 0.5
    if gust > rules["max_gusts_kmh"]:
        flags.append(f"gusts {gust:.0f} km/h at the top")
        s -= min((gust - rules["max_gusts_kmh"]) / 40, 1.0) * 0.3
    if snow > rules["snow_depth_max_m"]:
        flags.append(f"snow at the top ({snow * 100:.0f} cm)")
        s -= 0.6
    min_fz = min(fz) if fz else None
    if min_fz is not None and min_fz < top_ele + 100 and precip > 0.2:
        flags.append(f"freezing level {min_fz:.0f} m, near or below the top")
        s -= 0.3
    if cloud_mean is not None:
        s -= (cloud_mean / 100) * 0.15
    if vis and min(vis) < 1000:
        flags.append("fog at the top")
        s -= 0.1
    return {
        "score": round(max(0.0, min(1.0, s)), 3),
        "flags": flags,
        "storm": storm,
        "precip_mm": round(precip, 1),
        "precip_prob_max": prob,
        "gust_max_kmh": round(gust),
        "cloud_mean": None if cloud_mean is None else round(cloud_mean),
        "temp_min": None if not temps else round(min(temps)),
        "temp_max": None if not temps else round(max(temps)),
        "freezing_level_min_m": None if min_fz is None else round(min_fz),
        "snow_depth_top_m": round(snow, 2),
    }
