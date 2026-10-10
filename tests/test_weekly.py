from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trainhead import plan as P
from trainhead import transit as T
from trainhead import weather as W

TZ = ZoneInfo("Europe/Berlin")
RULES = {"max_precip_mm": 1.0, "storm_cape_jkg": 800, "max_gusts_kmh": 60, "snow_depth_max_m": 0.05}


def make_loc(day="2026-10-17", ndays=2, **over):
    times = []
    d0 = date.fromisoformat(day)
    for k in range(ndays):
        for h in range(24):
            times.append(f"{(d0 + timedelta(days=k)).isoformat()}T{h:02d}:00")
    n = len(times)

    def series(name, default):
        v = over.get(name, default)
        return v(times) if callable(v) else [v] * n

    return {
        "hourly": {
            "time": times,
            "temperature_2m": series("temperature_2m", 12.0),
            "precipitation": series("precipitation", 0.0),
            "precipitation_probability": series("precipitation_probability", 5),
            "weather_code": series("weather_code", 1),
            "cloud_cover": series("cloud_cover", 20),
            "wind_gusts_10m": series("wind_gusts_10m", 25),
            "freezing_level_height": series("freezing_level_height", 3200),
            "snow_depth": series("snow_depth", 0.0),
            "cape": series("cape", 0),
            "lightning_potential": series("lightning_potential", 0),
            "visibility": series("visibility", 30000),
        },
        "daily": {
            "time": [(d0 + timedelta(days=k)).isoformat() for k in range(ndays)],
            "sunset": [f"{(d0 + timedelta(days=k)).isoformat()}T18:25" for k in range(ndays)],
        },
    }


def window():
    return datetime(2026, 10, 17, 9, 0), datetime(2026, 10, 17, 15, 0)


def test_fair_weather_scores_high():
    a = W.assess(make_loc(), make_loc(), *window(), top_ele=1800, rules=RULES)
    assert a["score"] > 0.9 and a["flags"] == [] and not a["storm"]


def test_afternoon_thunderstorm_is_flagged():
    def codes(times):
        return [95 if t.endswith(("14:00", "15:00")) else 1 for t in times]
    a = W.assess(make_loc(), make_loc(weather_code=codes), *window(), top_ele=1800, rules=RULES)
    assert a["storm"] and "thunderstorm risk" in a["flags"] and a["score"] < 0.35


def test_storm_outside_window_is_ignored():
    def codes(times):
        return [95 if t.endswith("19:00") else 1 for t in times]
    a = W.assess(make_loc(), make_loc(weather_code=codes), *window(), top_ele=1800, rules=RULES)
    assert not a["storm"]


def test_snow_and_freezing_level():
    top = make_loc(snow_depth=0.12, freezing_level_height=1500, precipitation=0.3)
    a = W.assess(make_loc(), top, *window(), top_ele=1800, rules=RULES)
    assert any("snow at the top" in f for f in a["flags"])
    assert any("freezing level" in f for f in a["flags"])
    assert a["score"] < 0.3


def _leg(mode, line, frm, to, dep, arr, agency="DB Regio", alerts=None, cancelled=False):
    return {"mode": mode, "displayName": line, "from": {"name": frm}, "to": {"name": to},
            "startTime": dep, "endTime": arr, "agencyName": agency, "alerts": alerts or [],
            "cancelled": cancelled, "realTime": False}


def _it(legs, dep, arr, transfers=1):
    d = datetime.fromisoformat(dep.replace("Z", "+00:00"))
    a = datetime.fromisoformat(arr.replace("Z", "+00:00"))
    return {"duration": int((a - d).total_seconds()), "startTime": dep, "endTime": arr,
            "transfers": transfers, "legs": legs}


def test_summarize_regional_connection():
    it = _it([
        {"mode": "WALK"},
        _leg("SUBWAY", "U3", "München Hbf", "Implerstraße", "2026-10-17T05:12:00Z", "2026-10-17T05:24:00Z", "MVG"),
        _leg("REGIONAL_RAIL", "RB 55", "München Siemenswerke", "Bayrischzell", "2026-10-17T05:40:00Z",
             "2026-10-17T07:02:00Z", "Bayerische Regiobahn",
             alerts=[{"headerText": "Bauarbeiten: Ersatzverkehr zwischen Schliersee und Bayrischzell"}]),
    ], "2026-10-17T05:10:00Z", "2026-10-17T07:02:00Z")
    s = T.summarize(it, TZ)
    assert s["dep"].startswith("2026-10-17T07:10")
    assert s["covered_by_dticket"]
    assert [l["line"] for l in s["legs"]] == ["U3", "RB 55"]
    assert any("Ersatzverkehr" in i for i in s["issues"])


@pytest.mark.parametrize("leg,covered", [
    (_leg("HIGHSPEED_RAIL", "ICE 513", "A", "B", "2026-10-17T06:00:00Z", "2026-10-17T07:00:00Z"), False),
    (_leg("RAIL", "IC 2083", "A", "B", "2026-10-17T06:00:00Z", "2026-10-17T07:00:00Z"), False),
    (_leg("REGIONAL_RAIL", "IRE 3", "A", "B", "2026-10-17T06:00:00Z", "2026-10-17T07:00:00Z"), True),
    (_leg("RAIL", "Zahnradbahn", "Garmisch", "Eibsee", "2026-10-17T06:00:00Z", "2026-10-17T07:00:00Z",
          agency="Bayerische Zugspitzbahn Bergbahn AG"), False),
    (_leg("BUS", "9562", "Schliersee", "Spitzingsee", "2026-10-17T06:00:00Z", "2026-10-17T07:00:00Z", "RVO"), True),
])
def test_dticket_coverage(leg, covered):
    s = T.summarize(_it([leg], leg["startTime"], leg["endTime"], 0), TZ)
    assert s["covered_by_dticket"] is covered


class FakeClient:
    """Outbound: leaves 07:10, arrives 09:00. Return: last train leaves `last_dep` (local)."""

    def __init__(self, last_dep="20:30"):
        self.last_dep = last_dep
        self.calls = []

    def geocode_stop(self, text, near=None):
        return {"name": text, "id": "de:09162:1110", "lat": 48.14, "lon": 11.56}

    def plan(self, from_place, to_place, when, arrive_by=False, num=3, regional_only=True, max_transfers=None):
        self.calls.append((from_place, to_place, when, arrive_by))
        day = when.astimezone(TZ).date()

        def at(hhmm):
            h, m = map(int, hhmm.split(":"))
            return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ).astimezone(ZoneInfo("UTC")).strftime(
                "%Y-%m-%dT%H:%M:%SZ")

        if from_place.startswith("48.14"):
            dep, arr = at("07:10"), at("09:00")
        elif arrive_by:
            h, m = map(int, self.last_dep.split(":"))
            dep = at(self.last_dep)
            arr = at(f"{h + 1:02d}:{m:02d}")
        else:
            local = when.astimezone(TZ)
            dep_dt = local + timedelta(minutes=10)
            dep = dep_dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
            arr = (dep_dt + timedelta(minutes=95)).astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
        leg = _leg("REGIONAL_RAIL", "RB 57", "X", "Y", dep, arr, "Bayerische Regiobahn")
        return {"itineraries": [_it([leg], dep, arr, 0)]}


def _route(rid, din, run_min=150, max_sac=2, max_ele=1600, score=0.8, start="Bayrischzell", end="Osterhofen"):
    return {
        "id": rid, "name": rid, "type": "traverse", "distance_km": 14.0, "ascent_m": 1000, "descent_m": 1000,
        "max_ele_m": max_ele, "min_ele_m": 780, "din_min": din, "run_min": run_min, "max_sac": max_sac,
        "t3_km": 0.0, "marked_share": 0.8, "unpaved_share": 0.8, "road_km": 1.0, "busy_road_km": 0.0,
        "score": score,
        "start": {"name": start, "lat": 47.674, "lon": 12.012},
        "end": {"name": end, "lat": 47.690, "lon": 11.990},
        "target": {"name": f"Peak {rid}", "kind": "peak", "ele": max_ele, "lat": 47.70, "lon": 12.01},
    }


def fake_weather(points, start_date, end_date, tz):
    return {p["key"]: make_loc(start_date, 2) for p in points}


def test_weekly_candidates_with_transit(settings):
    routes = [_route("a", 300), _route("b", 420, start="Lenggries", end="Lenggries")]
    res = P.run_weekly(settings, routes, [], date(2026, 10, 15), fake_weather, FakeClient())
    assert set(res["days"]) == {"2026-10-17", "2026-10-18"}
    sat = res["days"]["2026-10-17"]
    ids = [c["route"]["id"] for c in sat["hikes"]]
    assert ids and set(ids) <= {"a", "b"}
    c = sat["hikes"][0]
    assert c["walk_start"] == "09:10"
    assert c["covered_by_dticket"]
    assert c["buffer_min"] >= settings["schedule"]["last_train_buffer_min"]
    assert c["suggested_return"] is not None


def test_last_train_buffer_drops_long_routes(settings):
    # 7 h DIN x 1.15 = 8 h 3 min of walking from 09:10 -> finish 17:13; last train 17:30 leaves 17 min.
    routes = [_route("long", 420)]
    res = P.run_weekly(settings, routes, [], date(2026, 10, 15), fake_weather, FakeClient(last_dep="17:30"))
    assert res["days"]["2026-10-17"]["hikes"] == []


def test_recently_done_and_too_hard_routes_are_skipped(settings):
    routes = [_route("done", 300), _route("hard", 300, start="Kochel"), _route("fresh", 300, start="Tegernsee")]
    history = [
        {"date": "2026-10-03", "route_id": "done", "rating": "right"},
        {"date": "2026-06-01", "route_id": "hard", "rating": "too_hard"},
    ]
    pools = P.prefilter(routes, settings, history, date(2026, 10, 15))
    assert [r["id"] for r in pools["hike"]] == ["fresh"]


def test_weekend_dates_from_thursday():
    assert P.weekend_dates(date(2026, 10, 15), ["saturday", "sunday"]) == [date(2026, 10, 17), date(2026, 10, 18)]
    assert P.weekend_dates(date(2026, 10, 17), ["saturday", "sunday"]) == [date(2026, 10, 17), date(2026, 10, 18)]


def test_recheck_flags_new_storm(settings):
    route = _route("a", 300)
    plan = {"home": {"lat": 48.14, "lon": 11.56},
            "picks": [{"route_id": "a", "date": "2026-10-17", "walk_start": "09:10", "expected_finish": "15:00",
                       "weather_score": 0.95, "storm": False, "outbound_dep": "07:10"}]}

    def stormy(points, s, e, tz):
        def codes(times):
            return [95 if t.endswith("13:00") else 1 for t in times]
        return {p["key"]: make_loc(s, 2, weather_code=codes) for p in points}

    res = P.run_recheck(settings, {"a": route}, plan, stormy, FakeClient())
    assert res["changed"]
    assert any("thunderstorm" in r for r in res["checks"][0]["reasons"])

    calm = P.run_recheck(settings, {"a": route}, plan, fake_weather, FakeClient())
    assert not calm["changed"]


class FailingClient(FakeClient):
    def plan(self, *args, **kwargs):
        raise T.TransitError("503 for /api/v6/plan")


def test_transit_outage_keeps_unchecked_candidates(settings):
    routes = [_route("a", 300), _route("b", 300, start="Lenggries")]
    res = P.run_weekly(settings, routes, [], date(2026, 10, 15), fake_weather, FailingClient())
    hikes = res["days"]["2026-10-17"]["hikes"]
    assert hikes and all(h["transit_checked"] is False for h in hikes)
    assert res["notes"] and "failed" in res["notes"][0]


def test_weather_points_are_gridded():
    r1 = _route("a", 300)
    r2 = _route("b", 300)
    r2["start"] = dict(r2["start"], lat=r2["start"]["lat"] + 0.004)  # 450 m away: same grid cell
    pts = P.weather_points([r1, r2])
    assert len(pts) == 2
    for p in pts:
        assert p["ele"] % 50 == 0


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload
        self.text = "" if payload is not None else "not found"

    def json(self):
        return self._payload


class _Session:
    def __init__(self, responses):
        self.responses = responses
        self.paths = []

    def get(self, url, params=None, timeout=None, headers=None):
        path = url.split("transitous.org")[-1]
        self.paths.append(path)
        return self.responses[path]


def test_plan_falls_back_to_older_api_version():
    session = _Session({"/api/v6/plan": _Resp(404), "/api/v5/plan": _Resp(200, {"itineraries": []})})
    client = T.Client(pause_s=0, session=session)
    when = datetime(2026, 10, 17, 7, 0, tzinfo=TZ)
    assert client.plan("48.1,11.5", "47.6,11.9", when) == {"itineraries": []}
    assert client.plan("48.1,11.5", "47.6,11.9", when) == {"itineraries": []}
    assert session.paths == ["/api/v6/plan", "/api/v5/plan", "/api/v5/plan"]


def test_on_demand_leg_is_flagged():
    leg = _leg("BUS", "AST 303", "Kreuth", "Bayerwald", "2026-10-17T06:00:00Z", "2026-10-17T06:20:00Z", "RVO")
    s = T.summarize(_it([leg], leg["startTime"], leg["endTime"], 0), TZ)
    assert any("on-demand" in i for i in s["issues"])
