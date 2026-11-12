# Strength API (`/api/strength`)

Hevy lifting workouts, each linked to the overlapping Garmin strength activity, plus weekly
totals, and per-exercise progress across all history ([`/api/strength/exercise`](#exercise-progress-apistrengthexercise)).
The Android app and the `strength_workouts` and `exercise_progress` MCP tools read these. Fields are only ever
added, never renamed or removed.

## Source

With `HEVY_API_KEY` set (Hevy Pro: Settings > Developer), every sync (auto-sync, **Sync now**
or `python cli.py sync`) also pulls Hevy after Garmin:

- The first sync pages through `GET /v1/workouts`; later syncs use
  `GET /v1/workouts/events?since=...` for updated and deleted workouts.
- Exercise names, muscle groups, equipment and thumbnails come from `GET /v1/exercise_templates`.
  All templates are fetched again when a workout uses an unknown one, when a stored one has
  never been fetched with equipment and thumbnail (rows from before they were added), or every
  7 days.
- A workout is linked to the Garmin activity whose type contains `strength` and whose time
  window overlaps it most (15 min of slack either side). Unlinked workouts take their local
  time zone from the nearest Garmin activity.

Data is stored in `hevy_workouts`, `hevy_exercises`, `hevy_sets` and
`hevy_exercise_templates`. A Hevy failure never stops the Garmin sync; it is reported in
`sync.error`.

## Request

`GET /api/strength?start=YYYY-MM-DD&end=YYYY-MM-DD`

- Both optional: `end` defaults to today, `start` to 27 days before `end`. At most 731 days.
- Dates filter on the workout's local start date.
- Hosted mode: a dashboard session or an OAuth bearer token (read-only, like the other `GET /api/*`).
- Errors: `400 {"error": "..."}` for a bad range, `401` without valid auth.

## Response

```json
{
  "start": "2026-10-19",
  "end": "2026-10-26",
  "weight_unit": "kg",
  "sync": {
    "configured": true,
    "last_synced_at": "2026-10-26T08:00:03",
    "error": null
  },
  "workouts": [
    {
      "id": "b459cba5-cd6d-463c-abd6-54f8eafcadcb",
      "title": "Push",
      "description": null,
      "start_time": "2026-10-20T17:00:00Z",
      "end_time": "2026-10-20T18:00:00Z",
      "start_local": "2026-10-20T19:00:00",
      "date": "2026-10-20",
      "duration_s": 3600,
      "sets": 2,
      "reps": 14,
      "volume_kg": 1120.0,
      "exercises": [
        {
          "index": 0,
          "title": "Bench Press (Barbell)",
          "template_id": "79D0BB3A",
          "type": "weight_reps",
          "primary_muscle_group": "chest",
          "secondary_muscle_groups": ["triceps", "shoulders"],
          "superset_id": null,
          "notes": "",
          "equipment": "barbell",
          "thumbnail_url": "https://pump-app.s3.eu-west-2.amazonaws.com/exercise-thumbnails/...jpg",
          "sets": [
            {"index": 0, "type": "warmup", "weight_kg": 40, "reps": 10, "rpe": null, "distance_m": null, "duration_s": null},
            {"index": 1, "type": "normal", "weight_kg": 80, "reps": 8, "rpe": 8, "distance_m": null, "duration_s": null}
          ]
        }
      ],
      "garmin": {
        "activity_id": 1234567890,
        "name": "Strength",
        "type": "strength_training",
        "start_time": "2026-10-20 18:55:00",
        "duration_s": 3900,
        "calories": 320,
        "avg_hr": 112,
        "max_hr": 151,
        "aerobic_te": 2.1,
        "anaerobic_te": 1.4,
        "training_load": 55.5
      }
    }
  ],
  "weekly": [
    {"week_start": "2026-10-19", "sessions": 1, "sets": 2, "reps": 14, "volume_kg": 1120.0, "duration_s": 3600},
    {"week_start": "2026-10-26", "sessions": 0, "sets": 0, "reps": 0, "volume_kg": 0, "duration_s": 0}
  ]
}
```

### Fields

- `weight_unit`: `"kg"` or `"lb"`, the unit the user logs weights in, for display only. Every
  weight in the response stays in kg (`weight_kg`, `volume_kg`, weekly `volume_kg`); convert
  with 1 lb = 0.45359237 kg. Set by `HEVY_WEIGHT_UNIT` when that is `kg` or `lb`; otherwise
  detected from all logged sets: `"lb"` when most nonzero weights are within 0.01 lb of a
  multiple of 0.5 lb and not a multiple of 0.25 kg, else `"kg"` (also when nothing is logged).
- `sync.configured`: whether the server has a Hevy key. `last_synced_at` (server local time)
  and `error` are `null` until a sync has run or when it succeeded.
- `workouts`: newest first.
  - `start_time`/`end_time`: UTC, ISO 8601 with `Z`. `start_local`: wall-clock time without
    an offset; `date` is its first 10 characters.
  - `duration_s`: `end_time - start_time`, or `null` if either is missing.
  - `sets`, `reps`, `volume_kg`: totals over every set except `warmup`. Volume is
    `weight_kg × reps`; bodyweight sets (no weight) add reps but no volume.
- `exercises[]`: in workout order. `type` (the Hevy template type, e.g. `weight_reps`,
  `reps_only`, `duration`, `distance_duration`), `primary_muscle_group` and
  `secondary_muscle_groups` come from the exercise template and are `null`/`[]` if unknown.
  Exercises in the same superset share `superset_id`. `equipment` (Hevy's value, e.g.
  `barbell`, `dumbbell`, `machine`, `none`) and `thumbnail_url` (a JPEG on Hevy's CDN) come
  from the template too and are `null` until the template has been fetched, or when Hevy has
  none (custom exercises usually have no thumbnail).
- `sets[]`: in order. `type` is Hevy's set type: `warmup`, `normal`, `failure` or `dropset`.
  `weight_kg`, `reps`, `rpe`, `distance_m` (metres) and `duration_s` are numbers or `null`
  depending on the exercise type.
- `garmin`: the linked Garmin activity, or `null`. `start_time` is Garmin's local time
  (`YYYY-MM-DD HH:MM:SS`); HR in bpm; `aerobic_te`/`anaerobic_te` 0–5; any field may be `null`.
- `weekly`: one entry per Monday-starting week that touches the range, oldest first, including
  empty weeks. Only workouts inside `start`..`end` are counted, so the first and last weeks may
  be partial.

## Exercise progress (`/api/strength/exercise`)

`GET /api/strength/exercise?template_id=<id>`

Every session of one exercise across all history (no date window), plus all-time records.
`template_id` is `exercises[].template_id` from `/api/strength` (e.g. `D04AC939` is Squat
(Barbell)). Same authentication as `/api/strength`. `400 {"error"}` when `template_id` is
missing, `404 {"error": "Unknown exercise template."}` when neither a template nor any logged
exercise has that id.

```json
{
  "template": {
    "id": "D04AC939",
    "title": "Squat (Barbell)",
    "type": "weight_reps",
    "primary_muscle_group": "quadriceps",
    "secondary_muscle_groups": ["hamstrings", "glutes"],
    "equipment": "barbell",
    "thumbnail_url": "https://pump-app.s3.eu-west-2.amazonaws.com/exercise-thumbnails/00431201-Barbell-Full-Squat_Thighs_thumbnail@3x.jpg"
  },
  "weight_unit": "lb",
  "sessions": [
    {
      "workout_id": "b23372e6-4ee5-43e2-854f-f2f46d902bda",
      "date": "2026-10-20",
      "title": "Afternoon workout",
      "sets": 3,
      "reps": 24,
      "volume_kg": 2014.0,
      "best_set": {"weight_kg": 83.91468824559335, "reps": 8},
      "e1rm_kg": 106.29,
      "max_reps": 8,
      "max_duration_s": null
    }
  ],
  "records": {
    "heaviest_kg": {"value": 83.91468824559335, "date": "2026-05-03", "workout_id": "14a8b6d1-..."},
    "best_e1rm_kg": {"value": 106.29, "date": "2026-05-03", "workout_id": "14a8b6d1-..."},
    "most_reps": {"value": 10, "date": "2023-01-31", "workout_id": "eb1fc9d7-..."},
    "best_volume_kg": {"value": 2014.0, "date": "2026-05-03", "workout_id": "14a8b6d1-..."},
    "longest_duration_s": null
  }
}
```

### Fields

- `template`: the stored Hevy template. Fields are `null` (`[]` for
  `secondary_muscle_groups`) when unknown; `title` then falls back to the logged exercise name.
- `weight_unit`: as in `/api/strength`. All weights are kg.
- `sessions`: one per workout that contains the exercise, oldest first. If a workout lists the
  exercise more than once, its sets are combined. `date` is the local date, as in `/api/strength`;
  `title` is the workout's title. Only working sets (every type except `warmup`) count:
  - `sets`, `reps`, `volume_kg`: as for workouts in `/api/strength`.
  - `best_set`: the heaviest working set, ties going to more reps, or `null`.
  - `e1rm_kg`: the highest Epley estimate, `weight_kg × (1 + reps / 30)`, over working sets
    with 1 to 12 reps, rounded to 0.01, or `null`.
  - `max_reps`: the most reps in one working set, or `null`.
  - `max_duration_s`: the longest working set, or `null`.
- Exercise types: weights count only for `weight_reps` and `bodyweight_weighted`, so for other
  types (`reps_only`, `duration`, `bodyweight_assisted`, ...) `best_set` and `e1rm_kg` are `null`
  and their records are `null`. `reps_only` exercises use `max_reps`; `duration` exercises use
  `max_duration_s`.
- `records`: each `{"value", "date", "workout_id"}` from the first session that reached the
  best value, or `null` when no session has a positive value. `heaviest_kg` is the best
  `best_set.weight_kg`, `best_e1rm_kg` the best `e1rm_kg`, `most_reps` the best `max_reps`,
  `best_volume_kg` the best session `volume_kg` (weighted types only), and
  `longest_duration_s` the best `max_duration_s`.
