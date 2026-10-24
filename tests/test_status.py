import json
import tempfile
import unittest
from pathlib import Path

from smartrow_bridge import config, status
from smartrow_bridge.protocol import RowingMetrics


class StatusTest(unittest.TestCase):
    def test_pulley_label_uses_the_pulley_id(self):
        self.assertEqual(status.pulley_label("SmartRow", {0x1235: b"34"}), "SmartRow-34")
        self.assertEqual(status.pulley_label("SmartRow", {}), "SmartRow")
        self.assertEqual(status.pulley_label("SmartRow", {0x1235: b"\x01\x02"}), "SmartRow")
        self.assertEqual(status.pulley_label("SmartRow", {0x004C: b"34"}), "SmartRow")

    def test_build_keeps_display_fields_only(self):
        m = RowingMetrics(distance_m=812, power_w=151, stroke_rate_spm=24.5, stroke_count=88,
                          pace_s_per_500m=122, elapsed_s=215, last_stroke_time=5.0, stopped=False)
        s = status.build(status.CONNECTED, "SmartRow-34", True, False, "SmartRow", "PiRower", m,
                         rowing=True, now=100.0)
        self.assertEqual(s["stage"], "connected")
        self.assertEqual(s["time"], 100.0)
        self.assertEqual(s["metrics"]["power_w"], 151)
        self.assertNotIn("last_stroke_time", s["metrics"])
        self.assertNotIn("stopped", s["metrics"])
        self.assertIsNone(s["coach"])
        json.dumps(s)  # must be plain JSON

    def test_build_carries_coach_state(self):
        coach = {"mode": "steady", "steady_power_w": 110, "intervals": 0, "last": None, "show_summary": False}
        s = status.build(status.CONNECTED, None, False, False, None, "PiRower", RowingMetrics(), rowing=True,
                         now=1.0, coach=coach)
        self.assertEqual(s["coach"], coach)

    def test_write_then_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "run" / "status.json")  # directory created by write()
            s = status.build(status.SEARCHING, None, False, False, None, "PiRower", RowingMetrics(),
                             rowing=False, now=1.0)
            status.write(path, s)
            self.assertEqual(status.read(path), s)
            self.assertFalse(Path(path + ".tmp").exists())

    def test_read_missing_or_broken_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            self.assertIsNone(status.read(str(path)))
            path.write_text("{half")
            self.assertIsNone(status.read(str(path)))

    def test_setting(self):
        self.assertEqual(config.parse([]).status_file, status.DEFAULT_PATH)
        self.assertEqual(config.parse(["--status-file", "off"]).status_file, "off")
        self.assertFalse(config.parse([]).coach)  # experimental: off unless asked for
        self.assertTrue(config.parse(["--coach"]).coach)
        cfg = config.parse(["--coach-start-pct", "12", "--coach-max-min", "12", "--coach-sensitivity", "high"])
        self.assertEqual((cfg.coach_start_pct, cfg.coach_max_min, cfg.coach_sensitivity), (12.0, 12.0, "high"))


if __name__ == "__main__":
    unittest.main()
