"""Walking graph built from OSM ways, with per-edge length, climb and difficulty."""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

from . import difficulty as dif
from .dem import DemSampler
from .geo import ascent_descent, haversine_m, resample, smooth
from .osm_extract import OsmData

log = logging.getLogger(__name__)

NO_PRED = -9999  # scipy's marker for "no predecessor"


@dataclass
class Graph:
    vlat: np.ndarray
    vlon: np.ndarray
    eu: np.ndarray
    ev: np.ndarray
    elen: np.ndarray
    ecls: np.ndarray
    esac: np.ndarray
    emarked: np.ndarray
    eunpaved: np.ndarray
    eup: np.ndarray       # climb going u -> v
    edown: np.ndarray     # descent going u -> v
    emaxele: np.ndarray
    eminele: np.ndarray
    egeo_start: np.ndarray
    egeo_end: np.ndarray
    glat: np.ndarray
    glon: np.ndarray
    eallowed: np.ndarray
    weights: csr_matrix | None = None
    entry_pos: np.ndarray | None = None   # (n_edges, 2) positions of each kept edge in weights.data
    lookup: dict | None = None            # (a, b) -> edge index, for kept edges
    _tree: cKDTree | None = None
    _tree_ids: np.ndarray | None = None

    @property
    def n_vertices(self) -> int:
        return len(self.vlat)

    @property
    def n_edges(self) -> int:
        return len(self.eu)


def _vertex_ids(osm: OsmData, anchors=None) -> np.ndarray:
    all_nodes = np.concatenate(osm.way_nodes)
    uniq, counts = np.unique(all_nodes, return_counts=True)
    shared = uniq[counts > 1]
    ends = np.concatenate([[w[0], w[-1]] for w in osm.way_nodes])
    # Closed ways get an extra split point so no edge starts and ends at the same vertex.
    mids = [w[len(w) // 2] for w in osm.way_nodes if w[0] == w[-1] and len(w) > 3]
    extra = np.asarray(mids, dtype=np.int64)
    vids = np.union1d(np.union1d(shared, ends), extra)
    if anchors:
        vids = np.union1d(vids, _anchor_nodes(osm, all_nodes, anchors))
    return vids


def _anchor_nodes(osm: OsmData, all_nodes: np.ndarray, anchors) -> np.ndarray:
    """Way nodes closest to stations and destinations become vertices, so a
    summit in the middle of a path can be reached and left in both directions."""
    lats = np.concatenate(osm.way_lats)
    lons = np.concatenate(osm.way_lons)
    tree = cKDTree(_project(lats, lons))
    picked = []
    for lat, lon, radius in anchors:
        dist, i = tree.query(_project(lat, lon), distance_upper_bound=radius)
        if np.isfinite(dist):
            picked.append(all_nodes[i])
    return np.asarray(picked, dtype=np.int64)


def build_graph(osm: OsmData, dem: DemSampler, max_sac: int, untagged_max_ele_m: float,
                t3_penalty: float = 1.25, sample_step_m: float = 20.0, anchors=None) -> Graph:
    """anchors: [(lat, lon, radius_m)] for stations and destinations."""
    if osm.n_ways == 0:
        raise ValueError("no walkable ways in the input")
    vids = _vertex_ids(osm, anchors)
    vlat = np.zeros(len(vids))
    vlon = np.zeros(len(vids))

    eu, ev, ecls, esac, emarked, eunpaved = [], [], [], [], [], []
    geo_lat, geo_lon, gstart, gend = [], [], [], []
    offset = 0
    for i in range(osm.n_ways):
        nodes = osm.way_nodes[i]
        lats = osm.way_lats[i]
        lons = osm.way_lons[i]
        idx = np.searchsorted(vids, nodes)
        idx_c = np.minimum(idx, len(vids) - 1)
        is_v = vids[idx_c] == nodes
        pos = np.nonzero(is_v)[0]
        vlat[idx_c[pos]] = lats[pos]
        vlon[idx_c[pos]] = lons[pos]
        for p0, p1 in zip(pos[:-1], pos[1:]):
            u = int(idx_c[p0])
            v = int(idx_c[p1])
            if u == v:
                continue
            seg_lat = lats[p0:p1 + 1]
            seg_lon = lons[p0:p1 + 1]
            eu.append(u)
            ev.append(v)
            ecls.append(osm.way_class[i])
            esac.append(osm.way_sac[i])
            emarked.append(osm.way_marked[i])
            eunpaved.append(osm.way_unpaved[i])
            geo_lat.append(seg_lat)
            geo_lon.append(seg_lon)
            gstart.append(offset)
            offset += len(seg_lat)
            gend.append(offset)

    glat = np.concatenate(geo_lat)
    glon = np.concatenate(geo_lon)
    gstart = np.asarray(gstart, dtype=np.int64)
    gend = np.asarray(gend, dtype=np.int64)
    n_e = len(eu)
    log.info("graph: %d vertices, %d edges", len(vids), n_e)

    # Per-edge length and elevation along a resampled line.
    elen = np.zeros(n_e)
    eup = np.zeros(n_e, dtype=np.float32)
    edown = np.zeros(n_e, dtype=np.float32)
    emax = np.zeros(n_e, dtype=np.float32)
    emin = np.zeros(n_e, dtype=np.float32)
    samples_lat, samples_lon, sample_owner = [], [], []
    for k in range(n_e):
        s, e = gstart[k], gend[k]
        rl, ro = resample(glat[s:e], glon[s:e], sample_step_m)
        samples_lat.append(rl)
        samples_lon.append(ro)
        sample_owner.append(np.full(len(rl), k, dtype=np.int64))
        elen[k] = float(haversine_m(glat[s:e - 1], glon[s:e - 1], glat[s + 1:e], glon[s + 1:e]).sum())
    s_lat = np.concatenate(samples_lat)
    s_lon = np.concatenate(samples_lon)
    owner = np.concatenate(sample_owner)
    ele = dem.sample(s_lat, s_lon)
    if np.isnan(ele).all():
        raise RuntimeError("elevation model returned no data for the region")
    ele = np.where(np.isnan(ele), np.nanmedian(ele), ele)
    bounds = np.concatenate([[0], np.cumsum(np.bincount(owner, minlength=n_e))])
    for k in range(n_e):
        prof = ele[bounds[k]:bounds[k + 1]]
        prof_s = smooth(prof, 3) if len(prof) >= 3 else prof
        # Near-raw sums here: a hysteresis threshold per short edge would drop
        # small climbs systematically. Final routes are recomputed with
        # hysteresis over the whole profile.
        up, down = ascent_descent(prof_s, threshold_m=0.5)
        eup[k] = up
        edown[k] = down
        emax[k] = prof.max()
        emin[k] = prof.min()

    ecls_a = np.asarray(ecls, dtype=np.int8)
    esac_a = np.asarray(esac, dtype=np.int8)
    allowed = np.array([
        dif.edge_allowed(int(c), int(sc), float(mx), max_sac, untagged_max_ele_m)
        for c, sc, mx in zip(ecls_a, esac_a, emax)
    ], dtype=bool)

    g = Graph(
        vlat=vlat, vlon=vlon,
        eu=np.asarray(eu, dtype=np.int64), ev=np.asarray(ev, dtype=np.int64),
        elen=elen, ecls=ecls_a, esac=esac_a,
        emarked=np.asarray(emarked, dtype=bool), eunpaved=np.asarray(eunpaved, dtype=bool),
        eup=eup, edown=edown, emaxele=emax, eminele=emin,
        egeo_start=gstart, egeo_end=gend, glat=glat, glon=glon,
        eallowed=allowed,
    )
    _build_weights(g, t3_penalty)
    return g


def edge_cost(g: Graph, k: int, t3_penalty: float = 1.25) -> float:
    cls = dif.CODE_CLASSES[int(g.ecls[k])]
    factor = dif.COST_FACTOR[(cls, bool(g.emarked[k]))]
    if g.esac[k] >= 3:
        factor *= t3_penalty
    return max(g.elen[k], 0.1) * factor


def _build_weights(g: Graph, t3_penalty: float) -> None:
    n = g.n_vertices
    cand = np.nonzero(g.eallowed)[0]
    costs = np.array([edge_cost(g, int(k), t3_penalty) for k in cand])
    a = np.minimum(g.eu[cand], g.ev[cand])
    b = np.maximum(g.eu[cand], g.ev[cand])
    # Keep the cheapest of parallel edges between the same two vertices.
    order = np.lexsort((costs, b, a))
    a_s, b_s = a[order], b[order]
    first = np.ones(len(order), dtype=bool)
    first[1:] = (a_s[1:] != a_s[:-1]) | (b_s[1:] != b_s[:-1])
    kept = cand[order][first]
    kept_cost = costs[order][first]
    ka = np.minimum(g.eu[kept], g.ev[kept])
    kb = np.maximum(g.eu[kept], g.ev[kept])
    m = len(kept)

    rows = np.concatenate([ka, kb])
    cols = np.concatenate([kb, ka])
    data = np.concatenate([kept_cost, kept_cost])
    srt = np.lexsort((cols, rows))
    rows_s, cols_s, data_s = rows[srt], cols[srt], data[srt]
    indptr = np.concatenate([[0], np.cumsum(np.bincount(rows_s, minlength=n))])
    g.weights = csr_matrix((data_s, cols_s, indptr), shape=(n, n))
    inv = np.empty_like(srt)
    inv[srt] = np.arange(len(srt))
    entry_pos = np.full((g.n_edges, 2), -1, dtype=np.int64)
    entry_pos[kept, 0] = inv[:m]
    entry_pos[kept, 1] = inv[m:]
    g.entry_pos = entry_pos
    lookup = {}
    for k, x, y in zip(kept.tolist(), ka.tolist(), kb.tolist()):
        lookup[(x, y)] = k
        lookup[(y, x)] = k
    g.lookup = lookup
    connected = np.zeros(n, dtype=bool)
    connected[ka] = True
    connected[kb] = True
    ids = np.nonzero(connected)[0]
    g._tree_ids = ids
    g._tree = cKDTree(_project(g.vlat[ids], g.vlon[ids]))
    log.info("routable: %d of %d edges", m, g.n_edges)


def _project(lat, lon, lat0: float = 47.6):
    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    x = lon * np.cos(np.radians(lat0)) * 111320.0
    y = lat * 110540.0
    return np.stack([x, y], axis=-1)


def snap(g: Graph, lat: float, lon: float, max_dist_m: float) -> tuple[int, float] | None:
    dist, i = g._tree.query(_project(lat, lon), distance_upper_bound=max_dist_m)
    if not np.isfinite(dist):
        return None
    return int(g._tree_ids[i]), float(dist)


def shortest_tree(g: Graph, source: int, limit: float, weights: csr_matrix | None = None):
    w = g.weights if weights is None else weights
    dist, pred = dijkstra(w, directed=True, indices=source, return_predecessors=True, limit=limit)
    return dist.astype(np.float32), pred.astype(np.int32)


def path_from_tree(g: Graph, pred: np.ndarray, source: int, target: int):
    """Edges and directions from source to target, or None if unreachable."""
    if source == target:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=bool)
    verts = [target]
    guard = 0
    while verts[-1] != source:
        p = int(pred[verts[-1]])
        if p < 0:
            return None
        verts.append(p)
        guard += 1
        if guard > 1_000_000:
            raise RuntimeError("predecessor loop")
    verts.reverse()
    edges = np.empty(len(verts) - 1, dtype=np.int64)
    fwd = np.empty(len(verts) - 1, dtype=bool)
    for i, (x, y) in enumerate(zip(verts[:-1], verts[1:])):
        k = g.lookup[(x, y)]
        edges[i] = k
        fwd[i] = g.eu[k] == x
    return edges, fwd


def penalized_weights(g: Graph, edges: np.ndarray, factor: float = 4.0) -> csr_matrix:
    data = g.weights.data.copy()
    pos = g.entry_pos[edges].ravel()
    pos = pos[pos >= 0]
    data[pos] *= factor
    return csr_matrix((data, g.weights.indices, g.weights.indptr), shape=g.weights.shape)


def path_geometry(g: Graph, edges: np.ndarray, fwd: np.ndarray):
    lats, lons = [], []
    for k, f in zip(edges, fwd):
        s, e = g.egeo_start[k], g.egeo_end[k]
        la, lo = g.glat[s:e], g.glon[s:e]
        if not f:
            la, lo = la[::-1], lo[::-1]
        if lats:
            la, lo = la[1:], lo[1:]
        lats.append(la)
        lons.append(lo)
    if not lats:
        return np.zeros(0), np.zeros(0)
    return np.concatenate(lats), np.concatenate(lons)


def path_stats(g: Graph, edges: np.ndarray, fwd: np.ndarray) -> dict:
    if len(edges) == 0:
        return {
            "length_m": 0.0, "up_m": 0.0, "down_m": 0.0, "max_ele": float("nan"), "min_ele": float("nan"),
            "class_m": np.zeros(6), "sac_m": np.zeros(7), "marked_m": 0.0, "unpaved_m": 0.0,
            "untagged_path_m": 0.0,
        }
    lens = g.elen[edges]
    up = np.where(fwd, g.eup[edges], g.edown[edges]).sum()
    down = np.where(fwd, g.edown[edges], g.eup[edges]).sum()
    cls = g.ecls[edges].astype(np.int64)
    sac = g.esac[edges].astype(np.int64)
    untagged_path = (sac == 0) & (cls <= 1)
    return {
        "length_m": float(lens.sum()),
        "up_m": float(up),
        "down_m": float(down),
        "max_ele": float(g.emaxele[edges].max()),
        "min_ele": float(g.eminele[edges].min()),
        "class_m": np.bincount(cls, weights=lens, minlength=6),
        "sac_m": np.bincount(sac, weights=lens, minlength=7),
        "marked_m": float(lens[g.emarked[edges]].sum()),
        "unpaved_m": float(lens[g.eunpaved[edges]].sum()),
        "untagged_path_m": float(lens[untagged_path].sum()),
    }
