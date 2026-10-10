"""Read walkable ways, stations and destinations from an OSM file.

Works on .osm.pbf extracts (Geofabrik) and on small .osm XML files (tests).
Two passes: hiking route relations first (to know which ways are marked
trails), then nodes and ways with locations.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import numpy as np
import osmium

from . import difficulty as dif

log = logging.getLogger(__name__)

EXCLUDED_STATION_TYPES = {"subway", "light_rail", "monorail", "funicular", "miniature", "tram"}
MOUNTAIN_FOOD = {"restaurant", "cafe", "biergarten"}
# Stops outside Germany: tickets and timetables differ, and the map data there is partial.
FOREIGN_NETWORKS = {"VVT", "SVV", "Salzburger Verkehrsverbund", "Verkehrsverbund Tirol", "OÖVV", "VVV"}
FOREIGN_OPERATOR = re.compile(r"ÖBB|Postbus|Salzburg AG|Salzburger Lokalbahn|Innsbrucker Verkehrsbetriebe")
# On-demand services have to be booked by phone, so their stops are not trailheads.
ON_DEMAND_NAME = re.compile(r"^(AST|Rufbus|Ruftaxi)\b|Anrufsammeltaxi", re.IGNORECASE)
# Cog railways and cable cars aren't trailheads the planner can rely on.
MOUNTAIN_RAILWAY = re.compile(
    r"talstation|bergstation|mittelstation|seilbahn|zugspitzbahn|wendelsteinbahn|bergbahn|zahnrad|chiemsee-bahn",
    re.IGNORECASE,
)


@dataclass
class OsmData:
    way_ids: list = field(default_factory=list)
    way_nodes: list = field(default_factory=list)   # np.int64 arrays of node ids
    way_lats: list = field(default_factory=list)    # np.float64 arrays
    way_lons: list = field(default_factory=list)
    way_class: list = field(default_factory=list)   # difficulty.CLASS_CODES
    way_sac: list = field(default_factory=list)     # 0 = untagged
    way_marked: list = field(default_factory=list)
    way_unpaved: list = field(default_factory=list)
    stations: list = field(default_factory=list)    # dicts: id, name, lat, lon
    bus_stops: list = field(default_factory=list)   # dicts: id, name, lat, lon
    targets: list = field(default_factory=list)     # dicts: id, name, lat, lon, kind, ele

    @property
    def n_ways(self) -> int:
        return len(self.way_ids)


def parse_ele(value: str | None) -> float | None:
    if not value:
        return None
    m = re.match(r"^\s*(\d+)(?:([.,])(\d+))?", value)
    if not m:
        return None
    whole, sep, frac = m.groups()
    if sep and frac and len(frac) == 3 and int(whole) < 10:
        # "1.838" written with a thousands separator
        return float(whole + frac)
    try:
        return float(whole + ("." + frac if frac else ""))
    except ValueError:
        return None


def _in_bbox(lat, lon, bbox) -> bool:
    west, south, east, north = bbox
    return south <= lat <= north and west <= lon <= east


def is_foreign(tags) -> bool:
    ifopt = tags.get("ref:IFOPT", "")
    if ifopt and not ifopt.lower().startswith("de:"):
        return True
    if tags.get("network") in FOREIGN_NETWORKS:
        return True
    return bool(FOREIGN_OPERATOR.search(tags.get("operator", "")))


def _station_from_node(node, bbox):
    tags = node.tags
    if tags.get("railway") not in ("station", "halt"):
        return None
    if tags.get("station") in EXCLUDED_STATION_TYPES:
        return None
    if tags.get("disused") == "yes" or tags.get("abandoned") == "yes":
        return None
    name = tags.get("name")
    if not name:
        return None
    if MOUNTAIN_RAILWAY.search(name) or MOUNTAIN_RAILWAY.search(tags.get("operator", "")):
        return None
    if tags.get("railway:preserved") == "yes" or tags.get("usage") == "tourism":
        return None
    if is_foreign(tags):
        return None
    lat, lon = node.location.lat, node.location.lon
    if not _in_bbox(lat, lon, bbox):
        return None
    return {"id": node.id, "name": name, "lat": lat, "lon": lon}


def _bus_stop_from_node(node, bbox):
    tags = node.tags
    is_stop = tags.get("highway") == "bus_stop" or (
        tags.get("public_transport") in ("platform", "stop_position") and tags.get("bus") == "yes"
    )
    if not is_stop:
        return None
    name = tags.get("name")
    if not name or tags.get("disused") == "yes":
        return None
    if ON_DEMAND_NAME.search(name) or is_foreign(tags):
        return None
    lat, lon = node.location.lat, node.location.lon
    if not _in_bbox(lat, lon, bbox):
        return None
    return {"id": node.id, "name": name, "lat": lat, "lon": lon}


def _target_kind(tags) -> str | None:
    if tags.get("natural") == "peak":
        return "peak"
    if tags.get("tourism") == "alpine_hut":
        return "hut"
    if tags.get("tourism") == "viewpoint":
        return "viewpoint"
    if tags.get("amenity") in MOUNTAIN_FOOD:
        return "inn"  # kept only if high enough, decided once elevation is known
    return None


def _target_from_way(way, bbox):
    """Huts and inns drawn as buildings: their centre becomes the destination."""
    tags = way.tags
    name = tags.get("name")
    kind = _target_kind(tags)
    if not name or kind not in ("hut", "inn"):
        return None
    lats, lons = [], []
    for n in way.nodes:
        if not n.location.valid():
            return None
        lats.append(n.lat)
        lons.append(n.lon)
    if len(lats) > 1 and lats[0] == lats[-1] and lons[0] == lons[-1]:
        lats, lons = lats[:-1], lons[:-1]
    lat, lon = sum(lats) / len(lats), sum(lons) / len(lons)
    if not _in_bbox(lat, lon, bbox):
        return None
    return {"id": -way.id, "name": name, "lat": lat, "lon": lon, "kind": kind, "ele": parse_ele(tags.get("ele"))}


def _target_from_node(node, bbox):
    tags = node.tags
    name = tags.get("name")
    if not name:
        return None
    kind = None
    if tags.get("natural") == "peak":
        kind = "peak"
    elif tags.get("tourism") == "alpine_hut":
        kind = "hut"
    elif tags.get("tourism") == "viewpoint":
        kind = "viewpoint"
    elif tags.get("amenity") in MOUNTAIN_FOOD:
        kind = "inn"  # kept only if high enough, decided once elevation is known
    if kind is None:
        return None
    lat, lon = node.location.lat, node.location.lon
    if not _in_bbox(lat, lon, bbox):
        return None
    return {
        "id": node.id,
        "name": name,
        "lat": lat,
        "lon": lon,
        "kind": kind,
        "ele": parse_ele(tags.get("ele")),
    }


def read_osm(path: str, bbox) -> OsmData:
    """bbox = [west, south, east, north]."""
    marked: set[int] = set()
    for rel in osmium.FileProcessor(path, osmium.osm.RELATION):
        if rel.tags.get("route") in ("hiking", "foot"):
            for m in rel.members:
                if m.type == "w":
                    marked.add(m.ref)
    log.info("%d ways belong to hiking routes", len(marked))

    data = OsmData()
    west, south, east, north = bbox
    pad = 0.02
    processor = (
        osmium.FileProcessor(path, osmium.osm.NODE | osmium.osm.WAY)
        .with_locations()
        .with_filter(osmium.filter.EmptyTagFilter())  # untagged nodes still feed the location index
    )
    for obj in processor:
        if obj.is_node():
            st = _station_from_node(obj, bbox)
            if st:
                data.stations.append(st)
                continue
            bs = _bus_stop_from_node(obj, bbox)
            if bs:
                data.bus_stops.append(bs)
                continue
            tg = _target_from_node(obj, bbox)
            if tg:
                data.targets.append(tg)
            continue

        tags = obj.tags
        if not dif.way_is_walkable(tags):
            tw = _target_from_way(obj, bbox)
            if tw:
                data.targets.append(tw)
            continue
        ids, lats, lons = [], [], []
        ok = True
        for n in obj.nodes:
            if not n.location.valid():
                ok = False
                break
            ids.append(n.ref)
            lats.append(n.lat)
            lons.append(n.lon)
        if not ok or len(ids) < 2:
            continue
        lats_a = np.asarray(lats)
        lons_a = np.asarray(lons)
        inside = (
            (lats_a >= south - pad) & (lats_a <= north + pad) & (lons_a >= west - pad) & (lons_a <= east + pad)
        )
        if not inside.any():
            continue
        cls = dif.way_class(tags)
        sac = dif.parse_sac(tags.get("sac_scale")) or 0
        is_marked = obj.id in marked or tags.get("trailblazed") == "yes"
        data.way_ids.append(obj.id)
        data.way_nodes.append(np.asarray(ids, dtype=np.int64))
        data.way_lats.append(lats_a)
        data.way_lons.append(lons_a)
        data.way_class.append(dif.CLASS_CODES[cls])
        data.way_sac.append(sac)
        data.way_marked.append(bool(is_marked))
        data.way_unpaved.append(dif.is_unpaved(cls, tags.get("surface")))

    log.info(
        "read %d walkable ways, %d stations, %d bus stops, %d destinations",
        data.n_ways,
        len(data.stations),
        len(data.bus_stops),
        len(data.targets),
    )
    return data
