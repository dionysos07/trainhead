"""Route catalog: day hikes from a station, over a summit, hut or viewpoint,
to the same or another station, using only trails the difficulty rules allow.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import math
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

from . import graph as G
from .dem import DemSampler
from .geo import (
    ascent_descent,
    din33466_minutes,
    encode_polyline,
    haversine_m,
    resample,
    smooth,
    trail_run_minutes,
)
from .osm_extract import OsmData
from .store import load_catalog  # noqa: F401  (re-exported)

log = logging.getLogger(__name__)

KIND_RANK = {"peak": 0, "hut": 1, "viewpoint": 2, "inn": 3}
SNAP_M = {"peak": 60.0, "hut": 80.0, "viewpoint": 60.0, "inn": 80.0, "station": 400.0}
MIN_ELE = {"peak": 700.0, "hut": 0.0, "viewpoint": 750.0, "inn": 900.0}


@dataclass
class CatalogParams:
    max_leg_km: float = 13.0
    min_target_km: float = 1.0       # straight-line distance station -> destination
    max_target_km: float = 11.0
    max_total_km: float = 28.0
    din_hours: tuple = (1.5, 8.5)    # wide on purpose: runs and bad-weather options come from here too
    max_ascent_m: float = 1700.0
    max_road_share: float = 0.45
    max_station_ele_m: float = 1000.0
    per_target: int = 4
    per_station: int = 30
    total: int = 1500
    loop_penalty: float = 4.0
    t3_penalty: float = 1.25


def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "x"


def _project(lat, lon, lat0=47.6):
    return np.stack([np.asarray(lon) * math.cos(math.radians(lat0)) * 111320.0, np.asarray(lat) * 110540.0], axis=-1)


def anchors_for(osm: OsmData) -> list[tuple[float, float, float]]:
    """Points that must become graph vertices: stations and destinations."""
    pts = [(s["lat"], s["lon"], SNAP_M["station"]) for s in osm.stations]
    pts += [(t["lat"], t["lon"], SNAP_M[t["kind"]]) for t in osm.targets]
    return pts


def prepare_stations(osm: OsmData, g: G.Graph, dem: DemSampler, params: CatalogParams) -> list[dict]:
    out = []
    seen = []
    if not osm.stations:
        return out
    eles = dem.sample([s["lat"] for s in osm.stations], [s["lon"] for s in osm.stations])
    for st, ele in zip(osm.stations, eles):
        if np.isfinite(ele) and ele > params.max_station_ele_m:
            continue  # mountain railway top stations
        snapped = G.snap(g, st["lat"], st["lon"], SNAP_M["station"])
        if snapped is None:
            continue
        dup = any(
            s["name"] == st["name"] and haversine_m(s["lat"], s["lon"], st["lat"], st["lon"]) < 1500
            for s in seen
        )
        if dup:
            continue
        rec = dict(st, vertex=snapped[0], snap_m=round(snapped[1]), ele=None if not np.isfinite(ele) else round(float(ele)))
        seen.append(rec)
        out.append(rec)
    log.info("%d usable stations", len(out))
    return out


def prepare_targets(osm: OsmData, g: G.Graph, dem: DemSampler) -> list[dict]:
    if not osm.targets:
        return []
    eles = dem.sample([t["lat"] for t in osm.targets], [t["lon"] for t in osm.targets])
    cands = []
    for t, dem_ele in zip(osm.targets, eles):
        ele = t["ele"] if t["ele"] else (float(dem_ele) if np.isfinite(dem_ele) else None)
        if ele is None:
            continue
        check_ele = float(dem_ele) if np.isfinite(dem_ele) else ele
        if check_ele < MIN_ELE[t["kind"]]:
            continue
        snapped = G.snap(g, t["lat"], t["lon"], SNAP_M[t["kind"]])
        if snapped is None:
            continue
        cands.append(dict(t, ele=round(ele), vertex=snapped[0], snap_m=round(snapped[1])))
    # One destination per 300 m: peaks over huts over viewpoints over inns, then height.
    cands.sort(key=lambda t: (KIND_RANK[t["kind"]], -t["ele"]))
    kept = []
    tree_pts = []
    for t in cands:
        p = _project(t["lat"], t["lon"])
        if tree_pts:
            d = np.min(np.linalg.norm(np.asarray(tree_pts) - p, axis=1))
            if d < 300:
                continue
        kept.append(t)
        tree_pts.append(p)
    log.info("%d usable destinations", len(kept))
    return kept


def _reverse(edges, fwd):
    return edges[::-1], ~fwd[::-1]


def _combine(a: dict, b: dict) -> dict:
    out = {}
    for key in ("length_m", "up_m", "down_m", "marked_m", "unpaved_m", "untagged_path_m"):
        out[key] = a[key] + b[key]
    out["max_ele"] = max(a["max_ele"], b["max_ele"])
    out["min_ele"] = min(a["min_ele"], b["min_ele"])
    out["class_m"] = a["class_m"] + b["class_m"]
    out["sac_m"] = a["sac_m"] + b["sac_m"]
    return out


def _flip(stats: dict) -> dict:
    out = dict(stats)
    out["up_m"], out["down_m"] = stats["down_m"], stats["up_m"]
    return out


def score(stats: dict, kind: str, route_type: str) -> float:
    length_km = stats["length_m"] / 1000
    din_h = din33466_minutes(length_km, stats["up_m"], stats["down_m"]) / 60
    road = stats["class_m"][3:].sum() / max(stats["length_m"], 1)
    s_time = math.exp(-(((din_h - 5.0) / 2.2) ** 2))
    s_height = min(max((stats["max_ele"] - 700) / 1200, 0), 1)
    s_marked = stats["marked_m"] / max(stats["length_m"], 1)
    s_road = 1 - min(road / 0.4, 1)
    s_unpaved = stats["unpaved_m"] / max(stats["length_m"], 1)
    s_type = {"traverse": 1.0, "loop": 0.85, "out-and-back": 0.55}[route_type]
    s_kind = {"peak": 1.0, "hut": 0.85, "viewpoint": 0.75, "inn": 0.7}[kind]
    return round(
        0.22 * s_time + 0.22 * s_height + 0.14 * s_marked + 0.14 * s_road
        + 0.08 * s_unpaved + 0.10 * s_type + 0.10 * s_kind,
        4,
    )


def _passes(stats: dict, params: CatalogParams) -> bool:
    length_km = stats["length_m"] / 1000
    if length_km > params.max_total_km or length_km < 3:
        return False
    din_h = din33466_minutes(length_km, stats["up_m"], stats["down_m"]) / 60
    if not params.din_hours[0] <= din_h <= params.din_hours[1]:
        return False
    if stats["up_m"] > params.max_ascent_m:
        return False
    road = stats["class_m"][3:].sum() / max(stats["length_m"], 1)
    return road <= params.max_road_share


def generate(g: G.Graph, stations: list[dict], targets: list[dict], params: CatalogParams) -> list[dict]:
    """All candidate routes with approximate stats, before selection."""
    t0 = time.time()
    limit = params.max_leg_km * 1000 * 1.6
    st_xy = _project([s["lat"] for s in stations], [s["lon"] for s in stations])
    tg_xy = _project([t["lat"] for t in targets], [t["lon"] for t in targets])
    tg_tree = cKDTree(tg_xy) if targets else None

    legs: dict[int, list] = {}  # target index -> [(station index, edges, fwd, stats)]
    for si, st in enumerate(stations):
        near = tg_tree.query_ball_point(st_xy[si], params.max_target_km * 1000) if tg_tree else []
        near = [ti for ti in near if np.linalg.norm(tg_xy[ti] - st_xy[si]) >= params.min_target_km * 1000]
        if not near:
            continue
        dist, pred = G.shortest_tree(g, st["vertex"], limit)
        for ti in near:
            tv = targets[ti]["vertex"]
            if not np.isfinite(dist[tv]):
                continue
            res = G.path_from_tree(g, pred, st["vertex"], tv)
            if res is None or len(res[0]) == 0:
                continue
            edges, fwd = res
            stats = G.path_stats(g, edges, fwd)
            if stats["length_m"] > params.max_leg_km * 1000:
                continue
            legs.setdefault(ti, []).append((si, edges, fwd, stats))
    log.info("legs computed for %d destinations in %.0fs", len(legs), time.time() - t0)

    cands = []
    for ti, entries in legs.items():
        tgt = targets[ti]
        for si1, e1, f1, s1 in entries:
            # Traverses to another station.
            for si2, e2, f2, s2 in entries:
                if si1 == si2:
                    continue
                if stations[si1]["name"] == stations[si2]["name"]:
                    continue
                desc_stats = _flip(s2)
                stats = _combine(s1, desc_stats)
                if not _passes(stats, params):
                    continue
                shared = np.intersect1d(e1, e2)
                overlap = g.elen[shared].sum() / max(min(s1["length_m"], s2["length_m"]), 1)
                rtype = "out-and-back" if overlap > 0.7 else "traverse"
                de, df = _reverse(e2, f2)
                cands.append({
                    "start": si1, "end": si2, "target": ti, "type": rtype,
                    "edges": np.concatenate([e1, de]), "fwd": np.concatenate([f1, df]),
                    "stats": stats, "score": score(stats, tgt["kind"], rtype),
                })
            # Loop back to the same station, avoiding the way up where possible.
            w = G.penalized_weights(g, e1, params.loop_penalty)
            dist, pred = G.shortest_tree(g, tgt["vertex"], limit, weights=w)
            sv = stations[si1]["vertex"]
            if not np.isfinite(dist[sv]):
                continue
            res = G.path_from_tree(g, pred, tgt["vertex"], sv)
            if res is None or len(res[0]) == 0:
                continue
            de, df = res
            desc = G.path_stats(g, de, df)
            stats = _combine(s1, desc)
            if not _passes(stats, params):
                continue
            shared = np.intersect1d(e1, de)
            overlap = g.elen[shared].sum() / max(min(s1["length_m"], desc["length_m"]), 1)
            rtype = "out-and-back" if overlap > 0.7 else "loop"
            cands.append({
                "start": si1, "end": si1, "target": ti, "type": rtype,
                "edges": np.concatenate([e1, de]), "fwd": np.concatenate([f1, df]),
                "stats": stats, "score": score(stats, tgt["kind"], rtype),
            })
    log.info("%d candidate routes in %.0fs", len(cands), time.time() - t0)
    return cands


def select(cands: list[dict], stations: list[dict], params: CatalogParams) -> list[dict]:
    """Best-scoring routes with variety: limits per destination and per start station,
    and only one direction of each traverse."""
    cands = sorted(cands, key=lambda c: -c["score"])
    per_target: dict[int, int] = {}
    per_station: dict[int, int] = {}
    seen_pairs = set()
    out = []
    for c in cands:
        key = (min(c["start"], c["end"]), max(c["start"], c["end"]), c["target"])
        if key in seen_pairs:
            continue
        if per_target.get(c["target"], 0) >= params.per_target:
            continue
        if per_station.get(c["start"], 0) >= params.per_station:
            continue
        seen_pairs.add(key)
        per_target[c["target"]] = per_target.get(c["target"], 0) + 1
        per_station[c["start"]] = per_station.get(c["start"], 0) + 1
        out.append(c)
        if len(out) >= params.total:
            break
    return out


def _thin(lats, lons, min_step_m=8.0):
    """Drop points closer than min_step_m to the previous kept point."""
    if len(lats) < 3:
        return lats, lons
    keep = [0]
    last = 0
    d = haversine_m(lats[:-1], lons[:-1], lats[1:], lons[1:])
    acc = 0.0
    for i in range(1, len(lats) - 1):
        acc += d[i - 1]
        if acc >= min_step_m:
            keep.append(i)
            acc = 0.0
    keep.append(len(lats) - 1)
    keep = np.asarray(keep)
    return lats[keep], lons[keep]


def finalize(g: G.Graph, c: dict, stations: list[dict], targets: list[dict], dem: DemSampler,
             pace_min_per_effort_km: float = 7.0) -> dict:
    """Exact geometry, elevation profile and times for one selected route."""
    s1 = stations[c["start"]]
    s2 = stations[c["end"]]
    tg = targets[c["target"]]
    lats, lons = G.path_geometry(g, c["edges"], c["fwd"])
    rl, ro = resample(lats, lons, 20.0)
    ele = dem.sample(rl, ro)
    ele = np.where(np.isnan(ele), np.nanmedian(ele), ele)
    up, down = ascent_descent(smooth(ele, 3), threshold_m=3.0)
    st = c["stats"]
    length_km = st["length_m"] / 1000
    din = din33466_minutes(length_km, up, down)
    seg = haversine_m(rl[:-1], ro[:-1], rl[1:], ro[1:])
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    prof_idx = np.linspace(0, len(rl) - 1, min(80, len(rl))).round().astype(int)
    tl, to = _thin(lats, lons)
    ele_label = f" ({tg['ele']} m)" if tg.get("ele") else ""
    if c["type"] == "traverse":
        name = f"{s1['name']} → {tg['name']}{ele_label} → {s2['name']}"
    elif c["type"] == "loop":
        name = f"{tg['name']}{ele_label}, loop from {s1['name']}"
    else:
        name = f"{tg['name']}{ele_label} from {s1['name']}, there and back"
    rid = "-".join([slug(s1["name"]), slug(tg["name"]), slug(s2["name"])])
    rid = rid[:80] + "-" + hashlib.sha1(f"{s1['id']}:{tg['id']}:{s2['id']}:{c['type']}".encode()).hexdigest()[:6]
    sac_m = st["sac_m"]
    class_m = st["class_m"]
    max_sac = int(max([lvl for lvl in range(1, 7) if sac_m[lvl] > 0], default=0))
    summit_dist = None
    tv_lat, tv_lon = g.vlat[tg["vertex"]], g.vlon[tg["vertex"]]
    dists_to_summit = haversine_m(rl, ro, tv_lat, tv_lon)
    if len(dists_to_summit):
        summit_dist = round(float(cum[int(np.argmin(dists_to_summit))]) / 1000, 2)
    return {
        "id": rid,
        "name": name,
        "type": c["type"],
        "start": {"name": s1["name"], "lat": s1["lat"], "lon": s1["lon"], "osm_node": s1["id"]},
        "end": {"name": s2["name"], "lat": s2["lat"], "lon": s2["lon"], "osm_node": s2["id"]},
        "target": {"name": tg["name"], "kind": tg["kind"], "ele": tg.get("ele"),
                   "lat": tg["lat"], "lon": tg["lon"], "osm_node": tg["id"], "km_from_start": summit_dist},
        "distance_km": round(length_km, 1),
        "ascent_m": int(round(up)),
        "descent_m": int(round(down)),
        "max_ele_m": int(round(float(np.max(ele)))),
        "min_ele_m": int(round(float(np.min(ele)))),
        "din_min": int(round(din)),
        "run_min": int(round(trail_run_minutes(length_km, up, pace_min_per_effort_km))),
        "max_sac": max_sac,
        "sac_km": {f"T{lvl}": round(float(sac_m[lvl]) / 1000, 2) for lvl in range(1, 7) if sac_m[lvl] > 0},
        "t3_km": round(float(sac_m[3]) / 1000, 2),
        "untagged_path_km": round(st["untagged_path_m"] / 1000, 2),
        "road_km": round(float(class_m[3:].sum()) / 1000, 2),
        "busy_road_km": round(float(class_m[5]) / 1000, 2),
        "marked_share": round(st["marked_m"] / max(st["length_m"], 1), 2),
        "unpaved_share": round(st["unpaved_m"] / max(st["length_m"], 1), 2),
        "score": c["score"],
        "profile": [[round(float(cum[i]) / 1000, 2), int(round(float(ele[i])))] for i in prof_idx],
        "polyline": encode_polyline(tl, to),
    }


def write_catalog(routes: list[dict], stations: list[dict], targets: list[dict], out_dir: str | Path,
                  meta: dict) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with gzip.open(out / "routes.json.gz", "wt", encoding="utf-8") as fh:
        json.dump(routes, fh, ensure_ascii=False, separators=(",", ":"))
    slim_st = [{k: s[k] for k in ("id", "name", "lat", "lon", "ele")} for s in stations]
    (out / "stations.json").write_text(json.dumps(slim_st, ensure_ascii=False, indent=1), encoding="utf-8")
    slim_tg = [{k: t[k] for k in ("id", "name", "kind", "ele", "lat", "lon")} for t in targets]
    (out / "destinations.json").write_text(json.dumps(slim_tg, ensure_ascii=False, indent=1), encoding="utf-8")
    meta = dict(meta, routes=len(routes), stations=len(stations), destinations=len(targets))
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
