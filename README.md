# trainhead

Weekend day hikes and trail runs from Munich, by regional train and bus.

Every Thursday evening: two picks and a bad-weather fallback, each with the
route, walking and running time, difficulty, the train there and the last
sensible train back. Friday evening: a ping only if something changed.

## How it works

1. **Catalog (monthly, GitHub Actions).** Reads OpenStreetMap for the Prealps
   and Alps south of Munich (Oberbayern and Schwaben) and a 30 m elevation
   model. Builds day hikes from a station or mountain bus stop over a summit,
   hut or viewpoint to the same or another stop, using only trails the
   difficulty rules allow. Writes about 1,500 routes to the `data` branch.
   Also rebuilds when catalog code or the hiking settings change.
2. **Weekly data (Thursday morning, GitHub Actions).** Filters the catalog by
   your settings and history, scores it on the weekend forecast at the start
   and at the highest point, and checks real connections from home to the
   start and back from the end. Writes `weekly/latest.json` to the `data`
   branch.
3. **Plan (Thursday evening, Claude).** A mastermind run with four subagents
   (conditions, logistics, route scout, safety checker) picks from the weekly
   data and publishes route cards to the
   [route page](https://claude.ai/artifact/NpeYLgvDoYNfEV29p1a2Us). See
   [MASTERMIND.md](MASTERMIND.md).
4. **Recheck (Friday, GitHub Actions + Claude).** Fresh weather and
   connections for the picks; the page and message change only when
   something did.
5. **Ratings.** After the weekend, one tap per route on the page (too easy,
   right, too hard, skipped). The next Thursday run copies them into
   `state/history.json` and plans with them.

## The difficulty rules

- Trails are classified by the OpenStreetMap `sac_scale` tag: T1 path, T2
  mountain path, T3 demanding mountain path, T4 and above alpine. Roughly,
  DAV blue ≈ T2, red ≈ T3, black ≈ T4 and above.
- Routes only use trail segments tagged at or below `hiking.max_sac`.
- Paths without a difficulty tag are only used below
  `hiking.untagged_path_max_ele_m` (1,000 m by default), where they are almost
  always easy. Tracks and roads carry no difficulty.
- Walking time follows DIN 33466: 4 km/h on the flat, 300 m/h up, 500 m/h
  down; the larger of horizontal and vertical time counts fully, the smaller
  one half. Breaks come on top (`hiking.break_factor`).
- Running time uses effort distance: km + ascent/100, times your pace.

## Settings

Everything adjustable is in [config/settings.yaml](config/settings.yaml). Ask
the mastermind in the Project chat, or edit the file. Changing
`hiking.max_sac`, `hiking.untagged_path_max_ele_m` or `region.bbox` needs a
catalog rebuild: Actions → catalog → Run workflow.

## Running it locally

```
pip install -r requirements-dev.txt
python -m pytest -q
python -m trainhead.cli catalog --osm region.osm.pbf --out out/catalog
python -m trainhead.cli weekly --catalog out/catalog --out out/weekly
python -m trainhead.cli show --catalog out/catalog --route <id>
python -m trainhead.cli gpx --catalog out/catalog --route <id> --out route.gpx
```

## Data sources

- Trails, stations and destinations: © OpenStreetMap contributors, ODbL,
  via the Geofabrik extract for Oberbayern.
- Elevation: Copernicus DEM GLO-30, © DLR e.V. 2010-2014 and © Airbus Defence
  and Space GmbH 2014-2018, provided under COPERNICUS by the European Union
  and ESA. Fallback: AWS Terrain Tiles.
- Weather: [Open-Meteo](https://open-meteo.com), CC BY 4.0. The free API is
  for non-commercial use; a public or sponsored version would need their paid
  plan.
- Connections: [Transitous](https://transitous.org) ([sources](https://transitous.org/sources/)),
  a community routing service on open timetable data. Its usage policy asks
  for a contact in every request (`transit.contact` in the settings), asks
  projects to get in touch before using route planning, and rules out
  commercial use. trainhead sends about 150 paced requests a week.
