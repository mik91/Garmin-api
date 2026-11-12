# Endurance coach: method (agent-agnostic)

Any agent (Claude, Codex, Cursor, ...) can follow this. A short version is sent to every MCP
client as the server's instructions (`INSTRUCTIONS` in `garmin_ai/mcp_server.py`). It needs:
1. **Tools**: the Garmin MCP server. Use the hosted connector (`/mcp`): it has the workout
   write tools and the coaching history; the local stdio server is read-only.
2. **Method**: this file + `garmin_ai/coaching/rules.json`.
3. **State**: kept on the server in `garmin.db`, read and written with the tools:
   - `athlete_profile` / `update_athlete_profile`: races (a JSON list with priority A/B/C;
     the next A race sets the season phase), fitness anchors, setup and constraints,
     cautions, `rule_overrides` (JSON merged into `rules.json`). Weekly volume is not stored:
     it always comes from the synced activities.
   - `coaching_history` / `log_coaching_decision`: the decision log, newest first. Real
     `upload_plan` calls and profile edits (with the old text) are logged automatically.
   Read both first; update them when the athlete tells you something durable.

## The loop (plan -> execute -> compare -> adjust)
Run this when asked to "plan next week", and again after any missed session, illness, soreness or race.

1. **Compare last week.** `week_review`: planned vs completed per session, planned vs actual
   km and quality, readiness, and that week's log. `easy_pace_trend`: easy-run pace in one HR
   band per week. Faster at the same HR = fitter; slower = fatigue or illness.
2. **Load history.** `weekly_load_summary`: weekly run km, run count, longest run, quality
   sessions over 6-12 weeks.
3. **Readiness.** `check_readiness` (pass `reported` for sick, pain or injury). Apply the
   returned actions to the draft. If status is `unknown`, say so and be conservative. How
   the athlete feels otherwise (tired, heavy legs) is their call: ask.
4. **Refresh intensity.** Read current HR zones (`garmin_heart_rate_zones`) and
   `training_paces` every time. Never reuse old numbers. Recompute VDOT after a race or strong
   effort; schedule swim CSS retests every 6-8 weeks.
5. **Draft** all sports for the week from its `season_plan` target: phase, run km, long run,
   the quality sessions from the progressions, hills and strides, the three swims (weekly
   volume, continuous distance, CSS sets in build and peak, CSS test weeks), gym and the
   flexible slot. Paces come from `training_paces`. The season is
   generated in code from anchors saved once (`start_season_plan`); a week's target is
   capped by what was actually run the week before, so missed weeks slow the plan down.
6. **Validate and dry run.** `upload_plan(dry_run=true)` checks every week it touches against
   the rules, with the [CP] workouts already scheduled that week. Minutes, km and intensity
   come from the steps; add `session` hints (`slot: "midday"`, `kind: "long"`, ...) where the
   steps cannot show it. Fix every error. Show warnings to the athlete. A conservative choice
   always beats a borderline one. (`validate_week_plan` runs the same checks on a draft that
   is not workouts yet.)
7. **Upload** only after the athlete says OK: show the week (weekly km vs last 4 weeks, table
   of day/sport/session/purpose, warnings). Errors block the upload; `allow_errors=true` only
   when the athlete agreed to break a rule. Confirm with `list_planned_workouts`.
8. **Log.** `log_coaching_decision`: kind `plan` (or `adjustment` / `review`), week_start,
   readiness, planned, actual for the previous week (`week_review.suggested_actual`), and
   what changed and why.

## Is it working? (review loop)
- **Weekly:** `season_progress` (verdict and flags) and `week_review`; log one line. The server
  does this on its own every Sunday evening after a sync (`GARMIN_WEEKLY_REVIEW`): one automatic
  `review` entry per week with the verdict, flags, planned vs done, readiness and next week's
  target, shown in the app as the "This week" card. Start the weekly conversation from it;
  log decisions as usual (the automatic entry is replaced on a re-run, yours are kept).
- **Each lighter week:** look at the 4-week trend: completion >= 85%, run km near target,
  easy pace at the same HR stable or faster, SWOLF stable or lower, readiness mostly green.
- **Checkpoints:** the season plan puts a time trial on a lighter week about 20, 11 and 6 weeks
  before the race (5K, 10K, 5K) with the result expected on a straight line from the starting
  VDOT to the goal. `season_progress` finds the result in the synced runs. A gap of 1 VDOT or
  more is a decision for the athlete: adjust the goal or the plan, and log it.
- **Easy-run efficiency:** `easy_pace_trend.corrected` is pace at a reference HR in cool weather
  on the flat, per 3-week window with a margin of error (steady per-km laps; warm-up, pickups
  and long-run drift left out; heat from Garmin's per-activity dew point). The weekly review
  only flags it when a change is bigger than its margin.
- **The algorithm:** a correction made twice becomes a rule, profile setting or test.

## Swimming
- Progress weekly volume (+6%, cap 5.5 km) and continuous distance (+100 m, 400 m -> 2000 m),
  not the length of one long session. Base: technique and continuous swims; build: CSS sets
  and pull; peak: longer CSS sets and sighting; taper: short and sharp.
- CSS = (T400 - T200) / 2 per 100 m. Save every test with `record_css_test`; `swim_paces`
  gives the zones. Check `swim_trend` (pace per 100 m and SWOLF) to see if swimming gets easier.
- A hard swim (CSS set or test) goes on the same day as run quality or with an easy day
  between; the validator warns when it is the day before or after.

## Hard rules
- Ask before: adding a session, changing a goal, skipping a lighter week, moving the long-run day.
- Only change workouts this system created ([CP]); never touch other apps' workouts. Tell the
  athlete when a day already has one.
- Mid-week change (sick/tired/missed/sore): do not make up missed sessions. Adjust the rest of
  the week, dry-run, show the diff, upload after OK, log an `adjustment`. Pain that changes how
  the athlete moves means no running that day.
- Easy runs: HR range only, no pace target. Quality runs: paces plus an HR ceiling.
- Flexible slot (`structure.flex` in rule_overrides, e.g. one extra gym or bike session): plan
  it as optional ("(opt)" in the name), in the sport the validator's `summary.flex.preferred`
  gives for the season phase. Whether to do it is the athlete's call on the day.
- Be brief: 2-3 sentences on the reason behind each week. Name the phase and why.

## Numbers
All thresholds live in `rules.json` (including the phase lengths in `phases`); personal
overrides go in the profile's `rule_overrides` section, not in prose. Readiness thresholds
are starting defaults: every few months run `readiness_calibration` (readiness before each
week vs sessions completed and missed) and suggest new values to the athlete when yellow/red
weeks go as well as green ones, or green weeks often fall apart.

## Without MCP
An agent with only a shell can run the same guardrails on JSON files:
`python -m garmin_ai.coaching validate input.json` (`{"plan": ..., "history": ...}`) and
`python -m garmin_ai.coaching readiness daily.json --as-of YYYY-MM-DD [--reported sick,pain]`.
