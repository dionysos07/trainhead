import io
import json
import zipfile
from datetime import date

import yaml

from trainhead import plan as P
from trainhead.cli import main as cli_main
from trainhead.store import load_catalog

from test_weekly import FakeClient, fake_weather


def _catalog(mini_osm, settings, tmp_path, monkeypatch):
    from conftest import bump
    from trainhead import dem as D

    monkeypatch.setattr(D, "open_dem", lambda cache, bbox: D.FunctionDem(bump))
    spath = tmp_path / "settings.yaml"
    spath.write_text(yaml.safe_dump(settings), encoding="utf-8")
    out = tmp_path / "catalog"
    assert cli_main(["--settings", str(spath), "catalog", "--osm", str(mini_osm), "--out", str(out),
                     "--dem-cache", str(tmp_path / "dem")]) == 0
    return spath, out


def test_page_files_from_plan(mini_osm, settings, tmp_path, monkeypatch):
    spath, cat = _catalog(mini_osm, settings, tmp_path, monkeypatch)
    routes = load_catalog(cat)
    weekly = P.run_weekly(settings, routes, [], date(2026, 10, 15), fake_weather, FakeClient())
    sat = weekly["days"]["2026-10-17"]["hikes"]
    assert sat, "synthetic catalog should give at least one Saturday hike"
    rid = sat[0]["route"]["id"]
    wpath = tmp_path / "weekly.json"
    wpath.write_text(json.dumps(weekly), encoding="utf-8")

    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / "latest.json").write_text(json.dumps({
        "weekend": ["2026-10-17", "2026-10-18"],
        "day_summaries": {"2026-10-17": "Dry and clear, 6 to 14 °C."},
        "picks": [{"date": "2026-10-17", "kind": "hike", "route_id": rid, "why": "Best day of the weekend."}],
        "fallback": {"route_id": rid, "why": "Low and short."},
    }), encoding="utf-8")
    site = tmp_path / "site"
    rc = cli_main(["--settings", str(spath), "page", "--plan", str(plans / "latest.json"), "--weekly", str(wpath),
                   "--catalog", str(cat), "--out", str(site)])
    assert rc == 0

    enriched = json.loads((plans / "latest.json").read_text())
    pick = enriched["picks"][0]
    assert pick["walk_start"] == "09:10" and pick["outbound_dep"] == "07:10"
    assert "weather_score" in pick and pick["storm"] is False
    assert (plans / "2026-10-17.json").exists()

    data = json.loads((site / "plan.json").read_text())
    card = data["days"][0]["cards"][0]
    assert card["route"]["id"] == rid and card["times"]["walk_start"] == "09:10"
    assert card["there"] and card["last"]["dep"]
    assert data["days"][0]["label"] == "Saturday 17 October"
    assert data["rules"][0] == ["Home stop", "München Hbf"]
    with zipfile.ZipFile(io.BytesIO((site / card["gpx"]).read_bytes())) as zf:
        names = zf.namelist()
        assert names == [f"{rid}.gpx"]
        assert "<trkpt" in zf.read(names[0]).decode()


def test_empty_page(settings, tmp_path):
    spath = tmp_path / "settings.yaml"
    spath.write_text(yaml.safe_dump(settings), encoding="utf-8")
    site = tmp_path / "site"
    assert cli_main(["--settings", str(spath), "page", "--empty", "--out", str(site)]) == 0
    data = json.loads((site / "plan.json").read_text())
    assert data["days"] == [] and data["next_plan"].endswith("evening")
    assert any(r[0] == "Hardest trail" for r in data["rules"])
