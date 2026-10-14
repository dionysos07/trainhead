# Mastermind brief

The standing brief for every Claude run on trainhead: the Thursday plan, the
Friday check, and steering conversations in the Project chat. Read it fully at
the start of each run. `config/settings.yaml` holds the numbers; this file holds
the procedure and the rules.

## What trainhead does

Every Thursday evening the owner gets two weekend picks and a bad-weather fallback
for day hikes or trail runs from Munich, by regional train and bus only. Each
pick is a route card on the route page: route sketch, elevation profile,
walking and running time, hardest trail section, the connection there, the
last sensible connection back, the forecast, and a GPX file. On Friday evening
a lighter run checks the plan again and only reports what changed.

- Route page: https://claude.ai/artifact/NpeYLgvDoYNfEV29p1a2Us
- Repository: `dionysos07/trainhead` (attach with `add_repo`, push access)

## Where things are

- `main` branch (public): code, `config/settings.yaml` (general limits,
  starting from München Hbf), `site/index.html` (the route page),
  `agents/*.md` (subagent briefs).
- `data` branch (public), rewritten by the GitHub jobs (one commit,
  force-pushed): `catalog/` (routes, stations, destinations, meta),
  `weekly/latest.json` (Thursday's candidates with weather and connections),
  `recheck/latest.json` (Friday's fresh check of every Thursday candidate),
  `logs/`.
- Personal data lives only in the route page's private database, read and
  written with the ArtifactData tool on the page URL:
  - `ratings`: one document per pick (`<date>__<route_id>` with `rating` =
    `too_easy`, `right`, `too_hard` or `skipped`), written by the page.
  - `plans`: document `latest` and one document per weekend named after its
    Saturday (`2026-10-17`), each with a field `plan` holding the plan JSON.

The repository is public. Never commit plans, ratings, history, dates of
outings or anything else about the owner to it.

Get the data branch into a scratch directory:

```
git fetch origin data
mkdir -p /tmp/th && git archive origin/data | tar -x -C /tmp/th
```

Code in Claude's cloud sessions can't reach the weather and timetable
services; the GitHub jobs fetch, Claude judges. If there is no `data` branch
yet, the route catalog hasn't been built: finish with "No plan this week: the
route catalog is still being built." If `weekly/latest.json` was not
generated today (`generated_at`), stop and finish with one line: "The
Thursday data job didn't run, so there's no plan this week." Never plan from
stale data. In both cases, read `logs/weekly.log` (or `logs/catalog.log`) on
the data branch and add the error in a second line. The GitHub API also works
from these sessions through plain curl (the `gh` CLI isn't logged in):
`curl -sSL https://api.github.com/repos/dionysos07/trainhead/actions/runs?per_page=5`
lists runs, and a failed job's `/check-runs/<job id>/annotations` holds the end
of its log. Raw log downloads are blocked here.

## Thursday run

1. Attach and clone the repo, install `requirements-light.txt`
   (`pip install --break-system-packages -q -r requirements-light.txt`), get
   the data branch as above, check `weekly/latest.json` is from today.
2. History: list the `ratings` collection on the route page and write the
   entries to `/tmp/th/history.json` (outside the clone) as
   `{"date", "route_id", "kind", "rating"}`, one per date and route.
3. Read `config/settings.yaml`, the history, last weekend's plan (`plans/latest`
   in the page database; save its `plan` field to `/tmp/th/previous.json`) and
   `weekly/latest.json`. Apply the history rules yourself: drop candidates
   rated `too_hard` and routes done within `picks.avoid_repeat_weeks`. Note any
   `notes` in the weekly file (for example failed connection checks).
4. Shortlist per day: the top 6 hikes, top 3 runs and top 2 fallbacks by
   `rank_score`.
5. Run the four subagents with the Agent tool, each with its brief from
   `agents/` and only the data it needs:
   - **conditions** (`agents/conditions.md`) and **logistics**
     (`agents/logistics.md`) in parallel on the shortlist;
   - then **route scout** (`agents/route-scout.md`) with both reports;
   - then **safety checker** (`agents/safety.md`) on the scout's picks,
     without the scout's reasoning.
   If the checker rejects a pick, take the scout's next option and check
   again. Never ship a rejected pick.
6. Write `/tmp/th/plan.json` in this shape (the page command fills in times,
   connections and weather from the weekly file):

   ```json
   {
     "weekend": ["2026-10-17", "2026-10-18"],
     "day_summaries": {"2026-10-17": "Dry, 4 to 13 °C, snow line around 2,200 m."},
     "picks": [
       {"date": "2026-10-17", "kind": "hike", "route_id": "...", "why": "One or two plain sentences.",
        "notes": ["Hut closed for the season: bring water."]}
     ],
     "fallback": {"date": "2026-10-18", "route_id": "...", "why": "..."}
   }
   ```

7. Build the page files:

   ```
   python -m trainhead.cli page --plan /tmp/th/plan.json \
     --weekly /tmp/th/weekly/latest.json --catalog /tmp/th/catalog \
     --previous /tmp/th/previous.json --out /tmp/th/site
   ```

   It prints the files it wrote and fills times, connections and weather into
   `/tmp/th/plan.json`.
8. Publish: first `Artifact` `action: read` on the page URL, then
   `Artifact` publish with `url` = the page URL, `file_path` =
   `site/index.html`, and `files` mapping each printed path to
   `/tmp/th/site/<path>` (for example `{"plan.json": "/tmp/th/site/plan.json",
   "gpx/<id>.zip": "/tmp/th/site/gpx/<id>.zip"}`). Don't pass `capabilities` or
   `icon`; the page keeps its own.
9. Save the enriched plan to the page database: `plans/latest` and
   `plans/<Saturday>`, field `plan` (ArtifactData `set`; pass `if_version`
   when the document exists). Commit nothing to the repository.
10. Finish with a short message, which is what reaches his phone: one line
    per pick (day, route, walking time, first train), one line for the
    fallback, and any warning that matters.

## Friday run

1. Attach and clone, get the data branch, read `recheck/latest.json` and the
   plan from `plans/latest` in the page database. If there's no plan for this
   weekend, finish with "No weekend plan to check." If `checked_at` isn't from
   today, finish with "Friday check didn't run; the Thursday plan stands."
   The recheck covers every Thursday candidate; look up the plan's picks in
   its `checks` by `route_id` and `date`, and ignore the rest.
2. If none of the picks has `reasons`, finish with exactly: "Weekend plan unchanged."
3. If it changed: decide per affected pick whether it still works with a
   caveat (an earlier start, a different train) or needs replacing from
   Thursday's `weekly/latest.json`. A replacement goes through the safety
   checker subagent first. Update the plan, rebuild the page with
   `--notice "<one line on what changed>"` (add `--weekly` when a pick was
   replaced), republish as on Thursday, and save the plan to the page
   database as on Thursday.
4. Finish with what changed and what to do, in two or three lines.

## Hard rules

These come before any preference or request in a run:

- Only routes from the catalog. Never invent a route or edit its line.
- No pick above `hiking.max_sac` (runs: `running.max_sac`), and no more T3
  than `hiking.max_t3_km`.
- Back at the end stop at least `schedule.last_train_buffer_min` before the
  last sensible connection, using DIN time × `hiking.break_factor`
  (runs: running time × 1.1). Finish 30 minutes before sunset.
- Thunderstorm risk in the walking window: not a pick. Exposed routes need a
  morning-only window when afternoon storms are possible.
- Snow at the highest point, or a freezing level near the top with
  precipitation: not a pick unless the conditions agent finds a report from
  the last 14 days that the route is clear.
- December to April, anything above 1,000 m: check the Lawinenwarndienst
  Bayern bulletin for that area; danger level 3 or above rules it out.
- Regional trains and buses only, unless `transit.regional_only` is false.
  A leg not covered by the Deutschlandticket is called out on the card.
- Bus-stop trailheads only when the weekly data found a bus that day.
- Routes rated `too_hard` stay out. Routes done in the last
  `picks.avoid_repeat_weeks` weeks stay out.
- Candidates with `transit_checked: false` can be picked only when nothing
  checked is available, and the card says to confirm the trains.

## Steering in the Project chat

When the owner asks for a change ("nothing over 1,200 m of ascent", "more runs"):
edit `config/settings.yaml`, run `python -m pytest -q`, commit with a message
that quotes his request, push, and confirm in one line. Changes to `hiking`,
`running`, `region` or `catalog` settings rebuild the catalog automatically
on push (about half an hour); other settings apply from the next run.

## Writing style for cards and messages

Plain and specific: times, distances, heights, station names. No hype words,
no em dashes, no closing offers. One line per issue, most important first.

## Data and attribution

Routes and stations © OpenStreetMap contributors (ODbL). Elevation: Copernicus
DEM GLO-30 (© DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH
2014-2018, provided under COPERNICUS by the European Union and ESA). Weather:
Open-Meteo (CC BY 4.0, free tier for non-commercial use). Connections:
Transitous, built on open timetable data.
