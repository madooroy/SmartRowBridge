import unittest

from smartrow_bridge import rhythm
from smartrow_bridge.intervals import effort_line
from tests.test_protocol import CAPTURE_DRIVE, load_capture


def row(t, spm, flag=0.0, rec=0.0, handle=0.0):
    """A stroke-log row: (time, distance, power, spm, hr, length, force, drive, flag drive, flag recovery)."""
    return (t, 0, 110.0, spm, 0, 120, (0,) * 24, handle, flag, rec)


class RhythmTest(unittest.TestCase):
    def test_drive_from_flag_matches_the_video(self):
        # 4 Nov medians, both filmed rows: (flag, spm, video drive)
        for flag, spm, video in ((0.90, 20, 1.40), (1.20, 20, 1.69), (0.90, 18.2, 1.44), (0.72, 24.1, 1.11),
                                 (0.70, 28.6, 1.00), (0.70, 29.9, 0.96)):
            self.assertAlmostEqual(rhythm.drive_from_flag(flag, spm), video, delta=0.08)

    def test_stroke_from_flag_and_from_handle(self):
        d, r, src = rhythm.stroke_rhythm(row(0, 20, flag=0.9, rec=2.1))
        self.assertEqual(src, "pulley")
        self.assertAlmostEqual(d + r, 3.0)
        self.assertAlmostEqual(d, 0.9 + 0.92 - 0.022 * 20, places=2)
        d, r, src = rhythm.stroke_rhythm(row(0, 20, handle=1.0))
        self.assertEqual((round(d, 2), round(r, 2), src), (1.0, 2.0, "handle"))
        self.assertIsNone(rhythm.stroke_rhythm(row(0, 20)))
        self.assertIsNone(rhythm.stroke_rhythm(row(0, 20, flag=0.9, rec=6.0)))  # cycle disagrees with the rate
        self.assertIsNone(rhythm.stroke_rhythm((0, 0, 110.0, 20, 0, 120, (0,) * 24, 0.0)))  # an older tuple

    def test_session_easy_and_efforts(self):
        log = [row(t * 3.0, 20, flag=0.9, rec=2.1) for t in range(100)]               # 0-300 s Easy
        log += [row(300 + t * 2.5, 24, flag=0.8, rec=1.7) for t in range(24)]         # effort 300-360 s
        log += [row(360 + t * 3.0, 20, flag=0.9, rec=2.1) for t in range(40)]
        efforts = [{"index": 1, "started_at_s": 300.0, "duration_s": 60}]
        rh = rhythm.session_rhythm(log, efforts)
        self.assertEqual(rh["easy"]["spm"], 20)
        d = rhythm.drive_from_flag(0.9, 20)
        self.assertEqual(rh["easy"]["ratio"], round((3.0 - d) / d, 2))
        self.assertEqual(rh["efforts"][0]["index"], 1)
        self.assertEqual(rh["efforts"][0]["spm"], 24)
        # Easy leaves out the warm-up (40 strokes before 120 s), the one 5 s before the effort,
        # the effort, and the 12 within 35 s after it
        self.assertEqual(rh["easy"]["strokes"], (100 - 40 - 1) + (40 - 12))
        text = rhythm.rhythm_findings(rh)
        self.assertEqual(len(text), 2)
        self.assertIn("drive estimated from the pulley", text[0])
        self.assertIn("usual aim of 1:2 means a drive of about 1.0 s", text[0])
        self.assertIn("1:1.2", text[0])
        self.assertNotIn("estimated", text[1])
        self.assertTrue(text[1].startswith("Rhythm in efforts: I1 1:"))

    def test_close_to_aim_and_no_aim_at_high_rate(self):
        easy = {"strokes": 50, "spm": 20, "drive_s": 1.0, "recovery_s": 2.0, "ratio": 2.0, "source": "handle"}
        self.assertIn("Close to the usual aim", rhythm.rhythm_findings({"easy": easy, "efforts": []})[0])
        fast = {**easy, "spm": 28, "ratio": 1.1}
        self.assertNotIn("aim", rhythm.rhythm_findings({"easy": fast, "efforts": []})[0])

    def test_rough_above_32_spm(self):
        e = {"index": 1, "strokes": 30, "spm": 34, "drive_s": 1.2, "recovery_s": 0.9, "ratio": 0.75, "source": "pulley"}
        self.assertIn("(drive 1.2 s, rough)", rhythm.rhythm_findings({"easy": None, "efforts": [e]})[0])
        self.assertNotIn("rough", rhythm.rhythm_findings({"easy": None, "efforts": [{**e, "spm": 28}]})[0])
        self.assertNotIn("rough", rhythm.rhythm_findings({"easy": None, "efforts": [{**e, "source": "handle"}]})[0])

    def test_nothing_without_drive_times(self):
        log = [row(t * 3.0, 20) for t in range(100)]
        self.assertIsNone(rhythm.session_rhythm(log, []))
        self.assertEqual(rhythm.rhythm_findings(None), [])

    def test_strava_effort_line(self):
        i = {"index": 2, "intensity": "Moderate", "duration_s": 90, "power_w": 142, "rating": "good",
             "rhythm": {"ratio": 1.04}}
        self.assertIn("rhythm 1:1.0", effort_line(i))
        self.assertNotIn("rhythm", effort_line({k: v for k, v in i.items() if k != "rhythm"}))

    @unittest.skipUnless(CAPTURE_DRIVE.exists(), "capture not present")
    def test_live_path_reaches_the_session_record(self):
        from smartrow_bridge.coach import IntervalCoach
        from smartrow_bridge.protocol import SmartRowParser
        p, c = SmartRowParser(), IntervalCoach(ftp=185)
        p.on_stroke = c.add
        for t, raw in load_capture(CAPTURE_DRIVE):
            p.feed(raw, now=t)
        s = c.session_summary()
        self.assertTrue(s["rhythm"]["efforts"])
        self.assertTrue(all("rhythm" in i for i in s["intervals"] if i["strokes"] >= rhythm.MIN_STROKES))
        self.assertTrue(any(f.startswith("Rhythm in") for f in s["findings"]))
        self.assertIn("rhythm", s["thresholds"])
