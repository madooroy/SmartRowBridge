import unittest

from smartrow_bridge import coach
from smartrow_bridge.coach import IntervalCoach, back_share
from smartrow_bridge.protocol import StrokeCurve

# Average curves (N) from a real 45-minute session: steady rowing, an effort that kept
# its shape, and one whose strokes went 2-3 % shorter (same shape over fewer samples: the
# samples are spaced by distance).
STEADY = [131, 210, 286, 344, 389, 422, 445, 465, 467, 455, 430, 401, 375, 340, 295, 246, 196, 148, 104, 68, 32,
          7, 0, 0]
FULL = [143, 232, 325, 401, 467, 520, 566, 593, 612, 604, 575, 537, 502, 461, 402, 337, 272, 211, 153, 100, 63, 28,
        0, 0]
SHORT = [176, 295, 414, 512, 593, 656, 696, 714, 701, 666, 613, 550, 492, 416, 339, 269, 203, 146, 95, 51, 14, 2,
         0, 0]
# The steady shape with the back half of the drive weakened (30 % less force there): a fading finish.
FADED = [round(v * (0.7 if 11 <= i <= 19 else 1)) for i, v in enumerate(STEADY)]
# Steady shape pushed 3 samples later: the force builds later in the drive (a late push).
LATE = [40, 80, 131, 210, 286, 344, 389, 422, 445, 465, 467, 455, 430, 401, 375, 340, 295, 246, 196, 148, 104, 68,
        32, 7]
RATE = 21.0
PERIOD = 60 / RATE


class Rower:
    """Feeds strokes to a coach, 21 spm."""

    def __init__(self, settings: coach.CoachSettings = coach.DEFAULT_SETTINGS, ftp=None) -> None:
        self.coach = IntervalCoach(settings, ftp=ftp)
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
        self.assertAlmostEqual(back_share(STEADY), 0.39, places=2)  # of its own drive
        self.assertAlmostEqual(back_share(FADED) / back_share(STEADY), 0.82, places=2)
        self.assertGreater(back_share(SHORT) / back_share(STEADY), 0.94)  # shorter, not fading
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
        r.row(3, 155, FADED, length=112)
        self.assertIsNone(r.state()["live"]["cue"])  # too early to judge
        self.assertIsNone(r.state()["live"]["finish_pct"])
        r.row(8, 155, FADED, length=112)
        live = r.state()["live"]
        self.assertEqual(live["cue"], "finish")
        self.assertLess(live["finish_pct"], -12)
        self.assertEqual(live["length_delta_cm"], -3)
        self.assertEqual(live["index"], 1)
        self.assertEqual(len(live["curve"]), 24)
        r.row(30, 155, FADED, length=112)
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
        self.assertIn("late push", logs.output[0])

    def test_shape_is_measured_on_the_strokes_own_drive(self):
        """The samples are spaced by distance: a shorter stroke ends sooner, which is LONGER STROKES, not
        a weaker finish or a later push."""
        # Real: Saturday's efforts with strokes 2-3 % shorter, against steady.
        self.assertLess(coach.drive_end(SHORT), coach.drive_end(STEADY))
        self.assertGreater(coach.back_share(SHORT) / coach.back_share(STEADY), 1 - (1 - coach.FINISH_CUE) / 2)
        self.assertLess(abs(coach.drive_lateness(SHORT, STEADY)), coach.LEGS_CUE)
        self.assertLess(abs(coach.drive_lateness(FULL, STEADY)), coach.LEGS_CUE)
        self.assertGreater(coach.drive_lateness(LATE, STEADY), coach.LEGS_CUE)

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


class FtpTest(unittest.TestCase):
    def test_long_effort_counts_with_an_ftp(self):
        """A 6-minute effort: a new steady pace without an FTP, an interval with one."""
        for ftp, found in ((None, 0), (185, 1)):
            r = Rower(ftp=ftp)
            r.row(60, 105)
            r.row(130, 145)  # about 6 minutes at 21 spm
            r.row(30, 105)
            self.assertEqual(len(r.summaries), found, ftp)
        self.assertEqual(r.summaries[0].as_dict()["ftp_pct"], 78)
        self.assertIn("Efforts: 1 Moderate (145 W).", coach.findings([{**r.summaries[0].as_dict(), "started_at_s": 0}]))

    def test_warmup_below_the_floor_is_not_an_effort(self):
        r = Rower(ftp=185)
        r.row(40, 80)  # easy start
        r.row(200, 105)  # into steady rowing: 31 % more, but under Moderate (67 % of FTP)
        self.assertEqual(r.summaries, [])
        self.assertIsNone(r.state().get("live"))

    def test_fixed_ftp_setting(self):
        from smartrow_bridge.bridge import _fixed_ftp
        self.assertEqual(_fixed_ftp("185"), 185.0)
        for unset in ("", "auto", "off", "0"):
            self.assertIsNone(_fixed_ftp(unset))


class FtpReadOnceTest(unittest.TestCase):
    def test_read_once_retrying_until_online(self):
        import asyncio
        import types
        from unittest import mock
        from smartrow_bridge import bridge, intervals
        answers = [OSError("offline"), 185.0]
        calls = []

        def fake(key):
            calls.append(key)
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a

        b = types.SimpleNamespace(cfg=types.SimpleNamespace(intervals_key_file="x"), coach=IntervalCoach())
        with mock.patch.object(intervals, "rowing_ftp", fake), mock.patch.object(intervals, "read_key", lambda f: "k"), \
                mock.patch.object(bridge, "FTP_RETRY_S", 0.01), self.assertLogs("smartrow_bridge.bridge"):
            asyncio.run(bridge.Bridge._ftp_loop(b))  # returns once it has an answer
        self.assertEqual((len(calls), b.coach.ftp), (2, 185.0))


class IntensityTest(unittest.TestCase):
    def test_bands_in_watts(self):
        self.assertEqual(coach.bands_text(185, None), "Recovery under 81 W, Easy 81\u2013124 W, "
                         "Moderate 124\u2013165 W, Challenging 165\u2013209 W, Max over 209 W")
        self.assertTrue(coach.bands_text(None, 100).startswith("Recovery under 80 W, Easy 80\u2013122 W"))
        self.assertEqual(coach.bands_text(None, None), "")

    def test_labels_by_percent_of_ftp(self):
        c = IntervalCoach(ftp=185)
        for watts, label in ((70, "Recovery"), (100, "Easy"), (145, "Moderate"), (185, "Challenging"),
                             (230, "Max")):
            self.assertEqual(c.intensity(watts), label, watts)

    def test_no_time_limit_with_an_ftp(self):
        self.assertEqual(IntervalCoach(ftp=185).max_effort_s, float("inf"))
        self.assertEqual(IntervalCoach().max_effort_s, 300)
        r = Rower(ftp=185)
        r.row(60, 105)
        r.row(400, 150)  # about 19 minutes at Moderate
        r.row(30, 105)
        self.assertEqual(len(r.summaries), 1)
        self.assertEqual(r.summaries[0].as_dict()["intensity"], "Moderate")
        self.assertGreater(r.summaries[0].duration_s, 18 * 60)

    def test_easy_and_recovery_in_state(self):
        r = Rower(ftp=185)
        r.row(60, 105)
        self.assertEqual(r.state()["intensity"], "Easy")
        r.row(20, 60)
        self.assertEqual(r.state()["intensity"], "Recovery")
        self.assertIsNone(r.state()["cue"])  # no cues in Recovery


def saved_stroke(length=115.0, curve=STEADY):
    return {"curve": list(map(float, curve)), "back_share": back_share(curve), "length_cm": length, "strokes": 80}


class EasyCueTest(unittest.TestCase):
    def test_nothing_saved_learns_todays_fresh_stroke(self):
        r = Rower(ftp=185)
        r.row(40, 105)  # the warm-up: no Easy cues in the first 2 minutes
        self.assertIsNone(r.state()["cue"])
        self.assertTrue(r.state()["warming_up"])
        r.row(40, 105)  # 30 strokes after 2:00: a provisional reference
        self.assertEqual(r.state()["cue"], "good")
        self.assertEqual(r.state()["easy_reference"], "today")
        r.row(10, 105, FADED)
        self.assertEqual(r.state()["cue"], "finish")
        r.row(10, 105, length=110)
        self.assertEqual(r.state()["cue"], "length")

    def test_last_sessions_stroke_from_the_start(self):
        r = Rower(ftp=185)
        r.coach = IntervalCoach(ftp=185, reference=lambda: saved_stroke())
        r.row(40, 105)
        self.assertIsNone(r.state()["cue"])  # still warming up
        self.assertEqual(r.state()["easy_reference"], "last session")
        r.row(10, 105)
        self.assertEqual(r.state()["cue"], "good")  # at 2:00, no learning needed
        r.row(10, 105, FADED)
        self.assertEqual(r.state()["cue"], "finish")

    def _calibrate(self, today_cm, saved_cm=115.0):
        r = Rower(ftp=185)
        r.coach = IntervalCoach(ftp=185, reference=lambda: saved_stroke(saved_cm))
        r.row(180, 105, length=today_cm)  # past 8:00
        return r.coach

    def test_length_calibration(self):
        c = self._calibrate(116)  # within 3 cm: last time's length stays
        self.assertEqual(c.baseline["length_cm"], 115)
        self.assertIsNone(c.length_note)
        self.assertEqual(c.session_summary()["fresh_easy"]["length_cm"], 115)
        c = self._calibrate(121)  # clearly longer: the new reference, and saved for next time
        self.assertEqual(c.baseline["length_cm"], 121)
        self.assertIn("Fresh Easy strokes were 6 cm longer than last time: the new length reference.",
                      c.session_summary()["findings"])
        self.assertEqual(c.session_summary()["fresh_easy"]["length_cm"], 121)
        c = self._calibrate(110)  # clearly shorter: last time's stays, so the cue speaks up
        self.assertEqual(c.baseline["length_cm"], 115)
        summary = c.session_summary()
        self.assertIn("Fresh Easy strokes were 5 cm shorter than last time; the coach kept last time's length.",
                      summary["findings"])
        self.assertEqual((summary["fresh_easy"]["length_cm"], summary["fresh_easy"]["measured_length_cm"]),
                         (115, 110))

    def test_fresh_stroke_leaves_out_the_warm_up(self):
        r = Rower(ftp=185)
        r.row(40, 105, length=105)  # short warm-up strokes
        r.row(140, 105, length=118)
        fresh = r.coach.session_summary()["fresh_easy"]
        self.assertEqual(fresh["length_cm"], 118)
        self.assertGreaterEqual(fresh["strokes"], 60)

    def test_latest_fresh_easy_from_saved_sessions(self):
        import tempfile
        import time as _time
        from smartrow_bridge import sessions
        with tempfile.TemporaryDirectory() as d:
            now = _time.time()
            for days_ago, fresh in ((40, saved_stroke(110)), (10, saved_stroke(118)), (2, None)):
                started = now - days_ago * 86400
                record = {"id": sessions.session_id(started), "started_at": started, "fresh_easy": fresh}
                sessions.save(d, record)
            self.assertEqual(sessions.latest_fresh_easy(d, now)["length_cm"], 118)  # skips the one without
            self.assertIsNone(sessions.latest_fresh_easy(d, now + 25 * 86400))  # older than a month


class PeakTest(unittest.TestCase):
    def test_peak_position(self):
        curve = [0] * 24
        curve[6] = 100
        curve[19] = 10  # the drive ends at sample 19 (samples are spaced by distance)
        self.assertAlmostEqual(coach.peak_position(curve), 6 / 19 * 100)
        flat = [0] * 24
        flat[6] = flat[7] = flat[8] = 100  # a flat top: its middle
        flat[19] = 10
        self.assertAlmostEqual(coach.peak_position(flat), 7 / 19 * 100)
        self.assertEqual(coach.peak_position([0] * 24), 0.0)

    def test_peak_wanders_only_on_a_clear_change(self):
        base = dict(index=1, duration_s=60, strokes=20, power_w=150, stroke_rate_spm=22, length_cm=115, peak_n=600,
                    back_share=0.3, steady_back_share=0.3, steady_length_cm=115, curve=[1] * 24,
                    steady_curve=[1] * 24, first_third_back_share=0.3, last_third_back_share=0.3,
                    steady_peak_at_pct=35, steady_peak_spread_pct=3.0)
        self.assertFalse(coach.IntervalSummary(**base, peak_spread_pct=4.0).peak_wanders)  # within the scatter
        self.assertTrue(coach.IntervalSummary(**base, peak_spread_pct=7.0).peak_wanders)
        d = coach.IntervalSummary(**base, peak_spread_pct=7.0).as_dict()
        self.assertEqual((d["peak_spread_pct"], d["steady_peak_spread_pct"], d["peak_wanders"]), (7.0, 3.0, True))
        text = " ".join(coach.findings([{**d, "rating": "good"}]))
        self.assertIn("force peak landed less consistently from stroke to stroke in I1", text)


class SettingsTest(unittest.TestCase):
    def test_defaults_are_the_calibrated_values(self):
        d = coach.CoachSettings()
        self.assertEqual((d.start_ratio, d.end_ratio, d.max_effort_s), (1.2, 1.1, 300.0))
        self.assertEqual(d.cues, coach.SENSITIVITY["normal"])
        self.assertEqual(d.cues.finish_cue, 0.88)

    def test_bad_sensitivity_is_refused(self):
        with self.assertRaises(ValueError):
            coach.CoachSettings(sensitivity="extreme")

    def test_sensitivity_levels_are_ordered(self):
        low, normal, high = (coach.SENSITIVITY[k] for k in ("low", "normal", "high"))
        self.assertLess(low.finish_cue, normal.finish_cue)
        self.assertLess(normal.finish_cue, high.finish_cue)
        self.assertGreater(low.legs_cue, normal.legs_cue)
        self.assertGreater(normal.legs_cue, high.legs_cue)
        self.assertGreater(low.length_cue_pct, high.length_cue_pct)
        for t in (low, normal, high):  # hysteresis: clearing is always easier than cueing
            self.assertLess(t.finish_cue, t.finish_clear)
            self.assertGreater(t.legs_cue, t.legs_clear)
            self.assertGreater(t.length_cue_pct, t.length_clear_pct)

    def test_smaller_surges_with_a_lower_start(self):
        """+12 % surges: missed at the default 20 %, found at 10 %."""
        for start, found in ((20.0, 0), (10.0, 1)):
            r = Rower(coach.CoachSettings(start_pct=start, start_min_w=10))
            r.row(60, 110)
            r.row(21, 123, FULL)
            r.row(15, 110)
            self.assertEqual(len(r.summaries), found, start)

    def test_longer_pieces_with_a_higher_limit(self):
        """A 6-minute piece: a new steady pace by default, an interval with a 10-minute limit."""
        for limit, found in ((300.0, 0), (600.0, 1)):
            r = Rower(coach.CoachSettings(max_effort_s=limit))
            r.row(60, 110)
            r.row(126, 150, FULL)  # 6 minutes at 21 spm
            r.row(15, 110)
            self.assertEqual(len(r.summaries), found, limit)

    def test_high_sensitivity_speaks_up_sooner(self):
        """A mildly fading finish (-10 %): no cue at normal, a cue at high."""
        mild = [round(v * (0.82 if 11 <= i <= 19 else 1)) for i, v in enumerate(STEADY)]
        ratio = back_share(mild) / back_share(STEADY)
        self.assertTrue(0.88 < ratio < 0.91, ratio)
        for sensitivity, cue in (("normal", "good"), ("high", "finish")):
            r = Rower(coach.CoachSettings(sensitivity=sensitivity))
            r.row(60, 110)
            r.row(12, 152, mild)
            self.assertEqual(r.state()["live"]["cue"], cue, sensitivity)

    def test_session_history_in_state(self):
        r = Rower()
        r.row(60, 110)
        for _ in range(3):
            r.row(21, 152, FULL)
            r.row(25, 110)
        history = r.state()["history"]
        self.assertEqual([h["index"] for h in history], [1, 2, 3])
        self.assertNotIn("curve", history[0])  # the list stays small: no curves


if __name__ == "__main__":
    unittest.main()
