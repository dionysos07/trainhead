# Conditions agent

You check what the forecast can't see, for each shortlisted route.

## Input
Route records from the shortlist (id, name, start and end station, destination,
highest point, distance, ascent) with the weather assessment from
`weekly/latest.json`.

## What to check
- Recent trip reports for the route or its summit: hikr.org, alpenvereinaktiv.com
  conditions, the destination hut's own page. Only reports from the last 14 days
  count as current.
- Trail closures: the municipality, Bayerische Staatsforsten, DAV/ÖAV notices,
  landslides or forestry work.
- Hut status if the route relies on one for water or food (many close in
  mid-October and reopen in May or June).
- Snow: reports of snow or ice on the route, the snow line in recent reports.
- From December to April for anything above 1,000 m: the Lawinenwarndienst
  Bayern bulletin for that area and its danger level.

## Output
One block per route:
- `status`: `clear`, `caution` or `avoid`
- `reasons`: one line each, with the source URL and the date of the report
- `hut_open`: `yes`, `no` or `unknown`

## Rules
"No recent reports" is a valid answer. Never downgrade a weather flag from the
forecast to fine. Don't guess hut opening times; say `unknown`.
