"""Geometry, timing and file-format helpers.

Everything here is pure and deterministic so it can be unit tested without
network access.
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

import numpy as np

EARTH_RADIUS_M = 6371008.8


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres. Works on scalars and numpy arrays."""
    lat1 = np.radians(lat1)
    lat2 = np.radians(lat2)
    dlat = lat2 - lat1
    dlon = np.radians(lon2) - np.radians(lon1)
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def polyline_length_m(lats: Sequence[float], lons: Sequence[float]) -> float:
    lats = np.asarray(lats, dtype=float)
    lons = np.asarray(lons, dtype=float)
    if len(lats) < 2:
        return 0.0
    return float(haversine_m(lats[:-1], lons[:-1], lats[1:], lons[1:]).sum())


def resample(lats: Sequence[float], lons: Sequence[float], step_m: float = 25.0):
    """Points along a polyline every ~step_m metres, keeping both ends."""
    lats = np.asarray(lats, dtype=float)
    lons = np.asarray(lons, dtype=float)
    if len(lats) < 2:
        return lats.copy(), lons.copy()
    seg = haversine_m(lats[:-1], lons[:-1], lats[1:], lons[1:])
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    total = cum[-1]
    if total == 0:
        return lats[:1].copy(), lons[:1].copy()
    n = max(2, int(math.ceil(total / step_m)) + 1)
    targets = np.linspace(0.0, total, n)
    return np.interp(targets, cum, lats), np.interp(targets, cum, lons)


def smooth(values: Sequence[float], window: int = 3) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    if window <= 1 or len(values) < window:
        return values.copy()
    kernel = np.ones(window) / window
    padded = np.pad(values, (window // 2, window - 1 - window // 2), mode="edge")
    return np.convolve(padded, kernel, mode="valid")


def ascent_descent(elevations: Sequence[float], threshold_m: float = 5.0):
    """Cumulative ascent and descent with a hysteresis threshold.

    DEM samples are noisy; counting every small wiggle inflates ascent. A
    change only counts once it exceeds the threshold relative to the last
    reference point.
    """
    elev = np.asarray(elevations, dtype=float)
    elev = elev[~np.isnan(elev)]
    if len(elev) < 2:
        return 0.0, 0.0
    up = down = 0.0
    ref = elev[0]
    for e in elev[1:]:
        if e >= ref + threshold_m:
            up += e - ref
            ref = e
        elif e <= ref - threshold_m:
            down += ref - e
            ref = e
    return up, down


def din33466_minutes(distance_km: float, ascent_m: float, descent_m: float) -> float:
    """Walking time after DIN 33466, the formula used on German trail signs.

    4 km/h on the flat, 300 m/h up, 500 m/h down. The larger of the
    horizontal and vertical times counts fully, the smaller one half.
    Breaks are not included.
    """
    horizontal = distance_km / 4.0
    vertical = ascent_m / 300.0 + descent_m / 500.0
    return 60.0 * (max(horizontal, vertical) + min(horizontal, vertical) / 2.0)


def trail_run_minutes(distance_km: float, ascent_m: float, pace_min_per_effort_km: float) -> float:
    """Running time from effort distance (km + ascent/100), a common trail heuristic."""
    return (distance_km + ascent_m / 100.0) * pace_min_per_effort_km


def fmt_minutes(minutes: float) -> str:
    minutes = int(round(minutes))
    return f"{minutes // 60}:{minutes % 60:02d} h"


# --- Google encoded polyline (precision 5) -------------------------------

def encode_polyline(lats: Iterable[float], lons: Iterable[float], precision: int = 5) -> str:
    factor = 10 ** precision
    out = []
    prev_lat = prev_lon = 0
    for lat, lon in zip(lats, lons):
        ilat = int(round(lat * factor))
        ilon = int(round(lon * factor))
        for delta in (ilat - prev_lat, ilon - prev_lon):
            value = ~(delta << 1) if delta < 0 else (delta << 1)
            while value >= 0x20:
                out.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            out.append(chr(value + 63))
        prev_lat, prev_lon = ilat, ilon
    return "".join(out)


def decode_polyline(encoded: str, precision: int = 5):
    factor = 10 ** precision
    lats, lons = [], []
    index = lat = lon = 0
    while index < len(encoded):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else (result >> 1)
            if which == 0:
                lat += delta
            else:
                lon += delta
        lats.append(lat / factor)
        lons.append(lon / factor)
    return lats, lons


# --- GPX ------------------------------------------------------------------

def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def to_gpx(name: str, lats, lons, eles=None, description: str = "") -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="trainhead" xmlns="http://www.topografix.com/GPX/1/1">',
        "  <metadata>",
        f"    <name>{_xml_escape(name)}</name>",
        f"    <desc>{_xml_escape(description)}</desc>" if description else "",
        "    <copyright author=\"OpenStreetMap contributors\"><license>https://opendatacommons.org/licenses/odbl/</license></copyright>",
        "  </metadata>",
        "  <trk>",
        f"    <name>{_xml_escape(name)}</name>",
        "    <trkseg>",
    ]
    for i, (lat, lon) in enumerate(zip(lats, lons)):
        ele = ""
        if eles is not None and i < len(eles) and eles[i] is not None and not math.isnan(eles[i]):
            ele = f"<ele>{eles[i]:.0f}</ele>"
        lines.append(f'      <trkpt lat="{lat:.6f}" lon="{lon:.6f}">{ele}</trkpt>')
    lines += ["    </trkseg>", "  </trk>", "</gpx>", ""]
    return "\n".join(line for line in lines if line != "")
