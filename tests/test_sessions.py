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
        self.assertIn("2 of 4 efforts kept their shape and length (I1 and I2)", text)
        self.assertIn("Strokes were shorter than Easy in I3 and I4", text)
        self.assertNotIn("finish faded", text)  # shorter strokes are not a fading finish
        self.assertIn("were the longer efforts", text)
        self.assertIn("Easy pace held at about 110 W", text)
        self.assertNotIn("-", text.replace("−", ""))  # real minus signs only

    def test_no_intervals(self):
        from tests.test_coach import STEADY
        summary = rowed((100, 110, STEADY, 115)).coach.session_summary()
        self.assertEqual(summary["intervals"], [])
        self.assertIn("No efforts", summary["findings"][0])
        self.assertEqual(summary["strokes"], 100)
        self.assertEqual(summary["avg_power_w"], 110)

    def test_all_good(self):
        self.assertEqual(coach.findings([{"index": 1, "rating": "good", "finish_pct": 1, "duration_s": 60,
                                          "length_delta_cm": 1, "fade_pct": 0}])[0],
                         "All 1 efforts kept their shape and length: finish within 5% of your usual at that intensity.")

    def test_faded_finish(self):
        iv = [{"index": 1, "rating": "short", "finish_pct": -15, "duration_s": 120, "length_delta_cm": 0,
               "fade_pct": 0}]
        self.assertIn("The finish faded in I1: \u221215% against your usual at that intensity.", coach.findings(iv))

    def test_legs_and_fade(self):
        iv = [{"index": 1, "rating": "good", "finish_pct": 8, "duration_s": 120, "length_delta_cm": 0,
               "fade_pct": -9, "legs_late": True, "steady_power_w": 100},
              {"index": 2, "rating": "good", "finish_pct": 0, "duration_s": 60, "length_delta_cm": 0,
               "fade_pct": 0, "legs_late": False, "steady_power_w": 120}]
        text = " ".join(coach.findings(iv))
        self.assertIn("The push came later in the drive than in Easy rowing in I1", text)
        self.assertNotIn("leg drive", text.lower())  # describes the curve, not muscles
        self.assertIn("I1 faded within itself: finish −9%", text)
        self.assertIn("Easy pace went from 100 W to 120 W", text)
        self.assertIn("1 of 2 efforts kept their shape and length (I2)", text)  # late legs don't count as kept


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

    def test_close_saves_complete_and_keeps_upload_marks(self):
        r = saturday()
        rec = self.recorder(r)
        wall0 = 1_800_000_000.0
        path = rec.update(RowingMetrics(distance_m=6000), mono_now=r.t, wall_now=wall0)
        record = json.loads(path.read_text())
        sessions.save(self.dir, {**record, "uploads": {"strava": {"activity_id": 1}}})
        rec.close(RowingMetrics(distance_m=6100))
        record = json.loads(path.read_text())
        self.assertTrue(record["complete"])
        self.assertEqual(record["uploads"], {"strava": {"activity_id": 1}})
        self.assertEqual(record["ended_at"] - record["started_at"], round(r.coach.last_time - r.coach.first_time))
        self.assertIsNone(rec.close(RowingMetrics()))  # once is enough

    def test_finish_by_hand(self):
        r = saturday()
        rec = self.recorder(r)
        rec.update(RowingMetrics(distance_m=6000), mono_now=r.t, wall_now=1_800_000_000.0)
        session = rec.finish(RowingMetrics(distance_m=6000))
        record = sessions.read_session(str(self.dir), session)
        self.assertTrue(record["complete"] and record["finished"])
        self.assertTrue(sessions.read_strokes(str(self.dir), session))
        from tests.test_coach import STEADY
        short = rowed((10, 110, STEADY, 115))
        self.assertIsNone(self.recorder(short).finish(RowingMetrics()))  # no session yet: nothing to finish

    def test_bridge_handles_done_from_the_screen(self):
        from types import SimpleNamespace
        from smartrow_bridge import status
        from smartrow_bridge.bridge import Bridge
        r = saturday()
        rec = self.recorder(r)
        rec.update(RowingMetrics(distance_m=6000), mono_now=r.t, wall_now=1_800_000_000.0)
        b = object.__new__(Bridge)
        b.cfg = SimpleNamespace(status_file=str(Path(self.tmp.name) / "status.json"), session_dir=str(self.dir))
        b.recorder, b.coach, b.uploader, b._finish_result = rec, r.coach, None, None
        b.parser = SimpleNamespace(metrics=RowingMetrics(distance_m=6000))
        token = status.send_request(b.cfg.status_file, "finish")
        session = rec._id
        b._handle_request()
        self.assertEqual(b._finish_result, {"token": token, "session": session})
        self.assertTrue(sessions.read_session(str(self.dir), session)["finished"])
        self.assertEqual(r.coach.strokes_total, 0)  # rowing again is a new session
        b._handle_request()  # nothing waiting: nothing happens
        self.assertEqual(b._finish_result["token"], token)

    def test_bridge_discards_from_the_screen(self):
        from types import SimpleNamespace
        from smartrow_bridge import status
        from smartrow_bridge.bridge import Bridge
        r = saturday()
        rec = self.recorder(r)
        rec.update(RowingMetrics(distance_m=6000), mono_now=r.t, wall_now=1_800_000_000.0)
        session = rec._id
        self.assertTrue((self.dir / f"{session}.json").exists())
        b = object.__new__(Bridge)
        b.cfg = SimpleNamespace(status_file=str(Path(self.tmp.name) / "status.json"), session_dir=str(self.dir))
        b.recorder, b.coach, b.uploader, b._finish_result = rec, r.coach, None, None
        b.parser = SimpleNamespace(metrics=RowingMetrics(distance_m=6000))
        token = status.send_request(b.cfg.status_file, "discard")
        b._handle_request()
        self.assertEqual(b._finish_result, {"token": token, "session": session, "action": "discard", "uploaded": []})
        self.assertFalse((self.dir / f"{session}.json").exists())
        self.assertFalse((self.dir / f"{session}.strokes.json").exists())
        self.assertEqual(r.coach.strokes_total, 0)
        # Rowing on: a new session, a new file, the discarded one stays gone.
        from tests.test_coach import STEADY
        r.row(40, 110, STEADY, 115)
        path = rec.update(RowingMetrics(distance_m=400), mono_now=r.t, wall_now=1_800_000_900.0)
        self.assertNotEqual(path.stem, session)
        self.assertFalse((self.dir / f"{session}.json").exists())

    def test_clock_set_mid_session_keeps_the_file(self):
        r = saturday()
        rec = self.recorder(r)
        first = rec.update(RowingMetrics(), mono_now=r.t, wall_now=1_000_000_000.0)  # clock not yet set
        later = rec.update(RowingMetrics(), mono_now=r.t + sessions.END_AFTER_S + 1,
                           wall_now=1_800_000_000.0 + sessions.END_AFTER_S + 1)  # the network set it
        self.assertEqual(first, later)
        record = json.loads(later.read_text())
        self.assertEqual(record["started_at"], round(1_800_000_000.0 - (r.t - r.coach.first_time)))

    def test_distance_offset(self):
        self.assertEqual(sessions.distance_offset([]), 0)
        self.assertEqual(sessions.distance_offset([9, 18, 27]), 0)
        self.assertEqual(sessions.distance_offset([5009, 5018, 5027]), 5000)

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
        self.assertEqual((listing[1]["intervals"], listing[1]["short"]), (4, 0))  # shorter, not faded
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


class EasyStrokeProgressTest(unittest.TestCase):
    def test_fresh_easy_stroke_in_progress(self):
        from smartrow_bridge import sessions
        from tests.test_coach import STEADY
        base = {"rowing_s": 1200, "distance_m": 5000, "intervals": [], "steady_power_w": 100}
        old = sessions.session_point({**base, "fresh_easy": {"curve": STEADY, "back_share": 0.45, "length_cm": 112.0}})
        new = sessions.session_point({**base, "fresh_easy": {"curve": STEADY, "back_share": 0.42, "length_cm": 118.0,
                                                             "measured_length_cm": 123.0}})
        none = sessions.session_point(base)
        self.assertEqual((old["easy_length_cm"], old["easy_back_pct"]), (112.0, 45.0))
        self.assertEqual(new["easy_length_cm"], 123.0)  # what was rowed, not the length the coach kept
        self.assertIsNone(none["easy_length_cm"])
        pts = [{**old, "started_at": 0}, {**new, "started_at": 86400}]
        text = " ".join(sessions.progress_findings(pts, None, now=2 * 86400))
        self.assertIn("Your Easy stroke: strokes 112 to 123 cm and force in the back half of the drive 45.0% to 42.0%",
                      text)
