import os
import tempfile
import unittest

from smartrow_bridge import workout


def write_fit(path: str, name: str, steps: list[dict]) -> None:
    """A FIT workout file as intervals.icu exports one. steps: dicts of WorkoutStepMessage fields."""
    from fit_tool.fit_file_builder import FitFileBuilder
    from fit_tool.profile.messages.file_id_message import FileIdMessage
    from fit_tool.profile.messages.workout_message import WorkoutMessage
    from fit_tool.profile.messages.workout_step_message import WorkoutStepMessage
    from fit_tool.profile.profile_type import FileType, Manufacturer

    b = FitFileBuilder(auto_define=True, min_string_size=50)
    fid = FileIdMessage()
    fid.type = FileType.WORKOUT
    fid.manufacturer = Manufacturer.DEVELOPMENT.value
    fid.product = 1
    b.add(fid)
    w = WorkoutMessage()
    w.workout_name = name
    w.sport = 15
    w.sub_sport = 14
    w.num_valid_steps = len(steps)
    b.add(w)
    for i, fields in enumerate(steps):
        m = WorkoutStepMessage()
        m.message_index = i
        for k, v in fields.items():
            setattr(m, k, v)
        b.add(m)
    b.build().to_file(path)


def timed(seconds: float, watts: float | None = None, pct: float | None = None, intensity: int = 5) -> dict:
    d = {"duration_type": workout.DUR_TIME, "duration_time": seconds, "intensity": intensity}
    if watts is not None:
        d.update(target_type=10, custom_target_power_low=int(1000 + watts), custom_target_power_high=int(1000 + watts))
    if pct is not None:
        d.update(target_type=10, custom_target_power_low=int(pct), custom_target_power_high=int(pct))
    return d


class ReadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, name="w.fit"):
        return os.path.join(self.tmp.name, name)

    def test_watts_as_written_like_intervals_icu(self):
        write_fit(self.path(), "Adrestia-like", [timed(480, 120), timed(60, 160), timed(60, 80, intensity=1)])
        w = workout.read_fit(self.path())
        self.assertEqual(w.name, "Adrestia-like")
        self.assertEqual([(s.kind, s.length, s.low_w, s.high_w) for s in w.steps],
                         [("time", 480, 120, 120), ("time", 60, 160, 160), ("time", 60, 80, 80)])
        self.assertEqual(w.steps[2].intensity, "rest")
        self.assertEqual(w.total_s, 600)
        self.assertFalse(w.uses_ftp())

    def test_percent_of_ftp_and_ranges(self):
        write_fit(self.path(), "Pct", [timed(300, pct=75), {**timed(120), "target_type": 4,
                                                             "custom_target_power_low": 1150,
                                                             "custom_target_power_high": 1170}])
        w = workout.read_fit(self.path())
        self.assertTrue(w.uses_ftp())
        self.assertEqual(w.steps[0].low_pct, 75)
        self.assertEqual((w.steps[1].low_w, w.steps[1].high_w, w.steps[1].target_w), (150, 170, 160))
        scaled = w.with_ftp(200)
        self.assertEqual(scaled.steps[0].target_w, 150)
        self.assertIsNone(w.with_ftp(None).steps[0].target_w)  # no FTP: shown without a target

    def test_repeats_are_unrolled_and_distance_steps(self):
        write_fit(self.path(), "Reps", [
            timed(300, 110),
            {"duration_type": workout.DUR_DISTANCE, "duration_distance": 500.0, "target_type": 10,
             "custom_target_power_low": 1200, "custom_target_power_high": 1200},
            timed(60, 90),
            {"duration_type": workout.DUR_REPEAT_STEPS, "duration_step": 1, "target_repeat_steps": 3},
            {"duration_type": workout.DUR_OPEN},  # open: skipped
            timed(300, 100)])
        w = workout.read_fit(self.path())
        self.assertEqual([(s.kind, s.length, s.low_w) for s in w.steps],
                         [("time", 300, 110)] + [("distance", 500, 200), ("time", 60, 90)] * 3 + [("time", 300, 100)])
        self.assertIsNone(w.total_s)  # not all by time

    def test_stroke_rate_target_second_or_main(self):
        # Power with a cadence (stroke rate) range as the second target, and a step with only a rate target.
        write_fit(self.path(), "Rate", [
            {**timed(300, 180), "secondary_target_type": 3, "secondary_custom_target_cadence_low": 22,
             "secondary_custom_target_cadence_high": 24},
            {"duration_type": workout.DUR_TIME, "duration_time": 60, "intensity": 0, "target_type": 3,
             "custom_target_cadence_low": 26, "custom_target_cadence_high": 26},
            timed(60, 120)])
        w = workout.read_fit(self.path())
        self.assertEqual([(s.low_w, s.spm_low, s.spm_high) for s in w.steps],
                         [(180.0, 22.0, 24.0), (None, 26.0, 26.0), (120.0, None, None)])

    def test_not_a_workout(self):
        with open(self.path("x.fit"), "wb") as f:
            f.write(b"hello")
        with self.assertRaises(workout.WorkoutError):
            workout.read_fit(self.path("x.fit"))


def stroke(t, dist, watts, spm=18, hr=120, length=120):
    return (t, dist, watts, spm, hr, length, (), 1.2, 0.0, 0.0)


class PlayerTest(unittest.TestCase):
    def plan(self):
        return workout.Workout("Test", [workout.Step("time", 60, 120, 120), workout.Step("time", 30, 200, 200),
                                        workout.Step("distance", 100, 80, 80)])

    def test_steps_by_time_then_distance_and_the_result(self):
        p = workout.Player(self.plan())
        self.assertEqual(p.state(0, 0, None, 0)["state"], "ready")
        p.start(1000.0, 50, ftp=None)
        log, checked = [], 0
        t, d = 1000.0, 50.0
        while not p.done and t < 1300:
            watts = 120 if t < 1060 else 185 if t < 1090 else 80
            log.append(stroke(t, d, watts))
            p.advance(t, d)
            if t == 1030.0:
                st = p.state(t, d, 121, 0)
                self.assertEqual((st["step"], st["left"], st["target_w"], st["on_target"]), (1, 30, 120, "on"))
                checked += 1
                self.assertEqual(st["next"], {"target_w": 200, "kind": "time", "length": 30})
            if t == 1072.0:
                st = p.state(t, d, 185, 0)
                self.assertEqual((st["step"], st["left"], st["on_target"]), (2, 18, "under"))
                checked += 1
            t += 3
            d += 10
        self.assertTrue(p.done)
        self.assertEqual(checked, 2)
        self.assertEqual([round(s[0]) for s in p.step_started], [1000, 1060, 1090])
        self.assertIsNone(p.state(t, d, 80, 5))  # "done" shown for a few strokes, then gone
        self.assertEqual(p.state(t, d, 80, 2)["state"], "done")
        r = p.result(log, 1000.0)
        self.assertTrue(r["complete"])
        self.assertEqual([s["verdict"] for s in r["steps"]], ["on", "under", "on"])
        self.assertEqual(r["steps"][1]["power_w"], 185)
        self.assertEqual(r["steps"][1]["off_pct"], -7)
        self.assertEqual((r["on_target"], r["targeted"]), (2, 3))
        lines = workout.findings(r)
        self.assertIn("all 3 steps; on target in 2 of 3", lines[0])
        self.assertIn("step 2 185 W for 200", lines[1])

    def test_stroke_rate_live_and_in_the_result(self):
        p = workout.Player(workout.Workout("Rate", [workout.Step("time", 60, 180, 180, spm_low=22, spm_high=24),
                                                    workout.Step("time", 60, 120, 120)]))
        p.start(1000.0, 0, ftp=None)
        log, t = [], 1000.0
        while t < 1060:
            log.append(stroke(t, (t - 1000) * 4, 180, spm=21 if t < 1030 else 23))
            p.advance(t, (t - 1000) * 4)
            t += 3
        st = p.state(1020.0, 80, 180, 0, 21.4)
        self.assertEqual((st["spm_low"], st["spm_high"], st["spm"], st["spm_on"]), (22, 24, 21, "under"))
        self.assertEqual(p.state(1040.0, 160, 180, 0, 23.0)["spm_on"], "on")
        p.advance(1061.0, 244)
        self.assertNotIn("spm_low", p.state(1062.0, 248, 120, 0, 20.0))  # no rate target on step 2
        r = p.result(log, 1000.0)["steps"][0]
        self.assertEqual((r["spm_low"], r["spm_high"], r["spm_verdict"]), (22, 24, "on"))  # average 22.2
        self.assertEqual(r["spm_on_pct"], 59)  # the step's strokes after the settle: 10 of 17 at 23, inside

    def test_a_time_step_runs_on_while_you_stop(self):
        p = workout.Player(self.plan())
        p.start(0.0, 0, None)
        p.advance(75.0, 0)  # no stroke for 75 s: into step 2
        self.assertEqual(p.step, 1)
        self.assertEqual(p.state(75.0, 0, None, 0)["left"], 15)
        r = p.result([stroke(1.0, 0, 120)], 0.0)
        self.assertFalse(r["complete"])
        self.assertIn("1 of 3 steps (stopped early)", workout.findings(r)[0])


    def test_seconds_left_for_the_countdown(self):
        p = workout.Player(self.plan())
        p.start(0.0, 0, None)
        st = p.state(57.4, 100, 120, 0)
        self.assertEqual((st["left_s"], st["step_elapsed_s"]), (2.6, 57.4))
        p.advance(91.0, 300)  # into the distance step (100 m) at 300 m
        st = p.state(91.0, 340, 2.8 * 8, 0)  # 22.4 W is 2 m/s: 60 m left takes 30 s
        self.assertEqual((st["step"], st["kind"], st["left_s"], st["step_elapsed_s"]), (3, "distance", 30.0, 1.0))
        self.assertIsNone(p.state(91.0, 340, None, 0)["left_s"])  # not rowing: no estimate


class FolderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def stroke(self, t, watts, dist):
        from smartrow_bridge.protocol import FORCE_SAMPLES, StrokeCurve
        curve = [round(600 * (i / FORCE_SAMPLES) * (1 - i / FORCE_SAMPLES) * 4) for i in range(FORCE_SAMPLES)]
        return StrokeCurve(time=t, force_n=curve, power_w=watts, stroke_rate_spm=18,
                           stroke_length_cm=120, stroke_count=int(t), distance_m=dist, heart_rate_bpm=120)

    def test_armed_started_moved_to_done_and_in_the_session(self):
        from smartrow_bridge.coach import IntervalCoach
        write_fit(os.path.join(self.dir, "plan.fit"), "Plan", [timed(30, 120), timed(30, 160)])
        with open(os.path.join(self.dir, "junk.fit"), "wb") as f:
            f.write(b"not a fit file")
        os.utime(os.path.join(self.dir, "junk.fit"), (2e9, 2e9))  # the newest, but unreadable: skipped
        coach, folder = IntervalCoach(), workout.Folder(self.dir)
        with self.assertLogs("smartrow_bridge.workout", "INFO") as logs:
            folder.tick(coach, 0)
        self.assertIn("can't be followed", "\n".join(logs.output))
        self.assertEqual(coach.workout.workout.name, "Plan")
        self.assertEqual(coach.state(0)["workout"]["state"], "ready")
        t, d = 100.0, 0
        while t < 170:
            coach.add(self.stroke(t, 120 if t < 130 else 160, d))
            t, d = t + 3, d + 10
            folder.tick(coach, t)
            if t == 121.0:  # in step 1: its zone and the next one's, for the countdown's colours
                w = coach.state(t)["workout"]
                self.assertEqual(w["zone"], coach.intensity(120))
                self.assertEqual(w["next"]["zone"], coach.intensity(160))
                self.assertAlmostEqual(w["left_s"], 9.0, delta=0.05)
        self.assertEqual(os.listdir(os.path.join(self.dir, "done"))[0][-9:], "-plan.fit")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "plan.fit")))
        summary = coach.session_summary()
        self.assertTrue(summary["workout"]["complete"])
        self.assertEqual([x["verdict"] for x in summary["workout"]["steps"]], ["on", "on"])
        self.assertTrue(summary["findings"][0].startswith("Workout Plan: all 2 steps"))
        folder.tick(coach, t + 10)
        self.assertIs(coach.workout, folder.player)  # still this session's: no new one armed
        coach.reset()  # a new session: the next file may arm
        folder.tick(coach, t + 20)
        self.assertIsNone(coach.workout)

    def test_removing_the_file_cancels_and_a_stray_pull_waits(self):
        from smartrow_bridge.coach import IntervalCoach
        path = os.path.join(self.dir, "plan.fit")
        write_fit(path, "Plan", [timed(30, 120)])
        coach, folder = IntervalCoach(), workout.Folder(self.dir)
        folder.tick(coach, 0)
        coach.add(self.stroke(10.0, 100, 0))  # a stray pull: starts it
        self.assertTrue(coach.workout.started)
        coach.add(self.stroke(100.0, 100, 5))  # ... then a long pause: the row starts again, and so does it
        self.assertTrue(coach.workout.started)
        self.assertEqual(coach.workout.started_at, 100.0)
        coach.workout = None
        folder.player = None
        write_fit(path, "Plan", [timed(30, 120)])
        folder.tick(coach, 10)
        self.assertIsNotNone(coach.workout)
        os.remove(path)
        folder.tick(coach, 20)
        self.assertIsNone(coach.workout)


if __name__ == "__main__":
    unittest.main()
