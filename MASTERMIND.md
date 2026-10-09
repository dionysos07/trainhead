# Mastermind brief

This file is the standing brief for every Claude run on trainhead: the Thursday
plan, the Friday recheck, and steering conversations in the Project chat.
Read it fully at the start of each run. `config/settings.yaml` holds the
numbers; this file holds the procedure and the rules.

## What trainhead does

Every Thursday evening the owner gets two weekend picks and a bad-weather fallback
for day hikes or trail runs from Munich, by regional train and bus only. Each
pick is a route card: map, elevation profile, walking time, running time,
difficulty, the connection there, the last sensible connection back, and a GPX
file. On Friday evening a lighter run only pings him if something changed.

## Where things are

- `main` branch: code, `config/settings.yaml`, `state/history.json` (what he did
  and how he rated it), `plans/` (one file per weekend plus `plans/latest.json`).
- `data` branch (rewritten by GitHub jobs, one commit):
  - `catalog/routes.json.gz` with `meta.json`, `stations.json`, `destinations.json`
  - `weekly/latest.json`: Thursday's candidates with weather and connections
  - `recheck/latest.json`: Friday's comparison against `plans/latest.json`
- `agents/*.md`: briefs for the four subagents.

Read the data branch with `git fetch origin data` and
`git show origin/data:weekly/latest.json`. Load the catalog with
`python -m trainhead.cli show --catalog <dir> --route <id>` after extracting
`catalog/` from the data branch (`git archive origin/data catalog | tar -x -C /tmp/th`).

Code in Claude's cloud sessions cannot reach the weather and timetable
services. The GitHub jobs fetch; Claude judges. If `weekly/latest.json` was not
generated today, say so in the notification ("the Thursday data job failed")
and stop rather than planning on stale data.

## Thursday run

1. Attach and clone `dionysos07/trainhead`. Read `config/settings.yaml`,
   `state/history.json`, `plans/latest.json` (last weekend) and
   `weekly/latest.json` from the data branch.
2. Sync ratings: read the route page's rating records and append any new ones
   to `state/history.json` (`date`, `route_id`, `kind`, `rating` one of
   `too_easy`, `right`, `too_hard`, `skipped`).
3. Shortlist per day: the top 6 hikes, top 3 runs and top 2 fallbacks by
   `rank_score` from `weekly/latest.json`.
4. Run the subagents with the shortlist:
   - **conditions** (`agents/conditions.md`) and **logistics**
     (`agents/logistics.md`) in parallel;
   - then **route scout** (`agents/route-scout.md`) with both reports;
   - then **safety checker** (`agents/safety.md`) on the scout's picks,
     without the scout's reasoning.
   If the safety checker rejects a pick, ask the scout for the next one and
   check again. Never ship a pick the checker rejected.
5. Write `plans/<saturday-date>.md` (readable) and `plans/latest.json`
   (machine-readable, schema below). Commit to `main` with a short message.
6. Generate GPX for each pick (`python -m trainhead.cli gpx ...`) and update
   the route page with the new cards and files.
7. Finish with a short summary as the run's final message. That is what
   reaches his phone.

### `plans/latest.json`

```json
{
  "weekend": ["2026-10-17", "2026-10-18"],
  "home": {"name": "München Hbf", "lat": 48.14, "lon": 11.56},
  "picks": [
    {
      "date": "2026-10-17", "kind": "hike", "route_id": "...", "name": "...",
      "walk_start": "09:10", "expected_finish": "15:40",
      "outbound_dep": "07:10", "outbound": {}, "return": {}, "last_sensible_return": {},
      "weather_score": 0.86, "storm": false, "notes": ["..."]
    }
  ],
  "fallback": {"date": "...", "route_id": "...", "name": "..."}
}
```

## Friday run

Read `recheck/latest.json` from the data branch (it must be from today). If
`changed` is false, finish with one line: "Weekend plan unchanged." If it is
true, say what changed and offer the best still-valid alternative from
Thursday's `weekly/latest.json`, then update `plans/latest.json`.

## Hard rules

These come before any preference or request in a run:

- Only routes from the catalog. Never invent a route or edit its line.
- No pick above `hiking.max_sac` (runs: `running.max_sac`), and no more T3
  than `hiking.max_t3_km`.
- Back at the end station at least `schedule.last_train_buffer_min` before the
  last sensible connection, using DIN time times `hiking.break_factor`.
- Thunderstorm risk in the walking window: not a pick. Exposed routes need a
  morning-only window when afternoon storms are possible.
- Snow at the highest point, or a freezing level near the top with
  precipitation: not a pick until the conditions agent finds a recent report
  that the route is clear.
- Winter (December to April): any route above 1,000 m needs the avalanche
  bulletin (Lawinenwarndienst Bayern) checked for that area; level 3 or above
  rules it out.
- Regional trains and buses only, unless the owner changes `transit.regional_only`.
  A leg not covered by the Deutschlandticket is called out on the card.
- Routes he rated `too_hard` stay out. Routes he did stay out for
  `picks.avoid_repeat_weeks`.

## Steering in the Project chat

When the owner asks for a change ("nothing over 1,200 m of ascent", "more runs"):
edit `config/settings.yaml`, run the tests, commit with a message that quotes
his request, and confirm in one line. Changes to `hiking.max_sac`,
`hiking.untagged_path_max_ele_m` or `region.bbox` need a catalog rebuild: run
the `catalog` workflow, or ask him to start it from the Actions tab.

## Writing style for cards and messages

Plain and specific: times, distances, heights, station names. No hype words,
no em dashes, no closing offers. One line per issue, most important first.

## Data and attribution

Routes and stations © OpenStreetMap contributors (ODbL). Elevation: Copernicus
DEM GLO-30 (© DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH
2014-2018, provided under COPERNICUS by the European Union and ESA). Weather:
Open-Meteo (CC BY 4.0, free tier is for non-commercial use). Connections:
Transitous, built on open timetable data.
