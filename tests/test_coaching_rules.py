import copy
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from garmin_ai.coaching import assess_readiness, validate_week

START = "2026-11-16"  # a Monday


def day(i):
    return (date.fromisoformat(START) + timedelta(days=i)).isoformat()


def good_week():
    return {"week_start": START, "sessions": [
        {"date": day(0), "sport": "run", "minutes": 40, "km": 6.0, "intensity": "easy"},
        {"date": day(1), "sport": "run", "minutes": 45, "km": 6.5, "intensity": "quality", "hard_minutes": 15},
        {"date": day(2), "sport": "swim", "minutes": 22, "slot": "midday", "intensity": "easy"},
        {"date": day(3), "sport": "run", "minutes": 40, "km": 6.0, "intensity": "easy"},
        {"date": day(4), "sport": "run", "minutes": 35, "km": 5.5, "intensity": "easy"},
        {"date": day(4), "sport": "swim", "minutes": 22, "intensity": "easy"},
        {"date": day(5), "sport": "run", "minutes": 75, "km": 10.0, "kind": "long", "intensity": "easy"},
        {"date": day(5), "sport": "swim", "minutes": 55, "intensity": "easy"},
        {"date": day(6), "sport": "bike", "minutes": 45, "intensity": "easy"},
        {"date": day(2), "sport": "gym", "minutes": 40, "intensity": "easy"},
    ]}  # 34.0 km


HISTORY = {"weekly_run_km": [11, 17, 31, 27, 31], "longest_run_km_30d": 10.0, "weeks_since_light": 1}


def rules_of(result):
    return {e["rule"] for e in result["errors"]}


def test_good_week_passes():
    r = validate_week(good_week(), HISTORY)
    assert r["ok"], r
    assert r["summary"]["planned_run_km"] == 34.0


def test_ramp_too_steep():
    w = good_week()
    w["sessions"][0]["km"] = 14  # +8.2 km -> 42.2 km vs 31 (+36%)
    assert "ramp" in rules_of(validate_week(w, HISTORY))


def test_ramp_allows_rebuild_toward_recent_peak():
    # last week 31 km, peak 40 km: +19% (37 km) is a warning, not an error
    h = {**HISTORY, "weekly_run_km": [11, 17, 40, 36, 31]}
    w = good_week()
    w["sessions"][0]["km"] = 8.0  # 8.0+6.5+6.0+5.5+10.0 = 36.0 -> bump below
    w["sessions"][3]["km"] = 7.0  # = 37.0 km
    r = validate_week(w, h)
    assert "ramp" not in rules_of(r), r
    assert r["summary"]["planned_run_km"] == 37.0


def test_ramp_above_peak_is_capped_at_10_pct():
    h = {**HISTORY, "weekly_run_km": [40, 45, 50, 52, 53]}
    w = good_week()
    for s in w["sessions"]:
        if s["sport"] == "run":
            s["km"] *= 53 * 1.2 / 34  # ~63.6 km, +20% above the 53 km peak
    r = validate_week(w, {**h, "longest_run_km_30d": 30})
    assert "ramp" in rules_of(r)


def test_back_to_back_and_too_many_quality():
    w = good_week()
    w["sessions"][2].update(intensity="quality")  # swim quality on day 2, run quality day 1
    assert "quality" in rules_of(validate_week(w, HISTORY))


def test_strides_do_not_count_as_quality():
    w = good_week()
    w["sessions"][3]["intensity"] = "strides"
    w["sessions"][4]["intensity"] = "strides"
    assert "quality" not in rules_of(validate_week(w, HISTORY))


def test_long_run_caps():
    w = good_week()
    w["sessions"][6]["km"] = 13.5  # >2 km over the 10 km longest and ~38% of week
    assert "long_run" in rules_of(validate_week(w, HISTORY))


def test_midday_swim_limit():
    w = good_week()
    w["sessions"][2]["minutes"] = 40
    assert "swim" in rules_of(validate_week(w, HISTORY))


def test_easy_pace_target_rejected():
    w = good_week()
    w["sessions"][0]["pace_target"] = True
    assert "easy_target" in rules_of(validate_week(w, HISTORY))


def test_missing_light_week_warns():
    r = validate_week(good_week(), {**HISTORY, "weeks_since_light": 4})
    assert any(x["rule"] == "light_week" for x in r["warnings"])


def test_session_outside_week_errors():
    w = good_week()
    w["sessions"][0]["date"] = "2026-12-30"
    assert "dates" in rules_of(validate_week(w, HISTORY))


# ---- readiness -----------------------------------------------------------------
def daily(hrv_recent, hrv_base, rhr_recent, rhr_base, sleep_h, readiness=70, end="2026-11-16"):
    e = date.fromisoformat(end)
    rows = []
    for i in range(28):
        d = e - timedelta(days=i)
        recent = i < 7
        rows.append({"date": d.isoformat(),
                     "hrv_overnight": hrv_recent if recent else hrv_base,
                     "resting_hr": rhr_recent if recent else rhr_base,
                     "total_sleep_s": sleep_h * 3600 if i < 3 else 7.5 * 3600,
                     "training_readiness": readiness if i == 0 else None})
    return rows


def test_readiness_green():
    r = assess_readiness(daily(60, 60, 50, 50, 7.5), "2026-11-16")
    assert r["status"] == "green"


def test_readiness_yellow_and_red():
    y = assess_readiness(daily(52, 60, 54, 50, 7.5), "2026-11-16")  # hrv -13%, rhr +4 -> 2 pts
    assert y["status"] == "yellow"
    red = assess_readiness(daily(45, 60, 58, 50, 5.0, readiness=15), "2026-11-16")
    assert red["status"] == "red"


def test_reported_illness_forces_red():
    r = assess_readiness(daily(60, 60, 50, 50, 7.5), "2026-11-16", reported=["Sick"])
    assert r["status"] == "red"


def test_unknown_without_data():
    assert assess_readiness([], "2026-11-16")["status"] == "unknown"


def test_cli_roundtrip(tmp_path):
    f = tmp_path / "in.json"
    f.write_text(json.dumps({"plan": good_week(), "history": HISTORY}))
    root = Path(__file__).resolve().parent.parent
    p = subprocess.run([sys.executable, "-m", "garmin_ai.coaching", "validate", str(f)],
                       cwd=root, capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr


def test_all_out_efforts_need_four_weeks():
    w = good_week()
    w["sessions"][1].update(all_out=True)  # time trial on day 1
    ok = validate_week(w, {**HISTORY, "last_all_out_date": "2026-10-11"})  # 36 days earlier
    assert "all_out" not in rules_of(ok), ok
    bad = validate_week(w, {**HISTORY, "last_all_out_date": "2026-11-01"})  # 16 days earlier
    assert "all_out" in rules_of(bad)


def test_same_day_multi_event_counts_once():
    w = good_week()
    for i in (1, 3):  # two segments of one race day would share a date; use same date
        w["sessions"][i].update(all_out=True, date=day(1))
    r = validate_week(w, HISTORY)
    assert "all_out" not in rules_of(r), r


def test_low_weeks_after_a_race_all_count_as_lighter():
    from garmin_ai.coaching.history import weeks_since_light
    # race week 33 km, then 11 and 17 km (both lighter than the 3 weeks before), then build
    assert weeks_since_light([44, 39, 33, 11, 17, 31, 27, 33]) == 4
    assert weeks_since_light([30, 30, 30, 30]) == 4  # never lighter: all weeks
    assert weeks_since_light([40, 30]) == 1


def test_no_lighter_week_needed_while_rebuilding_below_peak():
    w = good_week()  # 34 km
    rebuilding = validate_week(w, {**HISTORY, "weekly_run_km": [53, 50, 31], "weeks_since_light": 5})
    assert not any(x["rule"] == "light_week" for x in rebuilding["warnings"])
    assert rebuilding["summary"]["rebuilding"] is True
    assert "none needed yet" in rebuilding["summary"]["light_week"]
    at_peak = validate_week(w, {**HISTORY, "weekly_run_km": [36, 35, 31], "weeks_since_light": 5})
    assert any(x["rule"] == "light_week" for x in at_peak["warnings"])


def _flex_rules():
    from garmin_ai.coaching import load_rules
    from garmin_ai.coaching.history import _merge
    return _merge(load_rules(), {"structure": {"gym_min": 1, "gym_max": 1, "bikes_min": 0, "flex": {
        "count": 1, "sports": ["gym", "bike"], "optional": True,
        "prefer": {"default": "gym", "build": "bike", "peak": "bike"}}}})


def _structure_warnings(result):
    return [x["message"] for x in result["warnings"] if x["rule"] == "structure"]


def test_flex_slot_gym_or_bike():
    rules = _flex_rules()
    base = {**HISTORY, "season_phase": "base"}
    w = good_week()  # 1 gym + 1 bike: the bike is the flex session
    r = validate_week(w, base, rules)
    assert _structure_warnings(r) == []
    assert r["summary"]["flex"]["sports_used"] == {"bike": 1}
    assert "is gym in the base phase" in r["summary"]["flex"]["note"]

    w["sessions"][-2]["sport"] = "gym"  # the bike becomes a second gym session
    r = validate_week(w, base, rules)
    assert _structure_warnings(r) == []
    assert r["summary"]["flex"]["sports_used"] == {"gym": 1}
    assert "note" not in r["summary"]["flex"]
    assert validate_week(w, {**HISTORY, "season_phase": "build"}, rules)["summary"]["flex"]["preferred"] == "bike"

    w["sessions"].append({"date": day(6), "sport": "bike", "minutes": 30, "intensity": "easy"})
    assert any("at most 1" in m for m in _structure_warnings(validate_week(w, base, rules)))


def test_flex_slot_is_optional_but_gym_min_is_not():
    rules = _flex_rules()
    w = good_week()
    w["sessions"] = [s for s in w["sessions"] if s["sport"] not in ("bike",)]
    r = validate_week(w, HISTORY, rules)
    assert _structure_warnings(r) == []  # no flex session: the athlete's call
    assert "athlete decides" in r["summary"]["flex"]["note"]
    w["sessions"] = [s for s in w["sessions"] if s["sport"] != "gym"]
    assert any("at least 1" in m for m in _structure_warnings(validate_week(w, HISTORY, rules)))


def test_default_rules_unchanged_without_flex():
    w = good_week()
    w["sessions"][-2]["sport"] = "gym"  # 2 gym, no bike
    messages = _structure_warnings(validate_week(w, HISTORY))
    assert "No bike session planned." in messages
    assert any("2 gym sessions; default max 1" in m for m in messages)
