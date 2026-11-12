"""Check a drafted week against the numeric coaching rules.

plan    = {"week_start": "YYYY-MM-DD", "sessions": [
             {"date", "sport": run|swim|bike|gym, "minutes", "km"(runs),
              "intensity": easy|quality|strides, "kind": "long"|None,
              "slot": "midday"|None, "hard_minutes"(opt), "pace_target"(opt bool)}]}
history = {"weekly_run_km": [oldest..newest completed week], "longest_run_km_30d": float,
           "weeks_since_light": int, "last_quality_date": "YYYY-MM-DD"(opt)}
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

RULES_PATH = Path(__file__).resolve().parent / "rules.json"


def load_rules(path: str | Path | None = None) -> dict:
    return json.loads(Path(path or RULES_PATH).read_text(encoding="utf-8"))


def _d(value: str) -> date:
    return date.fromisoformat(value)


def validate_week(plan: dict, history: dict, rules: dict | None = None) -> dict:
    rules = rules or load_rules()
    errors: list[dict] = []
    warnings: list[dict] = []

    def err(rule, msg):
        errors.append({"rule": rule, "message": msg})

    def warn(rule, msg):
        warnings.append({"rule": rule, "message": msg})

    sessions = plan.get("sessions", [])
    start = _d(plan["week_start"])
    end = start + timedelta(days=6)
    for s in sessions:
        if not start <= _d(s["date"]) <= end:
            err("dates", f"{s['date']} {s['sport']} is outside the week {start}..{end}")

    runs = [s for s in sessions if s["sport"] == "run"]
    missing_km = [s["date"] for s in runs if s.get("km") is None]
    if missing_km:
        err("km", f"Runs without a km estimate: {', '.join(missing_km)}")
    planned_km = sum(s.get("km") or 0 for s in runs)

    # --- weekly run-km ramp ---------------------------------------------------
    weekly = history.get("weekly_run_km") or []
    prev = weekly[-1] if weekly else 0
    peak = max(weekly[-rules["ramp"]["peak_lookback_weeks"]:], default=0)
    ramp_pct = None
    r = rules["ramp"]
    if prev <= 0:
        warn("ramp", "No previous week with running; ramp cannot be checked. Be conservative.")
    else:
        ramp_pct = round((planned_km / prev - 1) * 100, 1)
        hard_cap = max(min(prev * (1 + r["rebuild_max_pct"] / 100), peak),
                       prev * (1 + r["above_peak_max_pct"] / 100))
        soft_cap = max(min(prev * (1 + r["rebuild_warn_pct"] / 100), peak),
                       prev * (1 + r["above_peak_max_pct"] / 100))
        if planned_km > hard_cap + 1e-9:
            err("ramp", f"{planned_km:.1f} km is {ramp_pct:+.0f}% vs last week ({prev:.1f} km); "
                        f"max allowed {hard_cap:.1f} km (recent peak {peak:.1f} km).")
        elif planned_km > soft_cap + 1e-9:
            warn("ramp", f"{planned_km:.1f} km is {ramp_pct:+.0f}% vs last week; "
                         f"above the comfortable {soft_cap:.1f} km. Choose the conservative option.")

    # --- lighter week -----------------------------------------------------------
    lw = rules["light_week"]
    since = history.get("weeks_since_light")
    rebuild_pct = lw.get("skip_while_below_peak_pct")
    rebuilding = bool(rebuild_pct and peak > 0 and planned_km < peak * rebuild_pct / 100)
    light_note = None
    if since is not None and prev > 0 and since >= lw["every_weeks"]:
        if planned_km > prev * (1 - lw["reduction_pct_min"] / 100):
            if rebuilding:
                light_note = (f"{since} weeks since a lighter week, but none needed yet: rebuilding at "
                              f"{planned_km / peak:.0%} of the {peak:.1f} km peak.")
            else:
                warn("light_week", f"{since} weeks since a lighter week; plan is not lighter. "
                                   "Needs the athlete's OK to skip.")

    # --- quality sessions ----------------------------------------------------------
    q = rules["quality"]
    quality = sorted(_d(s["date"]) for s in sessions if s.get("intensity") in ("quality", "race"))
    if len(quality) > q["max_per_week"]:
        err("quality", f"{len(quality)} quality sessions across all sports; max {q['max_per_week']}.")
    if not q["allow_back_to_back"]:
        dates = list(quality)
        if history.get("last_quality_date"):
            dates.insert(0, _d(history["last_quality_date"]))
        for a, b in zip(dates, dates[1:]):
            if (b - a).days <= 1:
                err("quality", f"Quality sessions on consecutive days ({a} and {b}).")

    # Hard swims (CSS sets, tests) are spaced from run quality: same day is fine (one hard
    # day), the day before or after makes two hard days in a row.
    run_quality = {_d(s["date"]) for s in sessions if s.get("intensity") in ("quality", "race")}
    for s in sessions:
        if s["sport"] == "swim" and s.get("hard"):
            day = _d(s["date"])
            near = [q for q in run_quality if abs((q - day).days) == 1]
            if near:
                warn("hard_days", f"Hard swim on {day} is next to run quality on {min(near)}; "
                                  "put it on the same day or with an easy day between.")

    # --- all-out races / time trials: 4+ weeks apart (same-day multi-event counts once) ---------
    gap = rules.get("all_out", {}).get("min_days_between", 28)
    all_out = {_d(s["date"]) for s in sessions if s.get("all_out") or s.get("intensity") == "race"}
    if history.get("last_all_out_date"):
        all_out.add(_d(history["last_all_out_date"]))
    ordered = sorted(all_out)
    for a, b in zip(ordered, ordered[1:]):
        if (b - a).days < gap:
            err("all_out", f"All-out efforts on {a} and {b} are {(b - a).days} days apart; need {gap}+ days.")

    # --- easy share (80/20 by time) -----------------------------------------------------
    total = sum(s["minutes"] for s in sessions)
    hard = sum(s.get("hard_minutes",
                     s["minutes"] * q["default_hard_fraction"] if s.get("intensity") in ("quality", "race") else 0)
               for s in sessions)
    easy_share = round(1 - hard / total, 3) if total else None
    if easy_share is not None and easy_share < rules["polarization"]["min_easy_share"]:
        warn("easy_share", f"Only {easy_share:.0%} of time is easy; target {rules['polarization']['min_easy_share']:.0%}.")

    # --- long run -------------------------------------------------------------------------
    lr = rules["long_run"]
    longs = [s for s in runs if s.get("kind") == "long" and s.get("km")]
    if not longs:
        warn("long_run", "No long run planned.")
    for s in longs:
        if planned_km and s["km"] > lr["max_share_of_weekly_km"] * planned_km + 1e-9:
            err("long_run", f"Long run {s['km']:.1f} km is {s['km'] / planned_km:.0%} of the week; "
                            f"max {lr['max_share_of_weekly_km']:.0%}.")
        longest = history.get("longest_run_km_30d")
        if longest is not None and s["km"] > longest + lr["max_over_longest_30d_km"] + 1e-9:
            err("long_run", f"Long run {s['km']:.1f} km is more than {lr['max_over_longest_30d_km']:.0f} km "
                            f"longer than the longest run in 30 days ({longest:.1f} km).")

    # --- swims and easy-run targets ---------------------------------------------------------
    for s in sessions:
        if s["sport"] == "swim" and s.get("slot") == "midday" and s["minutes"] > rules["swim"]["max_midday_minutes"]:
            err("swim", f"Midday swim on {s['date']} is {s['minutes']} min; max {rules['swim']['max_midday_minutes']}.")
        if s["sport"] == "run" and s.get("intensity") == "easy" and s.get("pace_target"):
            err("easy_target", f"Easy run on {s['date']} has a pace target; use an HR range only.")

    # --- default weekly structure (changing it needs the athlete's OK) ---------------------------
    st = rules["structure"]
    count = lambda sport: sum(1 for s in sessions if s["sport"] == sport)
    if count("run") != st["runs"]:
        warn("structure", f"{count('run')} runs planned; default is {st['runs']}. Ask before changing.")
    if count("swim") != st["swims"]:
        warn("structure", f"{count('swim')} swims planned; default is {st['swims']}. Ask before changing.")
    gym, bike = count("gym"), count("bike")
    if bike < st["bikes_min"]:
        warn("structure", "No bike session planned.")
    if gym < st.get("gym_min", 0):
        warn("structure", f"{gym} gym sessions; at least {st['gym_min']}.")

    # Optional flexible slot(s), e.g. one extra gym or bike session when the athlete feels fresh.
    flex = st.get("flex") or {}
    flex_sports = flex.get("sports") or []
    extra = {"gym": max(0, gym - st["gym_max"]), "bike": max(0, bike - st["bikes_min"])}
    if extra["gym"] and "gym" not in flex_sports:
        warn("structure", f"{gym} gym sessions; default max {st['gym_max']}.")
    flex_summary = None
    if flex:
        used = {sport: extra[sport] for sport in flex_sports if sport in extra and extra[sport]}
        slots = flex.get("count", 1)
        phase = history.get("season_phase")
        prefer = flex.get("prefer") or {}
        preferred = prefer.get(phase) or prefer.get("default") if isinstance(prefer, dict) else prefer
        flex_summary = {"slots": slots, "used": sum(used.values()), "sports_used": used,
                        "sports": flex_sports, "optional": flex.get("optional", True),
                        "phase": phase, "preferred": preferred}
        if sum(used.values()) > slots:
            warn("structure", f"{sum(used.values())} flexible {'/'.join(flex_sports)} sessions; at most {slots}.")
        elif not used and not flex.get("optional", True):
            warn("structure", f"The flexible {'/'.join(flex_sports)} slot is not planned.")
        if preferred and used and preferred not in used:
            flex_summary["note"] = (f"The flexible slot is {preferred} in the {phase or 'current'} phase; "
                                    f"this week uses {', '.join(used)}. Fine if the athlete chose it.")
        elif not used:
            flex_summary["note"] = (f"Flexible slot not used. Offer a {preferred or '/'.join(flex_sports)} "
                                    "session; the athlete decides based on how they feel.")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "planned_run_km": round(planned_km, 1), "prev_run_km": prev, "peak_run_km": peak,
            "ramp_pct": ramp_pct, "quality_sessions": len(quality), "easy_share": easy_share,
            "total_minutes": total, "rebuilding": rebuilding,
            **({"flex": flex_summary} if flex_summary else {}),
            **({"light_week": light_note} if light_note else {}),
        },
    }
