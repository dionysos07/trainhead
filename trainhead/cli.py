"""Command line entry points used by the GitHub workflows and the Claude runs.

    python -m trainhead.cli catalog --osm region.osm.pbf --out out/catalog
    python -m trainhead.cli weekly  --catalog data/catalog --out out/weekly
    python -m trainhead.cli recheck --catalog data/catalog --plan plans/latest.json --out out/recheck
    python -m trainhead.cli gpx     --catalog data/catalog --route <id> --out route.gpx
    python -m trainhead.cli show    --catalog data/catalog --route <id>
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import REPO_ROOT, load_settings

log = logging.getLogger("trainhead")


def _read_json(path: Path, default):
    if path and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def cmd_catalog(args) -> int:
    import osmium

    from . import catalog as C
    from . import graph as G
    from .dem import open_dem
    from .osm_extract import read_osm

    settings = load_settings(args.settings)
    bbox = settings["region"]["bbox"]
    osm = read_osm(args.osm, bbox)
    dem = open_dem(args.dem_cache, bbox)
    hk = settings["hiking"]
    g = G.build_graph(osm, dem, max_sac=int(hk["max_sac"]), untagged_max_ele_m=float(hk["untagged_path_max_ele_m"]),
                      anchors=C.anchors_for(osm))
    params = C.CatalogParams(bus_stops=bool(settings.get("catalog", {}).get("bus_stops", True)))
    targets = C.prepare_targets(osm, g, dem)
    stations = C.prepare_stations(osm, g, dem, params, targets=targets)
    cands = C.generate(g, stations, targets, params)
    chosen = C.select(cands, stations, params)
    pace = float(settings["running"]["pace_min_per_effort_km"])
    routes = [C.finalize(g, c, stations, targets, dem, pace) for c in chosen]
    routes.sort(key=lambda r: -r["score"])
    try:
        reader = osmium.io.Reader(args.osm, osmium.osm.NOTHING)
        osm_ts = reader.header().get("osmosis_replication_timestamp") or ""
        reader.close()
    except Exception:
        osm_ts = ""
    meta = {
        "built_at": datetime.now(ZoneInfo("Europe/Berlin")).isoformat(timespec="minutes"),
        "osm_timestamp": osm_ts,
        "max_sac": int(hk["max_sac"]),
        "untagged_path_max_ele_m": hk["untagged_path_max_ele_m"],
        "dem": type(dem).__name__,
        "candidates": len(cands),
    }
    C.write_catalog(routes, stations, targets, args.out, meta)
    log.info("catalog: %d routes from %d candidates", len(routes), len(cands))
    return 0 if routes else 1


def cmd_weekly(args) -> int:
    from . import transit as T
    from . import weather as W
    from .plan import run_weekly
    from .store import load_catalog

    settings = load_settings(args.settings)
    routes = load_catalog(args.catalog)
    history = _read_json(Path(args.history), [])
    tz = ZoneInfo(settings["schedule"]["timezone"])
    today = date.fromisoformat(args.today) if args.today else datetime.now(tz).date()
    result = run_weekly(settings, routes, history, today, W.fetch, T.Client(contact=settings["transit"].get("contact") or None))
    out = Path(args.out)
    _write_json(out / "latest.json", result)
    _write_json(out / f"{today.isoformat()}.json", result)
    n = sum(len(d.get(k, [])) for d in result["days"].values() for k in ("hikes", "runs", "fallbacks"))
    log.info("weekly: %d candidates", n)
    return 0 if n else 1


def cmd_recheck(args) -> int:
    from . import transit as T
    from . import weather as W
    from .plan import run_recheck
    from .store import load_catalog

    settings = load_settings(args.settings)
    routes = {r["id"]: r for r in load_catalog(args.catalog)}
    if args.from_weekly:
        # Recheck every candidate from Thursday. The personal picks stay private;
        # the Friday run looks its own picks up in this list.
        weekly = _read_json(Path(args.from_weekly), {})
        picks = []
        for day, d in (weekly.get("days") or {}).items():
            for kind in ("hikes", "runs", "fallbacks"):
                for e in d.get(kind, []):
                    picks.append({
                        "date": day, "kind": e["kind"], "route_id": e["route"]["id"],
                        "walk_start": e.get("walk_start"), "expected_finish": e.get("expected_finish"),
                        "weather_score": e["weather"].get("score"), "storm": bool(e["weather"].get("storm")),
                        "outbound_dep": ((e.get("outbound") or {}).get("dep") or "")[11:16] or None,
                    })
        plan = {"home": weekly.get("home"), "picks": picks}
    else:
        plan = _read_json(Path(args.plan), {})
    result = run_recheck(settings, routes, plan, W.fetch, T.Client(contact=settings["transit"].get("contact") or None))
    result["checked_at"] = datetime.now(ZoneInfo(settings["schedule"]["timezone"])).isoformat(timespec="minutes")
    _write_json(Path(args.out) / "latest.json", result)
    log.info("recheck: changed=%s", result["changed"])
    return 0


def cmd_page(args) -> int:
    """Enrich plans/latest.json from the weekly data and write the page files."""
    from datetime import timedelta

    from . import page as PG

    settings = load_settings(args.settings)
    tz = ZoneInfo(settings["schedule"]["timezone"])
    today = datetime.now(tz).date()
    days_to_thu = (3 - today.weekday()) % 7
    next_thu = today + timedelta(days=days_to_thu)
    next_plan = f"{PG.day_label(next_thu.isoformat())}, evening"
    out = Path(args.out)
    if args.empty:
        data = {"updated": None, "notice": None, "next_plan": next_plan, "home": settings["home"]["stop"],
                "days": [], "fallback": None, "previous": [], "rules": PG.rules_summary(settings)}
        (out / "gpx").mkdir(parents=True, exist_ok=True)
        _write_json(out / "plan.json", data)
        print(json.dumps({"files": ["plan.json"]}))
        return 0

    from .store import load_catalog

    routes = {r["id"]: r for r in load_catalog(args.catalog)}
    plan_path = Path(args.plan)
    plan = _read_json(plan_path, {})
    if args.weekly:
        weekly = _read_json(Path(args.weekly), {})
        plan = PG.enrich_plan(plan, weekly)
        plan["updated"] = datetime.now(tz).isoformat(timespec="minutes")
        _write_json(plan_path, plan)
        if plan.get("weekend"):
            _write_json(plan_path.parent / f"{plan['weekend'][0]}.json", plan)
    previous = _read_json(Path(args.previous), None) if args.previous else None
    data = PG.site_data(plan, routes, settings, previous, notice=args.notice, next_plan=next_plan)
    files = PG.write_site(data, routes, out)
    missing = [p["route_id"] for p in plan.get("picks", []) if p.get("route_id") not in routes]
    print(json.dumps({"files": files, "missing_routes": missing}, ensure_ascii=False))
    return 1 if missing else 0


def _find(routes, rid):
    for r in routes:
        if r["id"] == rid:
            return r
    raise SystemExit(f"route {rid} not in catalog")


def cmd_gpx(args) -> int:
    from .geo import decode_polyline, to_gpx
    from .store import load_catalog

    r = _find(load_catalog(args.catalog), args.route)
    lats, lons = decode_polyline(r["polyline"])
    desc = f"{r['distance_km']} km, {r['ascent_m']} m up, {r['descent_m']} m down. Data: OpenStreetMap contributors (ODbL)."
    Path(args.out).write_text(to_gpx(r["name"], lats, lons, description=desc), encoding="utf-8")
    return 0


def cmd_show(args) -> int:
    from .store import load_catalog

    r = dict(_find(load_catalog(args.catalog), args.route))
    if not args.full:
        r.pop("polyline", None)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="trainhead")
    p.add_argument("--settings", default=str(REPO_ROOT / "config" / "settings.yaml"))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("catalog")
    c.add_argument("--osm", required=True)
    c.add_argument("--out", default="out/catalog")
    c.add_argument("--dem-cache", default=".cache/dem")
    c.set_defaults(fn=cmd_catalog)

    w = sub.add_parser("weekly")
    w.add_argument("--catalog", default="data/catalog")
    w.add_argument("--history", default=str(REPO_ROOT / "state" / "history.json"))
    w.add_argument("--out", default="out/weekly")
    w.add_argument("--today", help="YYYY-MM-DD, for testing")
    w.set_defaults(fn=cmd_weekly)

    r = sub.add_parser("recheck")
    r.add_argument("--catalog", default="data/catalog")
    r.add_argument("--plan", default=str(REPO_ROOT / "plans" / "latest.json"))
    r.add_argument("--from-weekly", help="recheck every candidate in this weekly file instead of a plan")
    r.add_argument("--out", default="out/recheck")
    r.set_defaults(fn=cmd_recheck)

    x = sub.add_parser("gpx")
    x.add_argument("--catalog", default="data/catalog")
    x.add_argument("--route", required=True)
    x.add_argument("--out", required=True)
    x.set_defaults(fn=cmd_gpx)

    pg = sub.add_parser("page")
    pg.add_argument("--plan", default=str(REPO_ROOT / "plans" / "latest.json"))
    pg.add_argument("--weekly", help="weekly/latest.json from the data branch; enriches the plan when given")
    pg.add_argument("--catalog", default="data/catalog")
    pg.add_argument("--previous", help="last weekend's plan file, for the rating list")
    pg.add_argument("--notice", help="a banner line, e.g. the Friday change")
    pg.add_argument("--empty", action="store_true", help="page files with no plan yet")
    pg.add_argument("--out", default="out/site")
    pg.set_defaults(fn=cmd_page)

    s = sub.add_parser("show")
    s.add_argument("--catalog", default="data/catalog")
    s.add_argument("--route", required=True)
    s.add_argument("--full", action="store_true")
    s.set_defaults(fn=cmd_show)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
