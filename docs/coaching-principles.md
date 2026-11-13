# Coaching principles: where each rule comes from

This is the "why" behind the coach. [coaching-method.md](coaching-method.md) says *what* to do;
this file says which training approach each part follows, how it compares with established
methods, where it deliberately differs, and what is still missing. Numbers live in
`garmin_ai/coaching/rules.json`; code references are given per section.

Evidence labels: **strong** (consistent research and coaching consensus), **moderate** (some
research, wide coaching use), **heuristic** (coaching rule of thumb, little direct evidence).

## 1. The overall model

| Choice | Follows | Evidence |
| --- | --- | --- |
| Mostly easy running, a little hard (target 80% of time easy) | Polarized / 80-20 training (Seiler; Fitzgerald, *80/20 Running*) | strong |
| Season in phases: base, build, peak, taper | Classic periodization (Lydiard; Daniels' four phases; Friel for triathlon) | moderate |
| Intensities from one fitness number (VDOT) | Daniels, *Daniels' Running Formula* | moderate |
| Plan, do, compare, adjust each week | Adaptive plans (Hudson, *Run Faster*; app plans that react to missed sessions) | heuristic |
| Rules in code, judgment in the conversation | Our own design choice: the plan does not depend on which model is coaching | n/a |

**Why code instead of prose:** a model can be inconsistent from one week to the next. Anything with a
number (ramp, limits, progressions, phase lengths) is computed, so two models produce the same
season and the same checks. The model drafts the details and talks with the athlete.

## 2. Volume and injury risk (`season.py`, `validator.py`)

| Rule | Ours | Established practice | Notes |
| --- | --- | --- | --- |
| Weekly ramp | +10%/week while rebuilding below the recent peak (validator allows up to +20% there), +5% near the peak, hard cap +10% above the 12-week peak | The "10% rule" (popular), or "return to previous volume faster than you build new volume" (Pfitzinger, Daniels) | The 10% rule is **heuristic**; the rebuild-vs-new-volume distinction is closer to how coaches work in practice. |
| Lighter weeks | Every 4th week once near the peak, about 20% less; none needed while rebuilding below 80% of the peak | 3:1 or 2:1 loading/cutback cycles (common in Friel, Pfitzinger plans) | Skipping cutbacks while rebuilding is a **deliberate difference**: volume is already below what the athlete handled recently. |
| What counts as lighter | 15% below the highest of the 3 weeks before | Usually the previous week only | Ours counts every low week after a race or illness, not just the first one. |
| Long run | <= 28-30% of weekly km, +1.5 km/week at most, <= longest run in 30 days + 2 km, cap by distance (10K: 18 km) | Pfitzinger 5K-10K plans: long runs up to ~20-24 km; Daniels: <= 25-30% of weekly volume or 2.5 h | Our 10K long run is on the **conservative** side; see gaps. |
| All-out efforts | >= 28 days between races or time trials; same-day multi-event races count once | Common recovery guidance (roughly a day per km raced for hard efforts, plus an easy week after) | **heuristic**. |
| Acute vs chronic load | Not used as a rule (only weekly ramp) | Acute:chronic workload ratio (Gabbett) | ACWR is **debated** in the research; we may add it as a warning, not a rule. |

## 3. Intensity and quality sessions (`sessions.py`)

- **Paces from VDOT** (Daniels): E (easy), M (marathon), T (threshold, about 1-hour race pace),
  I (VO2max, about 3K-5K race pace), R (repetition), plus goal race pace. Paces are always taken
  fresh from the latest fitness estimate, never stored in the plan.
- **Session limits per week** (Daniels): T <= ~10% of weekly km, I <= ~8%. Ours caps each session's
  dose by these shares of that week's volume. Daniels also caps R at ~5%; we use R only as short
  200 m reps and strides.
- **What each phase emphasises**:
  - **base:** threshold + hills/strides, like Lydiard's base and hill phase, or Daniels' Phase I-II.
  - **build:** threshold + VO2, like Daniels' Phase III or Pfitzinger's LT + VO2 blocks.
  - **peak:** goal-pace work. This follows the "specific endurance" idea (Canova, as popularised in
    Hudson's and Magness's writing): extend the time spent at race pace as the race gets closer.
  - **taper:** short and sharp.
- **Progression = dose, variety = format.** The *dose* (minutes at T, minutes at I, km at goal pace)
  progresses; the *format* rotates (cruise intervals, threshold pyramids, progression runs, tempo
  fartlek; 800s, 1 km reps, VO2 pyramids, hill repeats; goal-pace blocks, alternations, cutdowns).
  Coaches and plan apps rotate formats for engagement; keeping the dose as the thing that
  progresses is what makes the variety safe. **moderate**.
- **Lighter weeks** keep one quality session at ~80% of the dose (the progression does not
  advance). That matches common cutback practice: keep some intensity, cut volume.

## 4. Easy running: time, heart rate and efficiency (`efficiency.py`, `review.py`)

- **Easy and long runs are written in minutes with an HR cap, not in km.** Training stress follows
  time on feet, a km target tempts you to speed up on bad days, and the same session adapts as you
  get fitter. Lydiard and 80/20 plans also use time-based easy running. The validator converts
  minutes to km with the athlete's own recent easy pace, so volume checks stay realistic.
- **No pace targets on easy runs** (only an HR range). This keeps easy days easy, which is the
  most common mistake polarized training warns about.
- **Aerobic efficiency:** pace at a reference HR, fitted on steady laps and corrected for speed,
  hills (grade-adjusted) and heat (dew point). Like Maffetone's MAF test, but with corrections and a
  margin of error, so day-to-day noise and heat are not read as fitness changes. **moderate**: the
  HR-pace relationship is well known, and the corrections make it fairer.

## 5. Readiness and recovery (`readiness.py`)

- **HRV:** the 7-day median vs the prior 21 days, with a drop of 10% = yellow and 20% = red. This follows
  HRV-guided training research (Kiviniemi; Plews and Laursen), which uses rolling averages against a
  baseline rather than single mornings. **moderate**.
- **Resting HR** rise (+4 / +7 bpm), **sleep** in the last 3 nights, and Garmin's own training readiness
  each add points. Fixed actions per status (yellow: downgrade the next quality session, ~10% less
  volume; red: no quality, 20-30% less).
- **What the athlete reports overrides the data both ways.** Sick, pain or injury force red; feeling
  tired is the athlete's call. Coaches generally trust subjective wellness at least as much as HRV.
- **The thresholds are tuned to the athlete:** `readiness_calibration` compares past readiness with how
  each week actually went.

## 6. Tapering and racing (`season.py`)

- **Taper:** one week for 5K/10K, two for a half, three for a marathon, plus a race week. Volume drops
  to ~75% and then ~55% while intensity stays (short goal-pace reps). This matches taper research
  (Mujika and Padilla; Bosquet's meta-analysis: cut volume 40-60%, keep intensity and frequency, 1-3
  weeks). **strong**.
- **Multi-event race days** (e.g. 2K + 5K + 10K): the earlier events are run controlled (steady, about
  marathon pace or slower) to protect the goal race. There are two **rehearsal days** in the peak phase
  (a steady morning run, then goal-pace work in the evening). This is our own addition, but it applies
  the specificity principle.

## 7. Measuring progress (`progress.py`)

- **Checkpoint time trials** at about 20, 11 and 6 weeks before the race (5K, 10K, 5K), on lighter
  weeks, with the expected result on a straight line from the starting VDOT to the goal VDOT. Plans
  commonly use tune-up races or time trials (Daniels, Pfitzinger) to re-set paces. The straight
  line is a **simplification**: real gains are usually faster early and slower late, so a small
  gap at the first checkpoint is not alarming.
- **Weekly verdict** (on track / watch / act) from volume vs target, sessions completed, fitness vs
  the line, efficiency, swim trend, CSS age and readiness. This is our own tooling, playing the role
  of a coach's weekly check-in.

## 8. Swimming (`swim.py`)

- **CSS (critical swim speed)** from a 400 m + 200 m test: (T400 - T200) / 2 per 100 m. This comes
  from the critical speed concept (Wakayoshi et al.), as used by Swim Smooth and most triathlon
  coaching. Zones are set as offsets from CSS. **moderate to strong**.
- **For a developing adult swimmer, technique comes first** (drills, kick, catch-up). This is the
  consensus of Swim Smooth and Total Immersion: efficiency gains beat fitness gains early on.
- **What progresses:** continuous distance (400 m to 2000 m, +100 m per normal week) and weekly
  volume (+6%), not the length of one long session. Being able to swim race distance nonstop and
  relaxed is the triathlon goal. **heuristic** (common in beginner triathlon plans).
- **Phases:** base is technique + continuous swimming; build is CSS sets + pull; peak is longer CSS
  sets + sighting; open water is planned in the triathlon block.
- **SWOLF and pace trend** track efficiency. SWOLF depends on pool length and stroke count habits, so
  it is only compared with the athlete's own history.
- **Hard swims** (CSS sets, tests) go on run-quality days or with an easy day between, never the day
  before or after. This is a hard-easy principle across sports (Friel).

## 9. Strength and cross-training

- **At least one gym session a week**, plus an optional flexible gym-or-bike slot chosen by phase.
  Running research supports strength work for economy and injury resilience (e.g. heavy and
  plyometric training studies). **moderate**. There is no progression model for strength yet.
- Bike is added in the build phase for the triathlon; in this plan it is aerobic support, not a
  separate progression.

## 10. Where we deliberately differ

- **No make-up sessions:** missed work is not made up; the next week's target is capped at last
  week's actual km + 10%. Many apps reschedule missed sessions; most coaches advise against it.
- **Conservative by default:** a borderline choice is resolved toward less. The checks exist to stop
  the "just one more km" drift that causes most overuse injuries.
- **The athlete decides on feelings, the data decides on limits:** fatigue and enjoyment are the
  athlete's call; ramp, spacing and dose limits are not negotiable without an explicit OK.

## 11. Known gaps (candidates to improve)

- **No medium-long run:** a midweek 60-75 min run (Pfitzinger) would raise aerobic volume without a longer
  Sunday run. It may be worth adding in build for 10K goals.
- **Long-run cap for 10K (18 km) and the 28% share** are cautious compared with Pfitzinger's 10K plans;
  they could rise once the athlete holds 55+ km weeks.
- **Paces don't change from session data on their own:** `session_review` repeats a step after a
  missed or poor session and flags a consistent pace bias, but the paces themselves still come
  from race predictions and checkpoints (deliberately: one good or bad day should not move them).
- **Strength has no progression model** (exercises, loads, phases).
- **ACWR or monotony/strain warnings** (Foster) could complement the weekly ramp.
- **Heat and altitude** adjust the efficiency model but not the plan itself (e.g. summer paces).
- **The triathlon block** (bike progression, brick runs, open water) is not generated yet.

## References (books and papers)

- Daniels, J. *Daniels' Running Formula*.
- Pfitzinger, P. and Latter, P. *Faster Road Racing*.
- Fitzgerald, M. *80/20 Running*; Seiler, S. work on polarized training distribution.
- Hudson, B. and Fitzgerald, M. *Run Faster from the 5K to the Marathon*; Magness, S. *The Science of Running*.
- Lydiard, A. and Gilmour, G. *Running with Lydiard*.
- Friel, J. *The Triathlete's Training Bible*.
- Mujika, I. and Padilla, S. on tapering; Bosquet, L. et al. (2007), taper meta-analysis.
- Plews, D., Laursen, P. et al. on HRV-guided training; Kiviniemi, A. et al. (2007).
- Wakayoshi, K. et al. (1992) on critical swimming speed; Swim Smooth on CSS training.
- Gabbett, T. (2016) on the acute:chronic workload ratio (and later critiques of it).
- Maffetone, P. on the MAF test.
