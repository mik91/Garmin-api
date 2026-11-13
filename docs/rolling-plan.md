# Rolling plan: design

Status: proposal (not built). Branch `rolling-plan-design`.

## Goal

Plan every workout from today to race day, keep them on the server as drafts, and send
only the next week to Garmin. Once a week, a review (code first, an agent optionally on
top) looks at the week that just ended, adjusts the next week's drafts and publishes them.

Why:
- **The app**: the Android app can show the whole plan (like Runna) instead of only
  what is already on the watch.
- **The watch**: only workouts that are about to happen are on Garmin, so it never shows
  stale ones.
- **The algorithm**: we keep what was planned months ahead, what the review changed, and
  what was done. The gaps between them show which rules need changing.

## What exists today

| Piece | Where | What it does |
|---|---|---|
| Season targets | `coaching/season.py` `generate()` | Pure function: anchor + A race + rules -> one target per Monday week (phase, light week, run km, long run, quality sessions from progressions, extras, swims, gym, flex). Re-derived on every call; holds from session reviews live in the anchor. |
| Session builders | `coaching/sessions.py` | Threshold / VO2 / race-pace / long-run formats per progression step and VDOT. |
| Workout build + validation | `coach.upload_plan()` | Builds Garmin payloads from `{sport, name, steps}`, checks every week against `rules.json`, dry-run or upload, idempotent on (date, name). |
| Weekly review job | `coaching/weekly.py` | Sunday 19:00: sync, then progress verdict, planned vs done, readiness, next week's target capped by this week's km. Stored as a `coach_log` "review" entry. |
| Session review | `coaching/execution.py` | Matches quality runs to laps; repeat or advance (`apply_holds` writes holds into the anchor); pace bias. |
| Readiness | `coaching/readiness.py` | green / yellow / red / unknown and actions. |
| Mobile API | `web_server.py` | `GET /api/plan` (scheduled [TP] workouts), `POST /api/plan/move` (the only token write). |

The missing link: targets are **weekly numbers**. The day-by-day workouts are written
each week by the agent just before upload, so nothing concrete exists beyond next week.

## Design

### 1. Drafted workouts table

```sql
CREATE TABLE IF NOT EXISTS plan_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL,          -- plan_versions.id it was generated in
    week_start TEXT NOT NULL,          -- Monday
    date TEXT NOT NULL,
    slot TEXT,                         -- am / midday / pm (null = any)
    sport TEXT NOT NULL,               -- run / swim / bike / gym
    name TEXT NOT NULL,                -- "[TP] ..." name used on Garmin
    kind TEXT,                         -- easy, long, threshold, vo2, race_pace, sharpen, css, race, flex
    spec_json TEXT NOT NULL,           -- {sport, name, steps, description, pool_length_m}
    session_json TEXT,                 -- upload_plan "session" hints (km, minutes, intensity...)
    status TEXT NOT NULL,              -- draft | proposed | published | done | skipped | superseded
    origin TEXT NOT NULL,              -- generator | review | agent | athlete
    reason TEXT,                       -- why it differs from the previous version (1-2 sentences)
    parent_id INTEGER,                 -- the draft this one replaced
    workout_id TEXT,                   -- coach_workouts.workout_id once published
    activity_id TEXT,                  -- matched activity once done
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    trigger TEXT NOT NULL,             -- start | regenerate | css_test | race_result | vdot | manual
    anchor_json TEXT NOT NULL,         -- the season anchor it came from
    rules_hash TEXT NOT NULL,          -- rules.json + overrides, to tell rule changes apart
    note TEXT
);
```

Rules:
- A row is never edited in place once it is `proposed` or `published`. A change adds a new
  row with `parent_id` and marks the old one `superseded`. That gives the
  original / adjusted / done history for free.
- `published` rows link to `coach_workouts`. Only [TP] workouts are ever written, as today.
- Versions live only in this table. Garmin always holds one workout per published row:
  a new version of a published day reuses its `workout_id` (updated in place, as
  `upload_plan` does for the same date + name). A new date is a reschedule (`move`), and a
  rename or a removed session deletes the old [TP] workout, so nothing stale or duplicated is
  left on the watch.
- `done` / `skipped` are set by the sync (match on date + sport, same logic as
  `execution._activity_for`).

### 2. Week materializer (code, no LLM)

New module `coaching/materialize.py`:

```python
def week(target: dict, profile: dict, rules: dict, paces: dict, css: float | None) -> list[dict]:
    """One season week target -> concrete days: [{date, slot, sport, kind, spec, session}]."""
```

- **Placement**: a weekly template from the profile's setup (long run day, office days,
  pool days, gym days, rest day) plus spacing rules: no two hard days back to back, long run
  not the day after quality, swim CSS sets not on the run quality day.
- **Runs**: quality sessions from `target["quality"]` (already built by `sessions.py`);
  long run from `long_run_km` + `long_run`; the remaining km spread over easy runs
  (HR range only, no pace, as the method says); strides / hills from `extras`.
- **Swims**: from `target["swim"]`, paces from CSS.
- **Gym / bike / flex**: from `structure`, flex marked "(opt)" in the preferred sport.
- **Validation**: every materialized week goes through the same validator `upload_plan`
  uses. A generated week that breaks a rule is a generator bug, so the tests assert zero
  errors over a full season for a few anchors and races.

It must be **pure and deterministic** (same inputs give the same days) so it can be
ported to the Android app, tested, and diffed.

### 3. Generate and regenerate

`plan.generate(conn, trigger)`:
1. `season.generate()` for all weeks; `materialize.week()` for each.
2. Write a new `plan_versions` row and `draft` rows for every week **after the last
   published week**. Older `draft` rows of those weeks become `superseded`.
3. Never touch `published`, `done`, `skipped` rows or the current week.

Triggers: season plan start or reset, new CSS test (`record_css_test`), a race or time
trial result that changes VDOT, a profile change (races, setup, rule_overrides), or manual.

### 4. Weekly roll forward

Extends the Sunday job in `weekly.py` (after the existing review is stored):

1. **Close the week**: mark drafts done / skipped from the synced activities.
2. **Review**: existing `progress`, `week_review`, `session_review` (holds), readiness.
3. **Adjust next week (rules, no LLM)**: build a *proposal* from next week's drafts:
   | Signal | Adjustment |
   |---|---|
   | Week km under target (missed sessions) | `targets_for` cap: scale easy runs down; never make up missed sessions. |
   | Quality session missed or failed | Hold: repeat the same step (already in the anchor via `apply_holds`), rematerialize that session. |
   | Readiness red | Swap quality for easy, cut the long run by the light-week rule, keep swims easy. |
   | Readiness yellow | Keep the plan, move quality away from day 1, drop the flex slot. |
   | Pace bias (runs too fast or too slow vs targets) | Flag only; changes paces after the athlete agrees. |
   | New caution in profile (niggle) | Flag; the agent or athlete decides. |
   Each changed day becomes a `proposed` row with `origin = "review"` and a `reason`.
4. **Agent pass (optional)**: an agent with the hosted connector reads the proposal plus
   the coaching history and either accepts it, edits it (`origin = "agent"`), or adds a
   note. The same tools work from Claude, the Android app's own model, or nothing.
5. **Approve and publish**: on approval, `upload_plan` for those rows (dry-run first, as
   now), set `published`, log a "plan" entry. Then refresh drafts for the weeks after.

**Approval policy** (rule in `rules.json`, overridable in the profile):
```json
"rolling": {
  "publish_days": 7,
  "auto_publish": "unchanged",     // never | unchanged | minor | always
  "minor": {"km_pct": 10, "no_quality_change": true}
}
```
- `unchanged`: if the review changed nothing, publish automatically; otherwise wait.
- `minor`: also publish automatically when the change is small (easy km within 10%, no
  quality, long run or day changes).
- Anything else waits for approval in the app (push notification) or from the agent.

### 5. Tools and API

MCP (hosted connector):
| Tool | Kind | Notes |
|---|---|---|
| `rolling_plan(start?, weeks?)` | read | Drafts by week with status, origin, reason and the season target. |
| `regenerate_plan(trigger, dry_run=true)` | write | Diff of the unpublished weeks before / after. |
| `propose_week(week_start, items, reason)` | write | Agent edits become `proposed` rows; validated like `upload_plan`. |
| `publish_week(week_start, dry_run=true)` | write | Uploads the week's approved rows with `upload_plan`. |
| `plan_drift(start?, end?)` | read | See section 6. |

Mobile API (OAuth token):
- `GET /api/rolling-plan?start&end`: whole plan, drafts and published, with status.
- `GET /api/rolling-plan/proposal?week`: the pending diff and reasons.
- `POST /api/rolling-plan/approve {week_start}` / `reject`: added to `TOKEN_POST_PATHS`.
- `POST /api/plan/move` keeps working for published days; moving a draft just updates it.

### 6. Plan drift report (algorithm feedback)

Per week and per rule, from `plan_drafts`:
- **Generator vs final**: how often and how much the first draft was changed before
  publishing, grouped by `kind` and by the review rule that changed it.
- **Final vs done**: done / skipped / modified per kind, km planned vs run.
- **Patterns to look for**:
  - Long runs cut every Nth week: `long_step_km` or `long_share` too aggressive.
  - VO2 steps repeated often: `ADVANCE_EVERY` for VO2 too fast, or VO2 paces too hard.
  - Easy km under target every week: `build_pct` / peak too high for the available time.
  - Same weekday skipped repeatedly: the placement template is wrong for the athlete.
  - Many red readiness swaps after a given session type: that session costs more than
    the plan assumes.

Shown in the app's review tab and as a section of the existing weekly review card.

## Android app port

What the app needs to do it on-device (a Runna-like plan without the server):
1. `season.generate` + `sessions.py` + `materialize.week` + the validator: pure code
   with `rules.json` as data. Keep them free of I/O so they port 1:1 (Kotlin).
2. Weekly adjustment table (section 4.3): pure rules over a week's summary.
3. Shared test fixtures: JSON inputs and expected weeks, run by both the Python and
   Kotlin test suites, so the two never drift.

The server stays the source of truth while both exist; the app reads `/api/rolling-plan`.

## Edge cases

- **Athlete edits a workout on Garmin**: [TP] payload hash changes; the next sync marks
  the draft `origin = "athlete"` and keeps it.
- **Mid-week change** (sick, sore): same as today (adjust the rest of the week, no make
  up), done through `propose_week`, so it is recorded like the weekly roll forward.
- **Race added or moved**: regenerate; phases shift; published week is kept.
- **Review job missed** (server down): the next run handles every unclosed week in order
  and publishes at most the coming week.
- **No approval by Monday**: nothing is published; the app shows the proposal. Optional
  rule: publish the unchanged draft for day 1 only, so the watch is not empty.

## Build order

1. `plan_drafts` / `plan_versions` tables and `materialize.week` with full-season tests. (done)
2. `regenerate_plan` + `rolling_plan` read tool; the app reads `/api/rolling-plan`. (done)
3. Weekly roll forward with rule adjustments and `publish_week`; manual approval only. (done:
   `roll_forward`, `decide_proposal`, `propose_week`, `publish_week`; the Sunday review stores the
   proposal. A removal is a `proposed_drop` row; a rejected change keeps status `rejected`.)
4. Approval policy (`rolling` rules) and app approve / reject. (done: `rolling.auto_publish` in
   rules.json, default `never`, set in the profile's `rule_overrides`; `POST /api/rolling-plan/approve`
   `{week_start, publish?}`, `/reject` `{week_start, note?}`, `/publish` `{week_start}` for OAuth tokens.)
5. `plan_drift` report. (done: `plan_drift` tool, `GET /api/rolling-plan/drift?start&end`, and
   `plan_drift` patterns on the weekly review card. A proposal that only removes workouts is not
   counted per rule yet: removals carry no reason of their own.)
6. Kotlin port with shared fixtures.

## Found while building step 1

Materializing four full seasons (10K, multi-event 10K, half, marathon) and validating every
week showed conflicts between `season.generate` and the validator, now fixed
(`tests/test_materialize.py` asserts zero rule errors in every week):
- **Back from a lighter week**: the plan jumped straight back to the normal volume (+25% on
  the lighter week), over the validator's cap. It now comes back over two weeks: halfway,
  then the volume from before the lighter week, and growth resumes after that.
  `targets_for` also used to cap that week at +10% on the lighter week; it now allows the
  plan's own growth when the lighter week was run as planned (a missed week still slows it).
- **CSS tests vs run all-outs**: a CSS test counted as all-out and landed within 28 days of a
  time trial or the race. The 28-day rule is for running; CSS tests no longer count.
- **Rounding**: weekly km grew with `round()`, which could end 0.05 km over the +10% cap; it
  now rounds down.

## After step 5

- **Mid-week changes**: `propose_week` takes a week already on Garmin from today to Sunday;
  approving uploads the changes (in place when the day and name stay) and deletes removed
  [TP] workouts. Rejecting puts the week back as it was.
- **Regenerates on its own**: a profile change the plan depends on (`swim_css` after a CSS
  test, `races`, `season_plan` including session-review holds, `plan_schedule`,
  `rule_overrides`) rebuilds the open weeks, and so does the Sunday job when current fitness
  moved 0.5 VDOT or more from the plan's paces.
- **Cap floor**: after a one-off short week (the 3 weeks before it were at 80%+ of this
  week's target) the cap is the validator's own rebuild limit (+20%) with the long run at its
  largest allowed share (30%), instead of +10% and 28%. A bigger floor would break the
  validator's ramp rule.
- **The app's Plan screen**: `/api/plan` adds future drafts in the same shape as the workouts
  on Garmin (`schedule_id` "draft:<id>", `source` "draft"; `drafts=0` leaves them out) and a
  `rolling` object: `auto_publish`, `next_week`, and each pending proposal with its reason, km
  before and after, and the changes. `/api/plan/move` moves drafts too (a new row, origin
  athlete), and keeps the plan in step when a published workout is moved.

## Open questions

- Publish window: 7 days, or 10-14 so the watch has a buffer if approval is late?
- Should holds and other review changes ripple into later weeks right away (regenerate)
  or only when that week becomes next?
- ~~Where the weekly placement template lives~~: the profile section `plan_schedule` (JSON
  over `materialize.DEFAULT_SCHEDULE`, e.g. `{"long": "sat", "quality": ["tue", "fri"]}`).
