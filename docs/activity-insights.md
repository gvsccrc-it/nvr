# Activity insights

**Insights** (in the top navigation) shows how many people and vehicles the cameras saw, where they went, and lets you download the numbers.
It reads the tracks the AI stored; nothing here is a new measurement.

## What is counted

- **Camera:** each track counts once, in the hour or day it began.
- **Zone:** for each zone drawn in an enabled *zone entry* rule on a camera, a track counts once, when it first enters.
- **Line:** for each line in an enabled *line crossing* rule, every crossing counts (in the rule's direction, or both), so someone who
  walks across and back counts twice. The rule's schedule, cooldown and alarm settings do not matter here, only its shape and the kinds
  of object it watches (a line that watches people only counts people).
- Hours and days follow the site clock; a day with a clock change is 23 or 25 hours long. Hourly counts cover up to 31 days, daily up to 92.

## Time that was not analysed

Every hour or day shows how much of it the AI was analysing the camera. Where it was not all of it (the AI was off, delayed, failed or
interrupted) the chart shades it and the CSV says "Not fully analysed: counts may be lower than what happened". An hour that has not
happened yet is not called unanalysed. A quiet bar in a shaded hour means nothing was looked at, not that nothing happened.

## Heatmap

For one camera and the chosen period: how many times an object's centre was sampled in each cell of a 32 × 18 grid of the frame, drawn
over a still from the camera's recordings (with any privacy masks applied; with no recording it is drawn on a plain background).
Brighter cells are where objects spent more time.

## How fast it answers (#515)

The counts and the heatmap are added up as the AI stores what it sees, an hour at a time, so a month of a busy camera answers in well
under a second instead of minutes. **Stated bound: 31 days of one camera with 10 million detections answers within 1 second**, by the
hour or by the day, and so does its heatmap (measured on a generated month of 300,000 tracks: Insights about 50-130 ms, the heatmap
about 270-300 ms, 110 ms for one kind of object; reading the rows instead costs about 5-6 µs a detection for Insights with a zone or line
rule, about 50-60 s for that month, and about 2 µs a detection for the heatmap, about 20 s). The answer is the same either way:

- The **totals** (migration 109, `app/insight_totals.py`) hold, for each camera and hour, each track and the quarter hour it began in, the
  zone entries and line crossings per quarter hour, and the heatmap's cells and the tracks seen. Quarter hours, because a site's hour or day
  starts on one in every time zone in use (India's and Nepal's half and three-quarter hours included).
- **What is read from the rows as before**: the ragged ends of the range (less than a quarter hour, or an hour for the heatmap), the last
  hour or so (an hour is added up once it is older than the thinning horizon and five minutes), and any hour whose totals are not current.
- **Kept current by the database itself**: triggers take the "current" mark off every hour a write can change (a detection stored, moved,
  thinned or removed by retention; a track added, removed or relabelled) in the same transaction as the write, and the background worker
  adds those hours up again. A detection stored after the last of its track, which is what the AI worker does, touches only its own hour.
- **A change of privacy masks or of a zone or line rule** makes the camera's totals unused at once (they were added up under the old
  shapes); the background adds the camera up again from scratch, newest hours first, while the rest is read from the rows.
- **After an upgrade** nothing is added up at start-up: the background works back through the stored detections, newest first, a couple
  of seconds of work at a time, and until an hour is done it is read from the rows. A salvaged database adds everything up again.

Privacy masks apply exactly as before: a detection behind a mask is not in any total, and a track counts only when some of it is visible
in the range.

## CSV

`/api/insights/export.csv` (the Download CSV button) has one row per hour or day for each camera, zone and line:
`bucket_start_local, bucket_start_utc, kind, name, camera, person, vehicle, analysed_percent, note`. Names that a spreadsheet could run
as a formula are prefixed with an apostrophe.

## Access

Insights are for anyone signed in to the main interface (administrators and operators), and only for the cameras they may see: a camera
they cannot see is left out, and asking for it answers like a camera that does not exist. Tokens need the read scope (the reference still
also needs media).
