import json

import numpy as np
import pytest

from trainhead import catalog as C
from trainhead import graph as G
from trainhead.cli import main as cli_main
from trainhead.geo import decode_polyline
from trainhead.osm_extract import parse_ele, read_osm


def _build(mini_osm, dem, settings):
    bbox = settings["region"]["bbox"]
    osm = read_osm(str(mini_osm), bbox)
    g = G.build_graph(osm, dem, max_sac=3, untagged_max_ele_m=1000, anchors=C.anchors_for(osm))
    params = C.CatalogParams(min_target_km=0.5)
    stations = C.prepare_stations(osm, g, dem, params)
    targets = C.prepare_targets(osm, g, dem)
    return osm, g, params, stations, targets


@pytest.mark.parametrize("value,expected", [("1838", 1838), ("1838 m", 1838), ("1.838", 1838), ("2,5", 2.5), (None, None)])
def test_parse_ele(value, expected):
    assert parse_ele(value) == expected


def test_reads_stations_targets_and_marked_ways(mini_osm, dem, settings):
    osm = read_osm(str(mini_osm), settings["region"]["bbox"])
    names = {s["name"] for s in osm.stations}
    assert names == {"Talbahnhof", "Seebahnhof"}  # the funicular station is skipped
    kinds = {t["name"]: t["kind"] for t in osm.targets}
    assert kinds == {"Testkopf": "peak", "Testhütte": "hut", "Weghütte": "hut"}
    marked = [m for wid, m in zip(osm.way_ids, osm.way_marked) if wid == 101]
    assert marked == [True]


def test_rules_exclude_t4_and_high_untagged(mini_osm, dem, settings):
    _, g, *_ = _build(mini_osm, dem, settings)
    # way 4 is T4, ways 5 and 6 are untagged above 1000 m
    t4 = g.esac == 4
    assert t4.any() and not g.eallowed[t4].any()
    high_untagged = (g.esac == 0) & (g.ecls == 0) & (g.emaxele > 1000)
    assert high_untagged.any() and not g.eallowed[high_untagged].any()


def test_generates_traverse_and_loop(mini_osm, dem, settings):
    _, g, params, stations, targets = _build(mini_osm, dem, settings)
    cands = C.generate(g, stations, targets, params)
    chosen = C.select(cands, stations, params)
    routes = [C.finalize(g, c, stations, targets, dem) for c in chosen]
    peak_routes = [r for r in routes if r["target"]["name"] == "Testkopf"]
    assert peak_routes, "no route over the peak"
    trav = [r for r in peak_routes if r["type"] == "traverse"]
    assert trav, "expected a station-to-station traverse"
    r = trav[0]
    assert {r["start"]["name"], r["end"]["name"]} == {"Talbahnhof", "Seebahnhof"}
    assert r["max_sac"] == 2
    assert r["t3_km"] == 0
    assert 600 <= r["ascent_m"] <= 760
    assert 7 <= r["distance_km"] <= 11
    assert r["max_ele_m"] >= 1450
    # DIN time recomputed from final numbers
    assert r["din_min"] > 240
    lats, lons = decode_polyline(r["polyline"])
    assert len(lats) > 10
    # never touches the T4 line's interior points
    t4_lat = np.array([47.6150 + (47.6305 - 47.6150) * i / 3 for i in (1, 2)])
    t4_lon = np.array([11.8150 + (11.8300 - 11.8150) * i / 3 for i in (1, 2)])
    for la, lo in zip(t4_lat, t4_lon):
        assert not np.any((np.abs(np.array(lats) - la) < 1e-6) & (np.abs(np.array(lons) - lo) < 1e-6))
    # a loop from one station comes back to the same station
    loops = [x for x in peak_routes if x["type"] in ("loop", "out-and-back")]
    for x in loops:
        assert x["start"]["name"] == x["end"]["name"]


def test_selection_keeps_one_direction(mini_osm, dem, settings):
    _, g, params, stations, targets = _build(mini_osm, dem, settings)
    chosen = C.select(C.generate(g, stations, targets, params), stations, params)
    keys = [(min(c["start"], c["end"]), max(c["start"], c["end"]), c["target"]) for c in chosen]
    assert len(keys) == len(set(keys))


def test_cli_catalog_writes_files(mini_osm, settings, tmp_path, monkeypatch):
    import yaml

    from conftest import bump
    from trainhead import dem as D

    monkeypatch.setattr(D, "open_dem", lambda cache, bbox: D.FunctionDem(bump))
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    out = tmp_path / "catalog"
    rc = cli_main(["--settings", str(path), "catalog", "--osm", str(mini_osm), "--out", str(out),
                   "--dem-cache", str(tmp_path / "dem")])
    assert rc == 0
    routes = C.load_catalog(out)
    assert routes and all("polyline" in r for r in routes)
    meta = json.loads((out / "meta.json").read_text())
    assert meta["routes"] == len(routes)
    gpx_path = tmp_path / "r.gpx"
    assert cli_main(["--settings", str(path), "gpx", "--catalog", str(out), "--route", routes[0]["id"],
                     "--out", str(gpx_path)]) == 0
    assert "<trkpt" in gpx_path.read_text()


def test_destination_in_middle_of_a_path_is_reachable(mini_osm, dem, settings):
    _, g, params, stations, targets = _build(mini_osm, dem, settings)
    assert "Testhütte" in {t["name"] for t in targets}
    routes = [C.finalize(g, c, stations, targets, dem)
              for c in C.select(C.generate(g, stations, targets, params), stations, params)]
    assert any(r["target"]["name"] == "Testhütte" for r in routes)


def test_mountain_railway_station_is_skipped(mini_osm, settings):
    osm = read_osm(str(mini_osm), settings["region"]["bbox"])
    assert "Testbergbahn Talstation" not in {s["name"] for s in osm.stations}
    assert {b["name"] for b in osm.bus_stops} == {"Almbus Wendeplatz", "Talbahnhof Bus"}


def test_bus_stop_trailheads(mini_osm, dem, settings):
    osm, g, params, _, targets = _build(mini_osm, dem, settings)
    stations = C.prepare_stations(osm, g, dem, params, targets=targets)
    by_name = {s["name"]: s for s in stations}
    assert by_name["Almbus Wendeplatz"]["mode"] == "bus"
    assert "Talbahnhof Bus" not in by_name  # within 3 km of a railway station
    assert by_name["Talbahnhof"]["mode"] == "rail"
    routes = [C.finalize(g, c, stations, targets, dem)
              for c in C.select(C.generate(g, stations, targets, params), stations, params)]
    bus_routes = [r for r in routes if "bus" in (r["start"]["mode"], r["end"]["mode"])]
    assert bus_routes
    assert all(r["max_sac"] <= 3 for r in bus_routes)


def test_no_bus_stops_when_disabled(mini_osm, dem, settings):
    osm, g, params, _, targets = _build(mini_osm, dem, settings)
    params.bus_stops = False
    stations = C.prepare_stations(osm, g, dem, params, targets=targets)
    assert all(s["mode"] == "rail" for s in stations)


def test_skips_on_demand_and_foreign_stops_and_reads_hut_buildings(mini_osm, settings):
    osm = read_osm(str(mini_osm), settings["region"]["bbox"])
    names = {b["name"] for b in osm.bus_stops}
    assert "AST 303" not in names and "Grenzalm" not in names
    huts = [t for t in osm.targets if t["name"] == "Weghütte"]
    assert huts and huts[0]["kind"] == "hut" and huts[0]["id"] < 0
    assert abs(huts[0]["lat"] - 47.62985) < 1e-4


def test_selection_covers_every_destination_first():
    params = C.CatalogParams(total=3, per_target=4, per_station=30)
    stations = [{"name": "A"}, {"name": "B"}, {"name": "C"}]
    cands = [{"start": 0, "end": e, "target": 0, "score": 0.9 - e / 100} for e in (1, 2)]
    cands += [{"start": 1, "end": 2, "target": 0, "score": 0.85}]
    cands += [{"start": 0, "end": 1, "target": 1, "score": 0.2}]
    chosen = C.select(cands, stations, params)
    assert len(chosen) == 3
    assert {c["target"] for c in chosen} == {0, 1}
