import struct
import unittest
from types import SimpleNamespace

from smartrow_bridge import coach, config, ftms, status
from smartrow_bridge.protocol import RowingMetrics, SmartRowParser
from tests.test_coach import FULL, SHORT, STEADY, Rower

try:
    from smartrow_bridge import heartrate
except ImportError:  # bleak is only installed with the bridge
    heartrate = None


@unittest.skipIf(heartrate is None, "bleak not installed")
class StrapTest(unittest.TestCase):
    def test_parse_measurement(self):
        p = heartrate.parse_measurement
        self.assertEqual(p(bytes([0x00, 72])), 72)  # uint8
        self.assertIsNone(p(bytes([0x01, 0x2C, 0x01])))  # 300 bpm: out of range
        self.assertEqual(p(bytes([0x01, 0x8C, 0x00])), 140)  # uint16
        self.assertEqual(p(bytes([0x16, 128, 0x10, 0x02])), 128)  # contact detected, with RR interval
        self.assertIsNone(p(bytes([0x04, 0])))  # contact supported but not detected: strap off
        self.assertIsNone(p(bytes([0x00])))
        self.assertIsNone(p(b""))

    def test_matches(self):
        dev = SimpleNamespace(address="AA:BB:CC:DD:EE:01", name=None)
        adv = SimpleNamespace(service_uuids=[heartrate.HR_SERVICE_UUID.upper()], local_name="COROS HRM 7C1")
        other = SimpleNamespace(service_uuids=[], local_name="SmartRow")
        self.assertTrue(heartrate.matches("auto", dev, adv))
        self.assertFalse(heartrate.matches("auto", dev, other))
        self.assertTrue(heartrate.matches("coros", dev, adv))
        self.assertTrue(heartrate.matches("aa:bb:cc:dd:ee:01", dev, adv))
        self.assertFalse(heartrate.matches("AA:BB:CC:DD:EE:02", dev, adv))


class FitnessAppTest(unittest.TestCase):
    def test_rower_data_with_heart_rate(self):
        m = RowingMetrics(distance_m=1234, power_w=187, avg_power_w=150, stroke_rate_spm=24.5,
                          stroke_count=42, pace_s_per_500m=125, avg_pace_s_per_500m=130, elapsed_s=300,
                          heart_rate_bpm=141)
        data = ftms.encode_rower_data(m, heart_rate=True)
        flags = struct.unpack_from("<H", data, 0)[0]
        self.assertEqual(flags, 0x087C | 0x0200)
        self.assertEqual(struct.unpack_from("<HHhhBH", data, 8), (125, 130, 187, 150, 141, 300))
        self.assertLessEqual(len(data), 20)
        self.assertEqual(ftms.encode_rower_data(m), ftms.encode_rower_data(m, heart_rate=False))  # off: as before
        self.assertEqual(ftms.encode_rower_data(RowingMetrics(), heart_rate=True)[:2], b"\x7c\x08")  # no reading
        self.assertEqual(struct.unpack("<II", ftms.encode_features(True))[0] & (1 << 10), 1 << 10)
        self.assertEqual(struct.unpack("<II", ftms.encode_features())[0] & (1 << 10), 0)

    def test_settings(self):
        cfg = config.parse([])
        self.assertEqual((cfg.heart_rate, cfg.heart_rate_to_fitness), ("off", True))
        cfg = config.parse(["--heart-rate", "COROS", "--no-heart-rate-to-fitness"])
        self.assertEqual((cfg.heart_rate, cfg.heart_rate_to_fitness), ("COROS", False))

    def test_status_carries_strap(self):
        s = status.build(status.CONNECTED, None, False, False, None, "PiRower", RowingMetrics(heart_rate_bpm=99),
                         rowing=False, now=1.0, heart_rate={"name": "COROS HRM", "connected": True})
        self.assertEqual(s["metrics"]["heart_rate_bpm"], 99)
        self.assertEqual(s["heart_rate"]["name"], "COROS HRM")

    def test_strokes_carry_heart_rate(self):
        from tests.test_protocol import real
        p = SmartRowParser()
        strokes = []
        p.on_stroke = strokes.append
        p.metrics.heart_rate_bpm = 133
        for raw in (real("x@@AAI,18=BFIMB3"), real("y@@AAINNMLJIFB14"), real("z@@AAI>:50,'!!37"),
                    real("b@@ABA 54341232C")):
            p.feed(raw)
        self.assertEqual(strokes[0].heart_rate_bpm, 133)


class HeartRateRower(Rower):
    """Heart rate follows effort: 110 at steady, climbing to 150 in an effort, back down after."""

    def __init__(self, drift_per_stroke: float = 0.0):
        super().__init__()
        self.hr = 110.0
        self.drift = drift_per_stroke
        self.base = 110.0

    def row(self, n, watts, curve=STEADY, length=115):
        from smartrow_bridge.protocol import StrokeCurve
        for _ in range(n):
            self.t += 60 / 21
            self.count += 1
            self.base += self.drift
            target = self.base + (watts - 110) * 0.9
            self.hr += (target - self.hr) * 0.15
            s = StrokeCurve(time=self.t, force_n=list(curve), stroke_rate_spm=21.0, stroke_length_cm=length,
                            work_j=watts * 60 / 21, stroke_count=self.count, heart_rate_bpm=round(self.hr))
            summary = self.coach.add(s)
            if summary:
                self.summaries.append(summary)


class CoachHeartRateTest(unittest.TestCase):
    def test_no_strap_no_heart_rate(self):
        r = Rower()
        r.row(60, 110)
        r.row(21, 152, FULL)
        r.row(30, 110)
        summary = r.coach.session_summary()
        self.assertIsNone(summary["heart_rate"])
        self.assertNotIn("hr_peak", summary["intervals"][0])
        self.assertFalse([f for f in summary["findings"] if "Heart rate" in f])

    def test_interval_heart_rate_and_recovery(self):
        r = HeartRateRower()
        r.row(300, 110)  # 14 min: warm-up and steady
        r.row(42, 155, SHORT, 112)
        r.row(60, 110)
        summary = r.coach.session_summary()
        hr = summary["heart_rate"]
        i = summary["intervals"][0]
        self.assertGreater(i["hr_peak"], 140)
        self.assertGreater(i["hr_peak"], i["hr_avg"])
        self.assertGreater(i["hr_recovery"], 10)
        self.assertEqual(hr["max"], i["hr_peak"])
        self.assertAlmostEqual(hr["steady_avg"], 110, delta=2)
        self.assertAlmostEqual(hr["watts_per_beat"], 1.0, delta=0.03)
        text = " ".join(summary["findings"])
        self.assertIn(f"peaked at {hr['max']} (I1)", text)
        self.assertIn("in the first minute", text)

    def test_drift(self):
        flat = HeartRateRower()
        flat.row(800, 110)
        self.assertAlmostEqual(flat.coach.session_summary()["heart_rate"]["drift_pct"], 0, delta=1)
        drifting = HeartRateRower(drift_per_stroke=0.03)  # +24 bpm over the session at the same watts
        drifting.row(800, 110)
        summary = drifting.coach.session_summary()
        self.assertGreater(summary["heart_rate"]["drift_pct"], 5)
        self.assertIn("more heartbeats per watt", " ".join(summary["findings"]))

    def test_recovery_left_out_when_the_next_effort_starts(self):
        r = HeartRateRower()
        r.row(100, 110)
        r.row(21, 155, FULL)
        r.row(13, 110)  # about 37 s
        r.row(21, 155, FULL)
        r.row(40, 110)
        first = r.coach.session_summary()["intervals"][0]
        self.assertIsNone(first["hr_recovery"])


if __name__ == "__main__":
    unittest.main()
