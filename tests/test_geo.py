import math

import numpy as np
import pytest

from trainhead import difficulty as dif
from trainhead.geo import (
    ascent_descent,
    decode_polyline,
    din33466_minutes,
    encode_polyline,
    haversine_m,
    polyline_length_m,
    resample,
    to_gpx,
    trail_run_minutes,
)


def test_haversine_one_degree_latitude():
    assert haversine_m(47.0, 11.0, 48.0, 11.0) == pytest.approx(111_195, rel=1e-3)


def test_din_vertical_dominates():
    # 12 km, 800 m up and down: horizontal 3 h, vertical 2.67 + 1.6 = 4.27 h
    minutes = din33466_minutes(12, 800, 800)
    assert minutes == pytest.approx(60 * (800 / 300 + 800 / 500 + 3 / 2), abs=0.5)


def test_din_horizontal_dominates():
    # 16 km flat with 200 m up: horizontal 4 h, vertical 0.67 h
    assert din33466_minutes(16, 200, 0) == pytest.approx(60 * (4 + (200 / 300) / 2), abs=0.5)


def test_trail_run_effort_distance():
    assert trail_run_minutes(10, 500, 7.0) == pytest.approx(105)


def test_ascent_hysteresis_ignores_noise():
    flat_noisy = [800, 801, 799, 802, 800, 801, 799]
    assert ascent_descent(flat_noisy, threshold_m=5) == (0.0, 0.0)
    climb = list(range(800, 1001, 10)) + list(range(1000, 899, -10))
    up, down = ascent_descent(climb, threshold_m=5)
    assert up == pytest.approx(200)
    assert down == pytest.approx(100)


def test_polyline_roundtrip():
    lats = [47.60001, 47.61234, 47.63, 47.59999]
    lons = [11.80001, 11.81234, 11.83, 11.79999]
    dl, do = decode_polyline(encode_polyline(lats, lons))
    assert np.allclose(dl, lats, atol=1e-5)
    assert np.allclose(do, lons, atol=1e-5)


def test_resample_spacing():
    lats, lons = resample([47.0, 47.0], [11.0, 11.01], 25)
    total = polyline_length_m([47.0, 47.0], [11.0, 11.01])
    steps = np.diff(lons) / 0.01 * total
    assert len(lats) == math.ceil(total / 25) + 1
    assert steps.max() <= 25.01


def test_gpx_has_points_and_attribution():
    gpx = to_gpx("A → B", [47.0, 47.1], [11.0, 11.1], [800.0, 900.0])
    assert gpx.count("<trkpt") == 2
    assert "OpenStreetMap" in gpx
    assert "&" not in gpx.replace("&amp;", "").replace("&lt;", "").replace("&gt;", "").replace("&quot;", "")


@pytest.mark.parametrize("value,expected", [
    ("mountain_hiking", 2), ("T3", 3), ("hiking;alpine_hiking", 4), ("yes", None), (None, None), ("t9", None),
])
def test_parse_sac(value, expected):
    assert dif.parse_sac(value) == expected


def test_edge_rules():
    path = dif.CLASS_CODES["path"]
    track = dif.CLASS_CODES["track"]
    assert dif.edge_allowed(path, 2, 1800, max_sac=3, untagged_max_ele_m=1000)
    assert not dif.edge_allowed(path, 4, 900, max_sac=3, untagged_max_ele_m=1000)
    assert dif.edge_allowed(path, 0, 950, max_sac=3, untagged_max_ele_m=1000)
    assert not dif.edge_allowed(path, 0, 1200, max_sac=3, untagged_max_ele_m=1000)
    assert dif.edge_allowed(track, 0, 1600, max_sac=3, untagged_max_ele_m=1000)


def test_walkable_filters():
    assert dif.way_is_walkable({"highway": "path"})
    assert not dif.way_is_walkable({"highway": "path", "foot": "no"})
    assert not dif.way_is_walkable({"highway": "path", "access": "private"})
    assert dif.way_is_walkable({"highway": "track", "access": "private", "foot": "yes"})
    assert not dif.way_is_walkable({"highway": "path", "trail_visibility": "bad"})
    assert not dif.way_is_walkable({"highway": "cycleway"})
    assert dif.way_is_walkable({"highway": "cycleway", "foot": "designated"})
    assert not dif.way_is_walkable({"highway": "primary"})
    assert not dif.way_is_walkable({"highway": "via_ferrata"})
