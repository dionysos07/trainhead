import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trainhead.config import load_settings  # noqa: E402
from trainhead.dem import FunctionDem  # noqa: E402

PEAK = (47.630, 11.830)


def bump(lats, lons):
    """A single 1500 m mountain over an 800 m valley."""
    dlat = lats - PEAK[0]
    dlon = (lons - PEAK[1]) * math.cos(math.radians(PEAK[0]))
    return 800 + 700 * np.exp(-(dlat ** 2 + dlon ** 2) / (2 * 0.009 ** 2))


def _line(start, end, n):
    return [(start[0] + (end[0] - start[0]) * i / n, start[1] + (end[1] - start[1]) * i / n) for i in range(n + 1)]


def build_osm(path: Path) -> Path:
    """A valley with two stations, one peak, a hut, and trails of different difficulty.

    Way 1 (marked, T2): Talbahnhof -> Testkopf
    Way 2 (T2):         Testkopf -> Seebahnhof, passing the hut
    Way 3 (road):       Talbahnhof -> Seebahnhof along the valley
    Way 4 (T4):         a shorter, steeper line to the summit (must not be used)
    Way 5 (untagged):   a path high on the mountain without a difficulty tag (must not be used)
    """
    nodes = {}
    next_id = [1000]

    def node(lat, lon, tags=None, nid=None):
        if nid is None:
            next_id[0] += 1
            nid = next_id[0]
        nodes[nid] = (lat, lon, tags or {})
        return nid

    ways = []

    def way(points_ids, tags, wid):
        ways.append((wid, points_ids, tags))

    node(47.6000, 11.8000, {"railway": "station", "name": "Talbahnhof"}, nid=1)
    node(47.6000, 11.8600, {"railway": "station", "name": "Seebahnhof"}, nid=2)
    node(47.6305, 11.8302, {"natural": "peak", "name": "Testkopf", "ele": "1500"}, nid=3)
    node(47.6095, 11.8505, {"tourism": "alpine_hut", "name": "Testhütte"}, nid=4)
    node(47.6000, 11.8300, {"railway": "station", "name": "Bergbahn Gipfel", "station": "funicular"}, nid=5)

    a = node(47.6003, 11.8003)
    b = node(47.6003, 11.8597)
    p = node(47.6305, 11.8300)
    mid1 = node(47.6150, 11.8150)
    hut_n = node(47.6100, 11.8500)

    w1 = [a] + [node(la, lo) for la, lo in _line((47.6003, 11.8003), (47.6150, 11.8150), 12)[1:-1]] + [mid1]
    w1 += [node(la, lo) for la, lo in _line((47.6150, 11.8150), (47.6305, 11.8300), 12)[1:-1]] + [p]
    way(w1, {"highway": "path", "sac_scale": "mountain_hiking"}, 101)

    w2 = [p] + [node(la, lo) for la, lo in _line((47.6305, 11.8300), (47.6100, 11.8500), 14)[1:-1]] + [hut_n]
    w2 += [node(la, lo) for la, lo in _line((47.6100, 11.8500), (47.6003, 11.8597), 8)[1:-1]] + [b]
    way(w2, {"highway": "path", "sac_scale": "mountain_hiking"}, 102)

    w3 = [a] + [node(la, lo) for la, lo in _line((47.6003, 11.8003), (47.6003, 11.8597), 30)[1:-1]] + [b]
    way(w3, {"highway": "residential", "surface": "asphalt"}, 103)

    w4 = [mid1] + [node(la, lo) for la, lo in _line((47.6150, 11.8150), (47.6305, 11.8300), 3)[1:-1]] + [p]
    way(w4, {"highway": "path", "sac_scale": "alpine_hiking"}, 104)

    hi = node(47.6250, 11.8250)
    w5 = [hi] + [node(la, lo) for la, lo in _line((47.6250, 11.8250), (47.6305, 11.8300), 4)[1:-1]] + [p]
    w5_link = [mid1, hi]
    way(w5, {"highway": "path"}, 105)
    way(w5_link, {"highway": "path"}, 106)

    lines = ["<?xml version='1.0' encoding='UTF-8'?>", '<osm version="0.6" generator="trainhead-test">']
    for nid, (lat, lon, tags) in sorted(nodes.items()):
        if tags:
            lines.append(f'  <node id="{nid}" version="1" lat="{lat:.7f}" lon="{lon:.7f}">')
            for k, v in tags.items():
                lines.append(f'    <tag k="{k}" v="{v}"/>')
            lines.append("  </node>")
        else:
            lines.append(f'  <node id="{nid}" version="1" lat="{lat:.7f}" lon="{lon:.7f}"/>')
    for wid, ids, tags in ways:
        lines.append(f'  <way id="{wid}" version="1">')
        for i in ids:
            lines.append(f'    <nd ref="{i}"/>')
        for k, v in tags.items():
            lines.append(f'    <tag k="{k}" v="{v}"/>')
        lines.append("  </way>")
    lines.append('  <relation id="900" version="1">')
    lines.append('    <member type="way" ref="101" role=""/>')
    lines.append('    <tag k="type" v="route"/><tag k="route" v="hiking"/><tag k="name" v="Testweg"/>')
    lines.append("  </relation>")
    lines.append("</osm>")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


@pytest.fixture
def mini_osm(tmp_path):
    return build_osm(tmp_path / "mini.osm")


@pytest.fixture
def dem():
    return FunctionDem(bump)


@pytest.fixture
def settings():
    s = load_settings()
    s["region"]["bbox"] = [11.78, 47.58, 11.88, 47.65]
    return s
