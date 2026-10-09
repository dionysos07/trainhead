"""Settings loader."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_PATH = REPO_ROOT / "config" / "settings.yaml"

DEFAULTS: dict[str, Any] = {
    "home": {"stop": "München Hbf", "lat": 48.1402, "lon": 11.5600},
    "schedule": {
        "timezone": "Europe/Berlin",
        "days": ["saturday", "sunday"],
        "earliest_departure": "06:30",
        "latest_home": "22:30",
        "last_train_buffer_min": 45,
    },
    "transit": {"regional_only": True, "max_one_way_min": 150, "max_transfers": 3},
    "hiking": {
        "max_sac": 3,
        "max_t3_km": 2.0,
        "untagged_path_max_ele_m": 1000,
        "din_hours": [3.0, 7.5],
        "break_factor": 1.15,
        "max_ascent_m": 1400,
    },
    "running": {
        "enabled": True,
        "pace_min_per_effort_km": 7.0,
        "max_sac": 2,
        "distance_km": [10, 24],
        "max_ascent_m": 1200,
    },
    "weather": {
        "max_precip_mm": 1.0,
        "storm_cape_jkg": 800,
        "max_gusts_kmh": 60,
        "snow_depth_max_m": 0.05,
    },
    "picks": {"per_weekend": 2, "fallback": True, "avoid_repeat_weeks": 8},
    "region": {"bbox": [10.60, 47.25, 13.10, 47.97]},
    "catalog": {"bus_stops": True},
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_settings(path: str | Path | None = None) -> dict:
    path = Path(path) if path else DEFAULT_SETTINGS_PATH
    data = {}
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    settings = _merge(DEFAULTS, data)
    validate(settings)
    return settings


def validate(settings: dict) -> None:
    hiking = settings["hiking"]
    if not 1 <= int(hiking["max_sac"]) <= 6:
        raise ValueError("hiking.max_sac must be between 1 and 6")
    lo, hi = hiking["din_hours"]
    if not 0 < lo < hi:
        raise ValueError("hiking.din_hours must be [min, max] with 0 < min < max")
    west, south, east, north = settings["region"]["bbox"]
    if not (west < east and south < north):
        raise ValueError("region.bbox must be [west, south, east, north]")
    if settings["running"]["max_sac"] > hiking["max_sac"]:
        raise ValueError("running.max_sac can't exceed hiking.max_sac")
