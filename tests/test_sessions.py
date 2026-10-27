import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from smartrow_bridge import coach, sessions
from smartrow_bridge.protocol import RowingMetrics
from tests.test_coach import FULL, SHORT, Rower


def rowed(*plan):
    """A Rower that has rowed (strokes, watts, curve, length) blocks."""
    r = Rower()
    for n, watts, curve, length in plan:
        r.row(n, watts, curve, length)
    return r




def saturday():
    """Steady rowing with two 1-minute efforts that keep their shape and two 2-minute ones that go short."""
    from tests.test_coach import STEADY
    plan = [(60, 110, STEADY, 115)]
    for curve, n, length in ((FULL, 21, 119), (FULL, 21, 118), (SHORT, 42, 112), (SHORT, 42, 112)):
        plan += [(n, 152, curve, length), (40, 110, STEADY, 115)]
    return rowed(*plan)


class FindingsTest(unittest.TestCase):
    def test_saturday_like_session(self):
        summary = saturday().coach.session_summary()
        self.assertEqual(len(summary["intervals"]), 4)
        text = " ".join(summary["findings"])
        self.assertIn("2 of 4 kept their shape (I1 and I2)", text)
        self.assertIn("I3 and I4 went short", text)
        self.assertIn("were the longer efforts", text)
        self.assertIn("Steady pace held at about 110 W", text)
        self.assertNotIn("-", text.replace("−", ""))  # real minus signs only

    def test_no_intervals(self):
        from tests.test_coach import STEADY
        summary = rowed((100, 110, STEADY, 115)).coach.session_summary()
        self.assertEqual(summary["intervals"], [])
        self.assertIn("No intervals", summary["findings"][0])
        self.assertEqual(summary["strokes"], 100)
        self.assertEqual(summary["avg_power_w"], 110)

    def test_all_good(self):
        self.assertEqual(coach.findings([{"index": 1, "rating": "good", "finish_pct": 1, "duration_s": 60,
                                          "length_delta_cm": 1, "fade_pct": 0}])[0],
                         "All 1 intervals kept their shape: finish within 5% of steady.")

    def test_legs_and_fade(self):
        iv = [{"index": 1, "rating": "good", "finish_pct": 8, "duration_s": 120, "length_delta_cm": 0,
               "fade_pct": -9, "legs_late": True, "steady_power_w": 100},
              {"index": 2, "rating": "good", "finish_pct": 0, "duration_s": 60, "length_delta_cm": 0,
               "fade_pct": 0, "legs_late": False, "steady_power_w": 120}]
        text = " ".join(coach.findings(iv))
        self.assertIn("Leg drive came late in I1", text)
        self.assertIn("I1 faded within itself: finish −9%", text)
        self.assertIn("Steady pace went from 100 W to 120 W", text)
        self.assertIn("1 of 2 kept their shape (I2)", text)  # late legs don't count as kept


class RecorderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "sessions"

    def tearDown(self):
        self.tmp.cleanup()

    def recorder(self, r):
        return sessions.SessionRecorder(str(self.dir), r.coach)

    def test_too_short_is_not_saved(self):
        from tests.test_coach import STEADY
        r = rowed((10, 110, STEADY, 115))
        self.assertIsNone(self.recorder(r).update(RowingMetrics(), mono_now=r.t, wall_now=1e9))
        self.assertFalse(self.dir.exists())

    def test_saved_on_interval_and_at_the_end(self):
        r = saturday()
        rec = self.recorder(r)
        wall0 = 1_800_000_000.0
        path = rec.update(RowingMetrics(distance_m=6000, elapsed_s=int(r.t)), mono_now=r.t, wall_now=wall0)
        self.assertIsNotNone(path)
        record = json.loads(path.read_text())
        self.assertFalse(record["complete"])
        self.assertEqual(len(record["intervals"]), 4)
        self.assertEqual(record["distance_m"], 6000)
        self.assertEqual(record["started_at"], round(wall0 - (r.t - r.coach.first_time)))
        self.assertEqual(path.stem, sessions.session_id(record["started_at"]))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)  # the web page runs as another user
        # nothing new: not rewritten
        self.assertIsNone(rec.update(RowingMetrics(), mono_now=r.t + 1, wall_now=wall0 + 1))
        # two minutes without a stroke: the session is over, saved once more as complete
        done = rec.update(RowingMetrics(), mono_now=r.t + sessions.END_AFTER_S + 1,
                          wall_now=wall0 + sessions.END_AFTER_S + 1)
        self.assertTrue(json.loads(done.read_text())["complete"])
        self.assertEqual(done, path)  # same file
        self.assertIsNone(rec.update(RowingMetrics(), mono_now=r.t + 999, wall_now=wall0 + 999))

    def test_new_session_new_file_and_listing(self):
        r = saturday()
        rec = self.recorder(r)
        first = rec.update(RowingMetrics(distance_m=6000), mono_now=r.t, wall_now=1_800_000_000.0)
        r.count = 0  # the SmartRow app starts a new session
        from tests.test_coach import STEADY
        r.row(80, 110, STEADY, 115)
        second = rec.update(RowingMetrics(distance_m=900), mono_now=r.t, wall_now=1_800_090_000.0)
        self.assertNotEqual(first, second)
        listing = sessions.list_sessions(str(self.dir))
        self.assertEqual([x["id"] for x in listing], [second.stem, first.stem])  # newest first
        self.assertEqual((listing[1]["intervals"], listing[1]["short"]), (4, 2))
        self.assertEqual(sessions.read_session(str(self.dir), first.stem)["distance_m"], 6000)

    def test_read_rejects_odd_ids(self):
        self.dir.mkdir(parents=True)
        (self.dir / "notes.json").write_text("{}")
        for bad in ("../../etc/passwd", "notes", "2026-10-27T0702/../x", ""):
            self.assertIsNone(sessions.read_session(str(self.dir), bad))
        self.assertEqual(sessions.list_sessions(str(self.dir)), [])

    def test_keeps_only_the_newest(self):
        r = saturday()
        record = sessions.build_record(r.coach, 1_800_000_000.0, 1_800_000_100.0, True, 0, 0)
        for k in range(sessions.KEEP + 5):
            sessions.save(self.dir, {**record, "id": f"2026-01-01T{k:04d}"})
        names = sorted(p.stem for p in self.dir.glob("*.json"))
        self.assertEqual(len(names), sessions.KEEP)
        self.assertEqual(names[0], "2026-01-01T0005")

    def test_unwritable_directory_warns_once(self):
        blocker = Path(self.tmp.name) / "a_file"
        blocker.write_text("x")
        r = saturday()
        rec = sessions.SessionRecorder(str(blocker / "sessions"), r.coach)
        with self.assertLogs("smartrow_bridge.sessions", "WARNING") as logs:
            self.assertIsNone(rec.update(RowingMetrics(), mono_now=r.t, wall_now=1e9))
            self.assertIsNone(rec.update(RowingMetrics(), mono_now=r.t, wall_now=1e9 + 400))
        self.assertEqual(len(logs.output), 1)


if __name__ == "__main__":
    unittest.main()


class ProgressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.now = 1_800_000_000.0

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, days_ago, steady_w, per_beat=None, finish_long=None, complete=True, rowing_s=1800):
        r = saturday()
        started = self.now - days_ago * 86400
        record = sessions.build_record(r.coach, started, started + rowing_s, complete, 6000, rowing_s)
        record.update(steady_power_w=steady_w, rowing_s=rowing_s)
        if per_beat is not None:
            record["heart_rate"] = {"avg": 120, "max": 150, "watts_per_beat": per_beat, "drift_pct": 2.0}
        if finish_long is not None:
            for i in record["intervals"]:
                if i["duration_s"] >= sessions.LONG_EFFORT_S:
                    i["finish_pct"] = finish_long
        sessions.save(self.dir, record)

    def test_trends_and_weeks(self):
        self.add(40, 95, 0.82, -16)
        self.add(30, 98, 0.85, -14)
        self.add(20, 102, 0.88, -10)
        self.add(10, 106, 0.90, -7)
        self.add(3, 110, 0.93, -4)
        self.add(1, 110, complete=False)  # still being rowed: left out
        self.add(2, 110, rowing_s=120)  # two minutes: left out
        p = sessions.progress(str(self.dir), weeks=12, now=self.now)
        self.assertEqual(len(p["sessions"]), 5)
        self.assertEqual(sum(w["sessions"] for w in p["by_week"]), 5)
        text = " ".join(p["findings"])
        self.assertIn("5 sessions and 30 km in the last 12 weeks", text)
        self.assertIn("(0.9 a week)", text)  # over the ~5.7 weeks since the first, not 12
        self.assertIn("Steady power went from about 95 W to 110 W (+16%)", text)
        self.assertIn("Watts per heartbeat at steady pace rose from 0.82 to 0.93", text)
        self.assertIn("Finish in longer efforts improved from −16% to −4%", text)
        self.assertNotIn("-", text.replace("−", "").replace("1:30", ""))
        recent = sessions.progress(str(self.dir), weeks=2, now=self.now)
        self.assertEqual(len(recent["sessions"]), 2)  # 10 and 3 days ago

    def test_one_session_and_none(self):
        self.assertEqual(sessions.progress(str(self.dir), weeks=4, now=self.now)["findings"],
                         ["No saved sessions in this period yet."])
        self.add(3, 110, finish_long=-15)
        p = sessions.progress(str(self.dir), weeks=4, now=self.now)
        self.assertEqual(len(p["findings"]), 1)  # nothing to compare yet
        point = p["sessions"][0]
        self.assertEqual(point["finish_long"], -15)
        self.assertEqual(point["intervals"], 4)

    def test_fatigue_reading(self):
        self.add(10, 110, 0.95)
        self.add(3, 110, 0.85)
        text = " ".join(sessions.progress(str(self.dir), weeks=4, now=self.now)["findings"])
        self.assertIn("fell from 0.95 to 0.85", text)
        self.assertIn("Steady power held at about 110 W", text)
