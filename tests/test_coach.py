import unittest

from smartrow_bridge import coach
from smartrow_bridge.coach import IntervalCoach, back_share
from smartrow_bridge.protocol import StrokeCurve

# Average curves (N) from a real 45-minute session: steady rowing, an effort that kept
# its shape, and one that went short and leg-heavy (back-half share 0.29 / 0.31 / 0.24).
STEADY = [131, 210, 286, 344, 389, 422, 445, 465, 467, 455, 430, 401, 375, 340, 295, 246, 196, 148, 104, 68, 32,
          7, 0, 0]
FULL = [143, 232, 325, 401, 467, 520, 566, 593, 612, 604, 575, 537, 502, 461, 402, 337, 272, 211, 153, 100, 63, 28,
        0, 0]
SHORT = [176, 295, 414, 512, 593, 656, 696, 714, 701, 666, 613, 550, 492, 416, 339, 269, 203, 146, 95, 51, 14, 2,
         0, 0]
# Steady shape pushed 3 samples later: a slow push off the catch, force from back and arms.
LATE = [40, 80, 131, 210, 286, 344, 389, 422, 445, 465, 467, 455, 430, 401, 375, 340, 295, 246, 196, 148, 104, 68,
        32, 7]
RATE = 21.0
PERIOD = 60 / RATE


class Rower:
    """Feeds strokes to a coach, 21 spm."""

    def __init__(self) -> None:
        self.coach = IntervalCoach()
        self.t = 0.0
        self.count = 0
        self.summaries = []

    def row(self, n: int, watts: float, curve=STEADY, length: int = 115) -> None:
        for _ in range(n):
            self.t += PERIOD
            self.count += 1
            work = watts * 60 / RATE
            s = StrokeCurve(time=self.t, force_n=list(curve), stroke_rate_spm=RATE, stroke_length_cm=length,
                            work_j=work, stroke_count=self.count)
            summary = self.coach.add(s)
            if summary:
                self.summaries.append(summary)

    def state(self) -> dict:
        return self.coach.state(self.t)


class CoachTest(unittest.TestCase):
    def test_back_share(self):
        self.assertAlmostEqual(back_share(STEADY), 0.29, places=2)
        self.assertAlmostEqual(back_share(SHORT), 0.24, places=2)
        self.assertEqual(back_share([0] * 24), 0.0)

    def test_warmup_then_steady(self):
        r = Rower()
        r.row(10, 110)
        self.assertEqual(r.state()["mode"], "warmup")
        r.row(30, 110)
        st = r.state()
        self.assertEqual(st["mode"], "steady")
        self.assertEqual(st["steady_power_w"], 110)
        self.assertIsNone(st["last"])

    def test_effort_detected_summarised_and_logged(self):
        r = Rower()
        r.row(60, 110)
        with self.assertLogs("smartrow_bridge.coach", "INFO") as logs:
            r.row(21, 152, FULL, length=119)  # 1 minute
            self.assertEqual(r.state()["mode"], "interval")
            r.row(15, 110)
        self.assertEqual(len(r.summaries), 1)
        s = r.summaries[0]
        self.assertEqual(s.strokes, 21)  # the hard strokes only: start back-dated, easy ones dropped
        self.assertAlmostEqual(s.duration_s, 60, delta=1)
        self.assertAlmostEqual(s.power_w, 152, delta=1)
        self.assertEqual(s.rating, "good")
        self.assertEqual(s.length_delta_cm, 4)
        self.assertIn("Interval 1: 1:00, 21 strokes, 152 W", logs.output[0])
        st = r.state()
        self.assertEqual(st["mode"], "steady")
        self.assertTrue(st["show_summary"])
        self.assertEqual(st["last"]["index"], 1)
        self.assertEqual(st["steady_power_w"], 110)  # the effort never taught the steady reference

    def test_summary_goes_away(self):
        r = Rower()
        r.row(60, 110)
        r.row(21, 152, FULL)
        r.row(10, 110)
        self.assertTrue(r.state()["show_summary"])
        r.row(30, 110)
        st = r.state()
        self.assertFalse(st["show_summary"])
        self.assertEqual(st["last"]["index"], 1)  # still there for the X button

    def test_short_burst_is_not_an_interval(self):
        r = Rower()
        r.row(60, 110)
        r.row(5, 170)
        r.row(20, 110)
        self.assertEqual(r.summaries, [])

    def test_slow_warmup_does_not_start_an_interval(self):
        """Warming up at 75 W before settling at 96 W is not an effort."""
        r = Rower()
        for watts in (70, 75, 80, 85, 90, 94):
            r.row(10, watts)
        r.row(300, 96)
        self.assertEqual(r.summaries, [])
        self.assertEqual(r.state()["mode"], "steady")

    def test_long_rise_becomes_the_new_steady(self):
        r = Rower()
        r.row(60, 100)
        r.row(120, 135)  # > 5 minutes harder: a new pace, not an interval
        self.assertEqual(r.summaries, [])
        self.assertEqual(r.state()["mode"], "steady")
        self.assertGreater(r.state()["steady_power_w"], 125)

    def test_live_cue_finish(self):
        r = Rower()
        r.row(60, 110)
        r.row(3, 155, SHORT, length=112)
        self.assertIsNone(r.state()["live"]["cue"])  # too early to judge
        self.assertIsNone(r.state()["live"]["finish_pct"])
        r.row(8, 155, SHORT, length=112)
        live = r.state()["live"]
        self.assertEqual(live["cue"], "finish")
        self.assertLess(live["finish_pct"], -12)
        self.assertEqual(live["length_delta_cm"], -3)
        self.assertEqual(live["index"], 1)
        self.assertEqual(len(live["curve"]), 24)
        r.row(30, 155, SHORT, length=112)
        r.row(15, 110)
        self.assertEqual(r.summaries[0].rating, "short")
        self.assertLess(r.summaries[0].finish_pct, -12)

    def test_live_cue_legs(self):
        r = Rower()
        r.row(60, 110)
        r.row(12, 150, LATE)
        self.assertEqual(r.state()["live"]["cue"], "legs")
        with self.assertLogs("smartrow_bridge.coach", "INFO") as logs:
            r.row(10, 150, LATE)
            r.row(15, 110)
        s = r.summaries[0]
        self.assertTrue(s.legs_late)
        self.assertTrue(s.as_dict()["legs_late"])
        self.assertIn("leg drive late", logs.output[0])

    def test_longer_stroke_is_not_weak_legs(self):
        """A longer stroke moves the centre of force later by itself; that is not a weak leg drive."""
        self.assertLess(coach.drive_lateness(FULL, STEADY, 7), coach.LEGS_CUE)
        self.assertGreater(coach.drive_lateness(LATE, STEADY, 0), coach.LEGS_CUE + 1)
        self.assertLess(coach.drive_lateness(SHORT, STEADY, -3), 0)  # leg-heavy: early, the opposite

    def test_live_cue_good_and_length(self):
        r = Rower()
        r.row(60, 110)
        r.row(12, 152, FULL, length=118)
        self.assertEqual(r.state()["live"]["cue"], "good")
        r.row(10, 152, FULL, length=110)
        self.assertEqual(r.state()["live"]["cue"], "length")

    def test_stopping_ends_the_effort(self):
        r = Rower()
        r.row(60, 110)
        r.row(20, 152, FULL)
        st = r.coach.state(r.t + coach.GAP_S + 1)
        self.assertEqual(st["mode"], "steady")
        self.assertEqual(st["last"]["strokes"], 20)

    def test_new_session_starts_over(self):
        r = Rower()
        r.row(60, 110)
        r.row(21, 152, FULL)
        r.row(15, 110)
        r.count = 0  # SmartRow app started a new session
        r.row(1, 110)
        st = r.state()
        self.assertEqual((st["mode"], st["intervals"], st["last"]), ("warmup", 0, None))


if __name__ == "__main__":
    unittest.main()
