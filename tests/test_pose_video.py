import json
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
except ImportError:  # the Pi's test run may not have numpy; the tool runs elsewhere
    np = None

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))


@unittest.skipIf(np is None, "numpy not installed")
class PoseVideoTest(unittest.TestCase):
    def setUp(self):
        import pose_video
        self.pv = pose_video

    def rowing(self, seconds=40.0, period=3.0, drive=1.2, fps=30.0, offset=0.0):
        """Synthetic joint signals: hands out at the catch, in at the finish; knees straighten over
        the drive; body from 20 deg forward to 35 back; knees rise 0.4 s into the recovery."""
        t = np.arange(0, seconds, 1 / fps)
        # strokes of slightly varying length, as real ones are (2.8-3.2 s)
        lengths = period + 0.2 * np.sin(np.arange(int(seconds / period) + 3) * 1.7)
        starts = np.concatenate([[0.0], np.cumsum(lengths)]) - offset
        k = np.searchsorted(starts, t, side="right") - 1
        ph = t - starts[k]
        period = lengths[k]
        d = np.clip(ph / drive, 0, 1)                      # 0..1 through the drive
        r = np.clip((ph - drive) / (period - drive), 0, 1)  # 0..1 through the recovery
        on_drive = ph < drive
        hands = np.where(on_drive, 700 - 400 * d, 300 + 400 * r)
        knee = np.where(on_drive, 55 + 110 * np.clip(d / 0.75, 0, 1),
                        np.where(r < 0.25, 165, 165 - 110 * np.clip((r - 0.25) / 0.75, 0, 1)))
        trunk = np.where(on_drive, 20 - 55 * np.clip((d - 0.3) / 0.7, 0, 1), -35 + 55 * np.clip(r / 0.6, 0, 1))
        elbow = np.where(on_drive, np.where(d < 0.8, 178, 178 - 128 * (d - 0.8) / 0.2), 50 + 128 * np.clip(r / 0.5, 0, 1))
        return t, {"hands": hands, "knee": knee, "trunk": trunk, "elbow": elbow, "shin": np.zeros_like(t)}

    def test_strokes_from_signals(self):
        t, S = self.rowing()
        rows = self.pv.strokes(t, S)
        self.assertGreaterEqual(len(rows), 11)
        med = self.pv.summary(rows, 1e9)[0]
        self.assertAlmostEqual(med["drive"], 1.2, delta=0.05)
        self.assertAlmostEqual(med["recovery"], 1.8, delta=0.05)
        self.assertAlmostEqual(med["catch_lean"], 20, delta=1)
        self.assertAlmostEqual(med["finish_lean"], -35, delta=1)
        self.assertLess(med["swing_vs_legs"], 0)  # back opens before the legs are done
        self.assertLess(med["lean_at_knees_up"], 0)  # knees rise with the body still back
        self.assertLess(med["elbow_at_knees_up"], 170)  # and the arms still bent

    def test_align_with_the_pulley(self):
        t, S = self.rowing(seconds=60)
        rows = self.pv.strokes(t, S)
        # the pulley's strokes: the same finishes, 250 s later in the session, 0.4 s after each finish,
        # with a little jitter and two strokes the video missed
        rng = np.random.default_rng(1)
        session = [r["finish_t"] + 250.4 + rng.normal(0, 0.05) for r in rows][2:]
        session += [100.0, 101.0, 102.5]  # earlier strokes, before the clip
        off, n = self.pv.align([r["finish_t"] for r in rows], session)
        self.assertAlmostEqual(off, 250.4, delta=0.1)
        self.assertEqual(n, len(rows) - 2)
        self.assertIsNone(self.pv.align([1.0, 2.0], session))

    def test_attach_copies_the_pulley_numbers(self):
        t, S = self.rowing(seconds=60)
        rows = self.pv.strokes(t, S)
        data = {"columns": ["t", "power_w", "spm", "length_cm", "flag_drive_s"],
                "rows": [[r["finish_t"] + 30.3, 120.0 + k, 20.0, 118, 0.8] for k, r in enumerate(rows)]}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.strokes.json"
            path.write_text(json.dumps(data))
            msg = self.pv.attach(rows, str(path), "clip")
        self.assertIn(f"{len(rows)} of {len(rows)} strokes paired", msg)
        self.assertEqual([r["power_w"] for r in rows], [120.0 + k for k in range(len(rows))])

    def test_peaks(self):
        x = np.array([0, 5, 1, 9, 1, 2, 1, 8, 0], dtype=float)
        self.assertEqual(self.pv.peaks(x, 1, 5), [3, 7])


@unittest.skipIf(np is None, "numpy not installed")
class PicturesTest(unittest.TestCase):
    def setUp(self):
        import pose_video
        self.pv = pose_video

    def test_typical_strokes_skip_efforts_and_spread_over_the_clip(self):
        rows = [{"t": float(i * 3), "catch_lean": 20.0, "finish_lean": -30.0, "lean_at_knees_up": 10.0 + (i % 3),
                 "knees_up_t": i * 3 + 2.0, "power_w": 110 if i % 7 else 190} for i in range(30)]
        chosen = self.pv.typical(rows)
        self.assertEqual(len(chosen), 3)
        self.assertTrue(all(r["power_w"] == 110 for r in chosen))  # not the efforts
        self.assertTrue(chosen[0]["t"] < chosen[1]["t"] < chosen[2]["t"])  # early, middle, late
        self.assertTrue(all(abs(r["lean_at_knees_up"] - 11.0) <= 1 for r in chosen))  # near each stretch's median
        self.assertEqual(self.pv.typical([]), [])

    def test_strokes_carry_the_knees_up_time(self):
        t = np.arange(0, 30, 1 / 30)
        ph = t % 3.0
        d, r = np.clip(ph / 1.2, 0, 1), np.clip((ph - 1.2) / 1.8, 0, 1)
        on = ph < 1.2
        S = {"hands": np.where(on, 700 - 400 * d, 300 + 400 * r),
             "knee": np.where(on, 55 + 110 * np.clip(d / 0.75, 0, 1),
                              np.where(r < 0.25, 165, 165 - 110 * np.clip((r - 0.25) / 0.75, 0, 1))),
             "trunk": np.where(on, 20 - 55 * d, -35 + 55 * r), "elbow": np.full_like(t, 170.0),
             "shin": np.zeros_like(t)}
        rows = self.pv.strokes(t, S)
        self.assertTrue(rows)
        for row in rows:
            self.assertGreater(row["knees_up_t"], row["finish_t"])
            self.assertLess(row["knees_up_t"] - row["finish_t"], 1.8)

    def test_picture_is_drawn_from_tracked_joints(self):
        try:
            import cv2
        except ImportError:
            self.skipTest("opencv not installed")
        with tempfile.TemporaryDirectory() as tmp:
            video = str(Path(tmp) / "clip.avi")
            out = cv2.VideoWriter(video, cv2.VideoWriter_fourcc(*"MJPG"), 30, (320, 180))
            for _ in range(150):
                out.write(np.full((180, 320, 3), 90, np.uint8))
            out.release()
            t = np.arange(0, 5, 1 / 30)
            marks = np.zeros((len(t), 33, 3))
            for i, (x, y) in {12: (400, 150), 14: (480, 220), 16: (560, 250), 24: (380, 380),
                              26: (560, 330), 28: (700, 420), 30: (700, 430), 32: (720, 430)}.items():
                marks[:, i] = (x, y, 0.9)
                marks[:, i - 1] = (x, y, 0.1)
            rows = [{"t": 1.0, "finish_t": 2.0, "knees_up_t": 3.0, "catch_lean": 20, "finish_lean": -30,
                     "lean_at_knees_up": 10, "power_w": 110.4}]
            path = Path(tmp) / "pics" / "pose-moments-clip.jpg"
            self.assertTrue(self.pv.pictures(video, t, marks, rows, path, seat_cm=lambda v: 48))
            img = cv2.imread(str(path))
            self.assertEqual(img.shape[:2], (self.pv.PANEL_H, 3 * self.pv.PANEL_W))  # one stroke, three moments
