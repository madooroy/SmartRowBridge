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
