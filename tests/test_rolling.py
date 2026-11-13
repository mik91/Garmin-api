import json
from contextlib import closing
from datetime import date

from garmin_ai import db, mcp_server, web_server
from garmin_ai.coaching import history, rolling
from test_coach_review import Base
from test_season import ANCHOR, RACE

TODAY = date(2026, 11, 12)  # first open week: Monday 2026-11-16


class RollingBase(Base):
    def setUp(self):
        super().setUp()
        mcp_server.update_athlete_profile("races", json.dumps([RACE]))
        mcp_server.update_athlete_profile("season_plan", json.dumps(ANCHOR))

    def regenerate(self, **kwargs):
        with db.get_connection() as conn:
            return rolling.regenerate(conn, today=TODAY, **{"dry_run": False, **kwargs})

    def live(self):
        with closing(self.conn()) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM plan_drafts "
                                                  "WHERE status NOT IN ('superseded', 'rejected')")]



class RollingTests(RollingBase):
    def test_dry_run_stores_nothing(self):
        result = self.regenerate(dry_run=True)
        self.assertEqual(result["first_week"], "2026-11-16")
        self.assertEqual(result["weeks"][-1]["week_start"], "2027-05-24")
        self.assertGreater(result["counts"]["added"], 200)
        self.assertEqual(self.live(), [])

    def test_save_then_nothing_to_change(self):
        first = self.regenerate(vdot=50.4)
        rows = self.live()
        self.assertEqual(len(rows), first["counts"]["added"])
        self.assertTrue(all(r["status"] == "draft" and r["origin"] == "generator" for r in rows))
        self.assertEqual(json.loads(rows[0]["spec_json"])["sport"] in ("running", "lap_swimming", "cycling",
                                                                     "strength"), True)
        again = self.regenerate(vdot=50.4)
        self.assertEqual(again["counts"], {"added": 0, "changed": 0, "removed": 0})
        self.assertNotIn("version", again)
        with closing(self.conn()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM plan_versions").fetchone()[0], 1)
            logged = conn.execute("SELECT summary FROM coach_log WHERE kind = 'plan'").fetchall()
        self.assertEqual(len(logged), 1)
        self.assertIn("Rolling plan created (manual)", logged[0][0])

    def test_new_fitness_supersedes_changed_workouts(self):
        self.regenerate(vdot=50.4)
        before = len(self.live())
        result = self.regenerate(vdot=53, trigger="vdot")
        self.assertGreater(result["counts"]["changed"], 0)
        rows = self.live()
        self.assertEqual(len(rows), before)
        changed = [r for r in rows if r["parent_id"]]
        self.assertEqual(len(changed), result["counts"]["changed"])
        self.assertTrue(all(r["reason"] == "Regenerated (vdot)" for r in changed))
        with closing(self.conn()) as conn:
            old = conn.execute("SELECT status FROM plan_drafts WHERE id = ?", (changed[0]["parent_id"],)).fetchone()
        self.assertEqual(old["status"], "superseded")

    def test_published_and_proposed_weeks_are_left_alone(self):
        self.regenerate(vdot=50.4)
        with closing(self.conn()) as conn:
            conn.execute("UPDATE plan_drafts SET status = 'published' WHERE week_start = '2026-11-16'")
            conn.execute("UPDATE plan_drafts SET status = 'proposed' WHERE week_start = '2026-11-30' "
                         "AND kind = 'easy'")
            conn.commit()
        result = self.regenerate(vdot=53)
        self.assertEqual(result["first_week"], "2026-11-23")
        skipped = next(w for w in result["weeks"] if w["week_start"] == "2026-11-30")
        self.assertIn("proposed", skipped["skipped"])
        published = [r for r in self.live() if r["week_start"] == "2026-11-16"]
        self.assertTrue(published and all(r["status"] == "published" and not r["parent_id"] for r in published))

    def test_other_cp_workouts_on_garmin_push_the_first_week(self):
        with closing(self.conn()) as conn:
            conn.execute("INSERT INTO coach_workouts VALUES ('1', '[TP] Easy', 'running', 'h', '{}', 'x', 'x')")
            conn.execute("INSERT INTO coach_schedules VALUES ('s1', '1', '2026-11-25', 'x')")
            conn.commit()
            self.assertEqual(rolling.first_open_week(conn, TODAY).isoformat(), "2026-11-30")

    def test_schedule_from_the_profile(self):
        mcp_server.update_athlete_profile("plan_schedule", json.dumps({"long": "sat", "easy": ["sun", "wed", "mon"]}))
        self.regenerate(vdot=50.4)
        longs = {date.fromisoformat(r["date"]).weekday() for r in self.live() if r["kind"] == "long"}
        self.assertEqual(longs, {5})
        mcp_server.update_athlete_profile("plan_schedule", json.dumps({"long": "someday"}))
        with db.get_connection() as conn, self.assertRaises(history.HistoryError):
            rolling.schedule(conn)

    def test_read_tool_and_api(self):
        self.assertIn("No rolling plan yet", mcp_server.rolling_plan())
        self.regenerate(vdot=50.4)
        data = json.loads(mcp_server.rolling_plan(start="2026-11-16", weeks=2))
        self.assertEqual([w["week_start"] for w in data["weeks"]], ["2026-11-16", "2026-11-23"])
        week = data["weeks"][0]
        self.assertEqual(week["phase"], "base")
        self.assertAlmostEqual(week["planned_run_km"], week["target_run_km"], delta=1.0)
        item = next(i for i in week["items"] if i["kind"] == "long")
        self.assertEqual(item["status"], "draft")
        self.assertTrue(item["summary"] and item["minutes"] > 30 and item["spec"]["steps"])
        api = web_server._rolling_plan({"start": ["2026-11-16"], "weeks": ["1"]})
        self.assertEqual(len(api["weeks"]), 1)
        with self.assertRaises(ValueError):
            web_server._rolling_plan({"weeks": ["0"]})

    def test_regenerate_tool_defaults_to_dry_run(self):
        text = mcp_server.regenerate_plan()
        self.assertTrue(json.loads(text)["dry_run"])
        self.assertEqual(self.live(), [])
        self.assertTrue(mcp_server.regenerate_plan(trigger="whenever").startswith("Error: trigger"))


class RollForwardBase(RollingBase):
    """Week 1 (2026-11-16) is published, then reviewed on Sunday 2026-11-22."""

    def setUp(self):
        super().setUp()
        self.regenerate(vdot=50.4)
        with db.get_connection() as conn:
            rolling.publish(conn, "2026-11-16", dry_run=False, allow_errors=True)

    def runs(self, days, km=8):
        with closing(self.conn()) as conn:
            for i, day in enumerate(days):
                conn.execute("INSERT INTO activities(activity_id, start_time, type, distance_m, duration_s, raw_json) "
                             "VALUES(?, ?, 'running', ?, 2640, '{}')", (9000 + i, f"{day} 07:00", km * 1000))
            conn.commit()

    def roll(self, review=None, save=True, week="2026-11-23"):
        with db.get_connection() as conn:
            return rolling.roll_forward(conn, week, date(2026, 11, 22), review=review, save=save)

    def statuses(self, week):
        rows = [r for r in self.live() if r["week_start"] == week]
        return sorted(r["status"] for r in rows), rows


class RollForwardTests(RollForwardBase):
    def test_publish_marks_the_week_and_moves_the_first_open_week(self):
        status, rows = self.statuses("2026-11-16")
        self.assertEqual(set(status), {"published"})
        self.assertTrue(all(r["workout_id"] for r in rows))
        self.assertTrue(any(c[0] == "schedule" for c in self.api.calls))
        with closing(self.conn()) as conn:
            self.assertEqual(rolling.first_open_week(conn, TODAY).isoformat(), "2026-11-23")

    def test_publish_dry_run_and_rule_errors(self):
        with db.get_connection() as conn:
            dry = rolling.publish(conn, "2026-11-23")
            self.assertTrue(dry["dry_run"] and dry["workouts"] > 5 and dry["published"] == 0)
            self.assertNotIn("garmin_payload", dry["items"][0])
        self.assertEqual(set(self.statuses("2026-11-23")[0]), {"draft"})

    def test_missed_runs_cap_next_week_and_approve(self):
        self.runs(["2026-11-17", "2026-11-18"])  # 16 km of ~36 (Tue quality, Wed easy)
        result = self.roll()
        self.assertTrue(result["proposal"])
        self.assertIn("Capped at 19.2 km (a one-off short week", result["reason"])
        self.assertEqual(result["closed"]["done"], 2)
        self.assertGreater(result["closed"]["skipped"], 0)
        week = json.loads(mcp_server.rolling_plan(start="2026-11-23", weeks=1))["weeks"][0]
        self.assertTrue(week["proposal"])
        self.assertLessEqual(week["proposed_run_km"], 19.2)
        self.assertGreater(week["planned_run_km"], 30)
        self.assertIn("Approve or reject", mcp_server.publish_week("2026-11-23"))
        decided = json.loads(mcp_server.decide_proposal("2026-11-23", approve=True))
        self.assertTrue(decided["approved"])
        week = json.loads(mcp_server.rolling_plan(start="2026-11-23", weeks=1))["weeks"][0]
        self.assertFalse(week["proposal"])
        self.assertLessEqual(week["planned_run_km"], 19.2)
        origins = {i["origin"] for i in week["items"]}
        self.assertIn("review", origins)
        week1 = self.statuses("2026-11-16")[0]
        self.assertIn("done", week1)

    def test_red_readiness_swaps_quality_and_reject_keeps_the_drafts(self):
        self.runs(["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-21"])  # 40 km
        before = sorted(r["id"] for r in self.live() if r["week_start"] == "2026-11-23")
        result = self.roll({"card": {"readiness": {"end": "red"}}, "progress": {"flags": [
            {"level": "act", "message": "Easy pace slower at the same HR"}]}})
        self.assertIn("Readiness red", result["reason"])
        self.assertEqual(result["flags"][0]["level"], "act")
        proposed = [r for r in self.live() if r["status"] == "proposed"]
        self.assertTrue(proposed)
        self.assertFalse(any(r["kind"] in ("threshold", "vo2") for r in proposed))
        self.assertTrue(any(r["kind"] == "threshold" for r in self.live() if r["status"] == "proposed_drop"))
        rejected = json.loads(mcp_server.decide_proposal("2026-11-23", approve=False, note="feeling fine"))
        self.assertFalse(rejected["approved"])
        self.assertEqual(sorted(r["id"] for r in self.live() if r["week_start"] == "2026-11-23"), before)
        self.assertIn("No proposal", mcp_server.decide_proposal("2026-11-23", approve=True))

    def test_nothing_to_change_and_dry_run(self):
        self.runs(["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-21"])
        dry = self.roll({"card": {"readiness": {"end": "green"}}}, save=False)
        self.assertTrue(dry["dry_run"])
        self.assertFalse(dry["proposal"])
        self.assertEqual(set(self.statuses("2026-11-16")[0]), {"published"})  # dry run closes nothing
        with self.assertRaisesRegex(history.HistoryError, "already on Garmin"):
            self.roll(week="2026-11-16")

    def test_agent_proposal(self):
        with closing(self.conn()) as conn:
            rows = conn.execute("SELECT * FROM plan_drafts WHERE week_start = '2026-11-30' AND status = 'draft'"
                                ).fetchall()
        items = [{"date": r["date"], "workout": json.loads(r["spec_json"]), "session": json.loads(r["session_json"])}
                 for r in rows if r["kind"] != "bike"]
        dry = json.loads(mcp_server.propose_week("2026-11-30", items, "No bike this week: the bike is in repair."))
        self.assertTrue(dry["dry_run"])
        self.assertEqual(len(dry["removed"]), 1)
        self.assertIn("needs a date", mcp_server.propose_week("2026-11-30", [{"date": "2026-12-25"}], "x"))
        json.loads(mcp_server.propose_week("2026-11-30", items, "No bike this week: the bike is in repair.",
                                           dry_run=False, allow_errors=True))
        drops = [r for r in self.live() if r["status"] == "proposed_drop"]
        self.assertEqual([r["kind"] for r in drops], ["bike"])
        mcp_server.decide_proposal("2026-11-30", approve=True)
        self.assertFalse(any(r["kind"] == "bike" for r in self.live() if r["week_start"] == "2026-11-30"))

    def test_sunday_review_stores_a_proposal(self):
        from garmin_ai.coaching import weekly
        self.runs(["2026-11-17"])
        with db.get_connection() as conn:
            weekly.run(conn, "2026-11-16", today=date(2026, 11, 22))
        self.assertTrue(any(r["status"] == "proposed" and r["week_start"] == "2026-11-23" for r in self.live()))
        with closing(self.conn()) as conn:
            kinds = [r[0] for r in conn.execute("SELECT kind FROM coach_log ORDER BY id")]
        self.assertIn("adjustment", kinds)


class AutoPublishTests(RollForwardBase):
    """The Sunday job with the rolling.auto_publish policy, and the app endpoints."""

    def policy(self, mode):
        mcp_server.update_athlete_profile("rule_overrides", json.dumps({"rolling": {"auto_publish": mode}}))

    def sunday(self):
        from garmin_ai.coaching import weekly
        with db.get_connection() as conn:
            weekly.run(conn, "2026-11-16", today=date(2026, 11, 22))
        return sorted({r["status"] for r in self.live() if r["week_start"] == "2026-11-23"})

    def full_week(self):
        self.runs(["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-21"])
        with closing(self.conn()) as conn:
            conn.execute("INSERT INTO activities(activity_id, start_time, type, distance_m, duration_s, raw_json) "
                         "VALUES(9100, '2026-11-22 08:00', 'running', 11000, 3900, '{}')")
            conn.commit()

    def test_never_by_default(self):
        self.full_week()
        self.assertEqual(self.sunday(), ["draft"])

    def test_unchanged_week_is_published(self):
        self.policy("unchanged")
        self.full_week()
        self.assertEqual(self.sunday(), ["published"])

    def test_unchanged_policy_waits_for_a_proposal(self):
        self.policy("unchanged")
        self.runs(["2026-11-17", "2026-11-18"])
        self.assertIn("proposed", self.sunday())

    def test_minor_policy_waits_for_a_big_change_always_does_not(self):
        self.policy("minor")
        self.runs(["2026-11-17", "2026-11-18"])  # capped: long run and days change, not minor
        self.assertIn("proposed", self.sunday())
        self.policy("always")
        self.assertEqual(self.sunday(), ["published"])
        with closing(self.conn()) as conn:
            notes = [r[0] for r in conn.execute("SELECT summary FROM coach_log WHERE kind = 'adjustment'")]
        self.assertTrue(any("approved automatically (auto_publish always)" in n for n in notes))

    def test_rule_errors_are_never_published_automatically(self):
        self.policy("unchanged")
        self.runs(["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-21"])  # longest 8 km
        self.assertEqual(self.sunday(), ["draft"])
        with closing(self.conn()) as conn:
            notes = [r[0] for r in conn.execute("SELECT summary FROM coach_log WHERE kind = 'note'")]
        self.assertTrue(any("was not published automatically" in n and "Long run" in n for n in notes))

    def test_is_minor(self):
        easy = {"date": "2026-11-25", "kind": "easy", "name": "Easy run 5 km",
                "spec": {"sport": "running", "name": "e", "steps": [
                    {"type": "interval", "duration": {"distance_m": 5000}}]}, "session": {}}
        row = {"id": 1, "date": "2026-11-25", "kind": "easy", "name": "Easy run 5.2 km",
               "spec_json": json.dumps({"sport": "running", "name": "e", "steps": [
                   {"type": "interval", "duration": {"distance_m": 5200}}]})}
        diff = {"added": [], "changed": [(easy, row)], "removed": []}
        self.assertTrue(rolling.is_minor(diff, [row], [easy], 10))
        self.assertFalse(rolling.is_minor(diff, [row], [easy], 1))
        moved = {**easy, "date": "2026-11-26"}
        self.assertFalse(rolling.is_minor({"added": [], "changed": [(moved, row)], "removed": []}, [row], [moved], 10))
        self.assertFalse(rolling.is_minor({"added": [], "changed": [], "removed": [{**row, "kind": "threshold"}]},
                                          [row], [], 50))

    def test_app_endpoints(self):
        self.assertTrue({"/api/rolling-plan/approve", "/api/rolling-plan/reject",
                         "/api/rolling-plan/publish"} <= web_server.TOKEN_POST_PATHS)
        with self.assertRaisesRegex(ValueError, "week_start"):
            rolling.reject_request({})
        with self.assertRaisesRegex(ValueError, "No proposal"):
            rolling.approve_request({"week_start": "2026-11-23"})
        self.runs(["2026-11-17", "2026-11-18"])
        self.roll()
        with self.assertRaisesRegex(ValueError, "Approve or reject"):
            rolling.publish_request({"week_start": "2026-11-23"})
        rejected = rolling.reject_request({"week_start": "2026-11-25", "note": "I feel fine"})  # any day of the week
        self.assertFalse(rejected["approved"])
        self.roll()
        done = rolling.approve_request({"week_start": "2026-11-23", "publish": True})
        self.assertTrue(done["decision"]["approved"])
        self.assertEqual(done["publish"]["published"], done["publish"]["workouts"])
        self.assertEqual(sorted({r["status"] for r in self.live() if r["week_start"] == "2026-11-23"}),
                         ["published"])


class DriftTests(RollForwardBase):
    """Week 1 run at 16 of 36 km; week 2 capped (rejected, then approved), published, not run."""

    def setUp(self):
        super().setUp()
        self.runs(["2026-11-17", "2026-11-18"])
        self.roll()
        mcp_server.decide_proposal("2026-11-23", approve=False, note="I can run more")
        self.roll()
        mcp_server.decide_proposal("2026-11-23", approve=True)
        with db.get_connection() as conn:
            rolling.publish(conn, "2026-11-23", dry_run=False, allow_errors=True)
            rolling.close_days(conn, date(2026, 11, 30))
            self.report = rolling.drift(conn, "2026-11-16", "2026-11-23", today=date(2026, 11, 30))

    def test_weeks(self):
        one, two = self.report["weeks"]
        self.assertEqual(one["week_start"], "2026-11-16")
        self.assertAlmostEqual(one["first_draft_run_km"], 36.0, delta=0.5)
        self.assertEqual(one["final_run_km"], one["first_draft_run_km"])
        self.assertEqual((one["run_km"], one["done"]), (16.0, 2))
        self.assertGreater(one["skipped"], 3)
        self.assertEqual(two["proposals"], ["rejected", "approved"])
        self.assertEqual(two["proposal_rules"], ["cap"])
        self.assertGreater(two["first_draft_run_km"], 30)
        self.assertLessEqual(two["final_run_km"], 19.2)
        self.assertEqual(two["first_draft_quality"], two["final_quality"])
        self.assertIn("I can run more", two["rejected_note"])
        self.assertEqual(two["run_km"], 0)

    def test_rules_kinds_and_patterns(self):
        cap = self.report["rules"]["cap"]
        self.assertEqual((cap["proposals"], cap["approved"], cap["rejected"]), (2, 1, 1))
        self.assertIn("long", cap["kinds"])
        easy = self.report["kinds"]["easy"]
        self.assertGreater(easy["skipped"], easy["done"])
        areas = {p["area"] for p in self.report["patterns"]}
        self.assertIn("sessions", areas)
        self.assertIn("schedule", areas)
        self.assertTrue(all(p["setting"] for p in self.report["patterns"]))

    def test_tool_api_and_review_card(self):
        data = json.loads(mcp_server.plan_drift(start="2026-11-16", end="2026-11-23"))
        self.assertEqual(len(data["weeks"]), 2)
        api = web_server.GET_ACTIONS["/api/rolling-plan/drift"]({"start": ["2026-11-16"], "end": ["2026-11-23"]})
        self.assertEqual(api["start"], "2026-11-16")
        with self.assertRaises(ValueError):
            web_server.GET_ACTIONS["/api/rolling-plan/drift"]({"start": ["soon"]})
        self.assertTrue(mcp_server.plan_drift(start="2026-12-07", end="2026-11-16").startswith("Error"))
        from garmin_ai.coaching import weekly
        with db.get_connection() as conn:
            card = weekly.build(conn, "2026-11-23", today=date(2026, 11, 29))["card"]
        self.assertIn("plan_drift", card)
        self.assertTrue(card["plan_drift"])


class MidWeekTests(RollForwardBase):
    """Week 1 (2026-11-16) is on Garmin; on Wednesday 2026-11-18 the coach changes the rest of it."""

    WEDNESDAY = date(2026, 11, 18)

    def rest_of_week(self):
        with closing(self.conn()) as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM plan_drafts WHERE week_start = '2026-11-16' "
                                                  "AND status = 'published' AND date >= '2026-11-18'")]
        return rows, [{"date": r["date"], "workout": json.loads(r["spec_json"]),
                       "session": json.loads(r["session_json"])} for r in rows]

    def propose(self, items, **kwargs):
        with db.get_connection() as conn:
            return rolling.propose_week(conn, "2026-11-16", items, "Sore calf: no bike, shorter long run.",
                                        today=self.WEDNESDAY, **kwargs)

    def changed_items(self):
        rows, items = self.rest_of_week()
        keep = [i for r, i in zip(rows, items) if r["kind"] != "bike"]
        long = next(i for i in keep if i["workout"]["name"].startswith("Long run"))
        long["workout"]["steps"][0]["duration"]["distance_m"] -= 2000  # same name: updated in place
        return rows, keep

    def test_days_before_today_are_refused(self):
        _, items = self.rest_of_week()
        with self.assertRaisesRegex(history.HistoryError, "from today on"):
            self.propose(items + [{"date": "2026-11-17", "workout": items[0]["workout"]}])

    def test_approve_updates_garmin(self):
        rows, items = self.changed_items()
        bike = next(r for r in rows if r["kind"] == "bike")
        long = next(r for r in rows if r["kind"] == "long")
        dry = self.propose(items)
        self.assertTrue(dry["on_garmin"])
        self.assertEqual(dry["from"], "2026-11-18")
        self.assertEqual(len(dry["removed"]), 1)
        self.assertEqual(len(dry["changed"]), 1)
        self.propose(items, dry_run=False, allow_errors=True)
        with db.get_connection() as conn:
            result = rolling.decide(conn, "2026-11-16", True)
        self.assertEqual(result["garmin"]["deleted"], [f"{bike['date']} {bike['name']}"])
        self.assertEqual(result["garmin"]["errors"], [])
        self.assertIn(("delete", int(bike["workout_id"])), self.api.calls)
        self.assertIn(("put", int(long["workout_id"])), self.api.calls)
        week = [r for r in self.live() if r["week_start"] == "2026-11-16"]
        self.assertFalse(any(r["kind"] == "bike" for r in week))
        new_long = next(r for r in week if r["kind"] == "long")
        self.assertEqual((new_long["status"], new_long["workout_id"]), ("published", long["workout_id"]))
        self.assertEqual(new_long["origin"], "agent")
        self.assertTrue(all(r["status"] == "published" for r in week))  # Mon/Tue untouched

    def test_reject_puts_the_week_back_on_garmin(self):
        _, items = self.changed_items()
        self.propose(items, dry_run=False, allow_errors=True)
        with db.get_connection() as conn:
            rolling.decide(conn, "2026-11-16", False)
        week = [r for r in self.live() if r["week_start"] == "2026-11-16"]
        self.assertTrue(week and all(r["status"] == "published" for r in week))
        self.assertFalse(any(c[0] in ("delete", "put") for c in self.api.calls))


class CapFloorTests(RollForwardBase):
    def test_not_a_one_off_keeps_the_plain_cap(self):
        with closing(self.conn()) as conn:
            conn.execute("DELETE FROM activities WHERE substr(start_time, 1, 10) "
                         "BETWEEN '2026-11-09' AND '2026-11-15'")
            conn.commit()
        self.runs(["2026-11-17", "2026-11-18"])
        result = self.roll()
        self.assertIn("Capped at 17.6 km: last week was 16 km", result["reason"])


class AutoRegenerateTests(RollingBase):
    def setUp(self):
        super().setUp()
        self.regenerate(vdot=50.4)

    def versions(self):
        with closing(self.conn()) as conn:
            return [tuple(r) for r in conn.execute("SELECT trigger, note FROM plan_versions ORDER BY id")]

    def test_css_test_and_schedule_change_rebuild_the_drafts(self):
        json.loads(mcp_server.record_css_test("7:20", "3:30", "2026-11-12"))
        self.assertEqual(self.versions()[-1][0], "css_test")
        mcp_server.update_athlete_profile("plan_schedule", json.dumps({"long": "sat", "easy": ["sun", "wed", "mon"]}))
        self.assertEqual(self.versions()[-1][0], "profile")
        longs = {date.fromisoformat(r["date"]).weekday() for r in self.live()
                 if r["kind"] == "long" and r["date"] >= "2026-11-23"}
        self.assertEqual(longs, {5})

    def test_fitness_change(self):
        with db.get_connection() as conn:
            self.assertIsNone(rolling.on_fitness(conn, 50.6))
            result = rolling.on_fitness(conn, 52)
        self.assertGreater(result["counts"]["changed"], 0)
        self.assertEqual(self.versions()[-1][0], "vdot")
        with db.get_connection() as conn:
            self.assertEqual(rolling.plan(conn)["version"]["vdot"], 52)

    def test_profile_sections_the_plan_ignores(self):
        before = len(self.versions())
        mcp_server.update_athlete_profile("cautions", "left calf a bit tight")
        self.assertEqual(len(self.versions()), before)


class AppPlanTests(RollForwardBase):
    def test_plan_payload_has_drafts_and_proposals(self):
        self.runs(["2026-11-17", "2026-11-18"])
        self.roll()
        payload = coach_plan()
        drafts = [w for w in payload["workouts"] if w.get("source") == "draft"]
        on_garmin = [w for w in payload["workouts"] if w.get("source") != "draft"]
        self.assertTrue(drafts and on_garmin)
        first = drafts[0]
        self.assertTrue(first["schedule_id"].startswith("draft:"))
        self.assertEqual((first["workout_id"], first["status"]), (-int(first["schedule_id"][6:]), "planned"))
        self.assertTrue(first["steps"] and first["category"])
        self.assertEqual([w["date"] for w in payload["workouts"]], sorted(w["date"] for w in payload["workouts"]))
        rolling_plan = payload["rolling"]
        self.assertEqual(rolling_plan["auto_publish"], "never")
        self.assertEqual(rolling_plan["next_week"], "2026-11-23")
        [proposal] = rolling_plan["proposals"]
        self.assertEqual(proposal["week_start"], "2026-11-23")
        self.assertIn("Capped", proposal["reason"])
        self.assertLess(proposal["proposed_run_km"], proposal["run_km"])
        self.assertTrue(proposal["changed"] or proposal["removed"])
        without = coach_plan(drafts=["0"])
        self.assertFalse(any(w.get("source") == "draft" for w in without["workouts"]))

    def test_move_a_draft_and_a_published_workout(self):
        from garmin_ai import coach
        with closing(self.conn()) as conn:
            easy = dict(conn.execute("SELECT * FROM plan_drafts WHERE week_start = '2026-11-30' AND kind = 'easy' "
                                     "AND status = 'draft' ORDER BY date LIMIT 1").fetchone())
        moved = coach.move_request({"schedule_id": f"draft:{easy['id']}", "date": "2026-12-04"})
        self.assertEqual((moved["from"], moved["date"]), (easy["date"], "2026-12-04"))
        new = next(r for r in self.live() if r["id"] == int(moved["schedule_id"].removeprefix("draft:")))
        self.assertEqual((new["origin"], new["parent_id"], new["status"]), ("athlete", easy["id"], "draft"))
        with self.assertRaisesRegex(ValueError, "Only a draft"):
            coach.move_request({"schedule_id": f"draft:{easy['id']}", "date": "2026-12-05"})
        with closing(self.conn()) as conn:
            published = dict(conn.execute(
                "SELECT p.*, s.schedule_id FROM plan_drafts p JOIN coach_schedules s ON s.workout_id = p.workout_id "
                "AND s.date = p.date WHERE p.status = 'published' AND p.date >= '2026-11-20' "
                "ORDER BY p.date LIMIT 1").fetchone())
        coach.move_request({"schedule_id": published["schedule_id"], "date": "2026-11-21"})
        row = next(r for r in self.live() if r["parent_id"] == published["id"])
        self.assertEqual((row["date"], row["status"], row["workout_id"]),
                         ("2026-11-21", "published", published["workout_id"]))


def coach_plan(**query):
    from garmin_ai import coach
    return coach.plan_payload({"start": ["2026-11-01"], "end": ["2027-01-31"], **query})


GARMIN_ZONES = [{"trainingMethod": "LACTATE_THRESHOLD", "restingHeartRateUsed": 50,
                 "lactateThresholdHeartRateUsed": 175, "zone1Floor": 105, "zone2Floor": 135, "zone3Floor": 149,
                 "zone4Floor": 154, "zone5Floor": 163, "maxHeartRateUsed": 198, "sport": "DEFAULT"}]


class HeartRateZoneTests(RollingBase):
    def test_zones_and_targets(self):
        zones = rolling.zones_from(GARMIN_ZONES)
        self.assertEqual((zones["method"], zones["lthr"], zones["floors"]),
                         ("LACTATE_THRESHOLD", 175, [105, 135, 149, 154, 163]))
        running = {**GARMIN_ZONES[0], "sport": "RUNNING", "zone1Floor": 100}
        self.assertEqual(rolling.zones_from(GARMIN_ZONES + [running])["floors"][0], 100)
        self.assertIsNone(rolling.zones_from([{"zone1Floor": 150, "zone2Floor": 140}]))
        from garmin_ai.coaching import materialize
        targets = materialize.hr_targets(zones)
        self.assertEqual(targets, {"easy": {"hr_bpm": [105, 148]}, "steady": {"hr_bpm": [149, 153]}})
        self.assertIsNone(materialize.hr_targets(None))

    def test_refresh_stores_zones_and_rebuilds_the_drafts(self):
        self.regenerate(vdot=50.4)
        with db.get_connection() as conn:
            self.assertIsNone(rolling.refresh_hr_zones(conn, fetch=lambda: 1 / 0))  # Garmin down: no change
            rolling.refresh_hr_zones(conn, fetch=lambda: GARMIN_ZONES)
            rolling.refresh_hr_zones(conn, fetch=lambda: GARMIN_ZONES)  # unchanged: nothing new
        with closing(self.conn()) as conn:
            triggers = [r[0] for r in conn.execute("SELECT trigger FROM plan_versions ORDER BY id")]
        self.assertEqual(triggers, ["manual", "hr_zones"])
        easy = next(r for r in self.live() if r["kind"] == "easy")
        step = json.loads(easy["spec_json"])["steps"][0]
        self.assertEqual(step["target"], {"hr_bpm": [105, 148]})
        self.assertIn("HR 105-148", json.loads(easy["spec_json"])["description"])
        long = next(r for r in self.live() if r["kind"] == "long" and "marathon pace" in r["spec_json"])
        self.assertEqual(json.loads(long["spec_json"])["steps"][-1]["target"], {"hr_bpm": [149, 153]})

    def test_time_estimates_use_your_own_easy_pace(self):
        self.regenerate(vdot=50.4)
        with db.get_connection() as conn:
            easy_pace, _ = rolling.own_paces(conn)
            data = rolling.plan(conn, "2026-11-16", 1)
        self.assertEqual(easy_pace, 330)  # the fixture's 8 km in 44:00
        run = next(i for i in data["weeks"][0]["items"] if i["kind"] == "easy" and "+" not in i["name"])
        self.assertAlmostEqual(run["minutes"], run["km"] * 330 / 60, delta=1)


class RampLimitTests(RollForwardBase):
    def test_limit_from_km_run(self):
        with db.get_connection() as conn:
            rules = history.rules(conn)
            self.runs(["2026-11-17", "2026-11-18"])  # last week 16 km, 12-week peak 40 km
            self.assertEqual(rolling._ramp_limit(conn, "2026-11-23", rules), (19.2, 16.0))
            conn.execute("DELETE FROM activities WHERE substr(start_time, 1, 10) < '2026-11-16'")
            conn.commit()
            self.assertEqual(rolling._ramp_limit(conn, "2026-11-23", rules), (17.6, 16.0))  # 16 is the peak: +10%
            self.assertIsNone(rolling._ramp_limit(conn, "2026-11-16", rules))


class OffWatchTests(RollingBase):
    """Gym (logged in Hevy) and bike stay in the plan but are never sent to Garmin."""

    def setUp(self):
        super().setUp()
        mcp_server.update_athlete_profile("rule_overrides", json.dumps(
            {"structure": {"gym_min": 1}, "rolling": {"watch_sports": ["run", "swim"]}}))
        self.regenerate(vdot=50.4)

    def week(self, monday):
        return [r for r in self.live() if r["week_start"] == monday]

    def test_publish_keeps_gym_and_bike_off_the_watch(self):
        with db.get_connection() as conn:
            dry = rolling.publish(conn, "2026-11-23")
            self.assertTrue(dry["off_watch"])
            self.assertFalse(any("Gym" in i["name"] or "Bike" in i["name"] for i in dry["items"]))
            result = rolling.publish(conn, "2026-11-23", dry_run=False, allow_errors=True)
        rows = self.week("2026-11-23")
        off = [r for r in rows if r["sport"] in ("gym", "bike")]
        self.assertTrue(off and all(r["status"] == "published" and r["workout_id"] == "" for r in off))
        on = [r for r in rows if r["sport"] in ("run", "swim")]
        self.assertTrue(all(r["status"] == "published" and r["workout_id"] for r in on))
        self.assertEqual(result["published"], len(on))
        uploaded = {w["workoutName"] for w in self.api.workouts.values()}
        self.assertFalse(any("Gym" in n or "Bike" in n for n in uploaded))
        with closing(self.conn()) as conn:
            self.assertEqual(rolling.first_open_week(conn, TODAY).isoformat(), "2026-11-30")

    def test_app_shows_them_and_they_can_move(self):
        with db.get_connection() as conn:
            rolling.publish(conn, "2026-11-23", dry_run=False, allow_errors=True)
            items = rolling.app_drafts(conn, date(2026, 11, 23), date(2026, 11, 29), None, today=TODAY)
        gym = next(i for i in items if i["kind"] == "gym")
        self.assertEqual((gym["source"], gym["off_watch"]), ("plan", True))
        self.assertFalse(any(i["kind"] == "easy" for i in items))  # runs are on Garmin, listed from there
        with db.get_connection() as conn:
            moved = rolling.move_draft(conn, gym["schedule_id"], "2026-11-27", today=TODAY)
        row = next(r for r in self.live() if r["id"] == int(moved["schedule_id"][6:]))
        self.assertEqual((row["date"], row["status"], row["workout_id"]), ("2026-11-27", "published", ""))

    def test_a_rejected_drop_goes_back_off_the_watch(self):
        with db.get_connection() as conn:
            rolling.publish(conn, "2026-11-23", dry_run=False, allow_errors=True)
        with closing(self.conn()) as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM plan_drafts WHERE week_start = '2026-11-23' "
                                                  "AND status = 'published' AND date >= '2026-11-23'")]
        items = [{"date": r["date"], "workout": json.loads(r["spec_json"]), "session": json.loads(r["session_json"])}
                 for r in rows if r["sport"] != "gym"]
        with db.get_connection() as conn:
            rolling.propose_week(conn, "2026-11-23", items, "No gym this week.", dry_run=False, allow_errors=True,
                                 today=date(2026, 11, 23))
            rolling.decide(conn, "2026-11-23", False)
        gym = [r for r in self.week("2026-11-23") if r["sport"] == "gym" and r["date"] >= "2026-11-23"]
        self.assertTrue(all(r["status"] == "published" and r["workout_id"] == "" for r in gym))

    def test_dry_run_and_plan_say_what_goes_to_the_watch(self):
        with db.get_connection() as conn:
            dry = rolling.regenerate(conn, trigger="manual", dry_run=True, today=TODAY)
            data = rolling.plan(conn, "2026-11-30", 1)
        self.assertEqual(dry["sent_to_watch"], ["run", "swim"])
        self.assertTrue(any("Gym" in b for w in dry["weeks"] for b in w.get("plan_only", [])))
        items = data["weeks"][0]["items"]
        self.assertFalse(next(i for i in items if i["kind"] == "gym")["on_watch"])
        self.assertTrue(next(i for i in items if i["kind"] == "long")["on_watch"])

    def test_no_gym_count_warning_when_gym_stays_off_the_watch(self):
        warnings = ["week 2026-11-23: 0 gym sessions; at least 1.", "week 2026-11-23: No bike session planned.",
                    "week 2026-11-23: 4 runs planned; default is 5."]
        self.assertEqual(rolling._without_off_watch(warnings, ["run", "swim"]), warnings[2:])
        self.assertEqual(rolling._without_off_watch(warnings, ["run", "swim", "bike", "gym"]), warnings)


class AdoptWeekTests(RollingBase):
    """The week of 2026-11-16 was planned by hand (on Garmin) before the rolling plan existed."""

    def setUp(self):
        super().setUp()
        from garmin_ai import coach
        mcp_server.update_athlete_profile("rule_overrides", json.dumps({"rolling": {"watch_sports": ["run", "swim"]}}))
        coach.create_workout("running", "Sub-threshold 4x5min", [
            {"type": "warmup", "duration": {"time_s": 900}, "target": {"hr_bpm": [105, 148]}},
            {"type": "repeat", "repeat": {"count": 4, "steps": [
                {"type": "interval", "duration": {"time_s": 300}, "target": {"pace_min_per_km": ["4:12", "4:22"]},
                 "notes": "Controlled"},
                {"type": "recovery", "duration": {"time_s": 60}}]}},
            {"type": "cooldown", "duration": {"time_s": 600}}], date_="2026-11-17")
        coach.create_workout("running", "Long 70min", [
            {"type": "interval", "duration": {"time_s": 4200}, "target": {"hr_bpm": [105, 148]}}], date_="2026-11-21")
        coach.create_workout("lap_swimming", "Swim A Technique 650", [
            {"type": "warmup", "duration": {"distance_m": 100}, "swim": {"stroke": "free"}},
            {"type": "repeat", "repeat": {"count": 3, "steps": [
                {"type": "interval", "duration": {"distance_m": 25}, "swim": {"stroke": "free", "drill": "kick",
                                                                            "equipment": "fins"}},
                {"type": "rest", "duration": {"time_s": 20}}]}}], pool_length_m=25, date_="2026-11-16")
        coach.create_workout("strength", "Triathlon gym 40min", [
            {"type": "repeat", "repeat": {"count": 3, "steps": [
                {"type": "interval", "duration": {"reps": 8}, "notes": "Back squat 8"},
                {"type": "rest", "duration": {"time_s": 120}}]}}], date_="2026-11-18")
        self.regenerate(vdot=50.4)

    def test_payload_reads_back_exactly(self):
        from garmin_ai import workouts
        for workout in self.api.workouts.values():
            if not str(workout.get("workoutName", "")).startswith(workouts.PREFIX):
                continue
            spec = workouts.spec_from_payload(workout)
            again = workouts.build_workout(spec["sport"], spec["name"], spec["steps"], spec.get("description"),
                                           spec.get("pool_length_m"))
            self.assertEqual(workouts.canonical_steps(again), workouts.canonical_steps(workout), spec["name"])

    def test_sunday_review_skips_a_week_planned_by_hand(self):
        self.assertEqual(self.versions_first_week(), "2026-11-23")
        with db.get_connection() as conn, self.assertRaisesRegex(history.HistoryError, "adopt_week"):
            rolling.roll_forward(conn, "2026-11-16", date(2026, 11, 15))

    def versions_first_week(self):
        with closing(self.conn()) as conn:
            return conn.execute("SELECT MIN(week_start) FROM plan_drafts").fetchone()[0]

    def test_adopt_then_keep_gym_off_the_watch(self):
        dry = json.loads(mcp_server.adopt_week("2026-11-16"))
        self.assertEqual(len(dry["adopted"]), 4)
        self.assertEqual(dry["on_garmin_but_kept_off_the_watch"], ["2026-11-18 Triathlon gym 40min"])
        self.assertEqual([r for r in self.live() if r["week_start"] == "2026-11-16"], [])
        with db.get_connection() as conn:
            rolling.adopt_week(conn, "2026-11-16", dry_run=False, today=date(2026, 11, 13))
        week = [r for r in self.live() if r["week_start"] == "2026-11-16"]
        self.assertEqual(len(week), 4)
        self.assertTrue(all(r["status"] == "published" and r["workout_id"] and r["origin"] == "agent" for r in week))
        kinds = {r["name"]: r["kind"] for r in week}
        self.assertEqual((kinds["Sub-threshold 4x5min"], kinds["Long 70min"], kinds["Triathlon gym 40min"]),
                         ("quality", "long", "gym"))
        self.assertIn("nothing to adopt", mcp_server.adopt_week("2026-11-16"))  # already in the plan
        with db.get_connection() as conn:
            self.assertRaisesRegex(history.HistoryError, "already on Garmin", rolling.roll_forward, conn,
                                   "2026-11-16", date(2026, 11, 15))
            gym = next(r for r in week if r["kind"] == "gym")
            off = rolling.keep_off_watch(conn, "2026-11-16", dry_run=False, today=date(2026, 11, 13))
        self.assertEqual(off["off_watch"], ["2026-11-18 Triathlon gym 40min"])
        self.assertIn(("delete", int(gym["workout_id"])), self.api.calls)
        now = next(r for r in self.live() if r["week_start"] == "2026-11-16" and r["kind"] == "gym")
        self.assertEqual((now["status"], now["workout_id"], now["parent_id"]), ("published", "", gym["id"]))

