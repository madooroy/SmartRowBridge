import re
import struct
import unittest
from pathlib import Path

from smartrow_bridge import advertising, ftms, protocol
from smartrow_bridge.gatt_spec import short_uuid
from smartrow_bridge.protocol import RowingMetrics, SmartRowParser


CAPTURE_2 = Path(__file__).resolve().parent.parent / "captures" / "sniff2.log"
_LOG_LINE = re.compile(r"^\s*([\d.]+)\s+0x000C 1236\s+len=17\s+([0-9a-f ]+?)\s+\|")


def load_capture(path: Path):
    """(time, raw 17-byte record) pairs from a tools/sniff_pulley.py log."""
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _LOG_LINE.match(line)
        if m:
            yield float(m.group(1)), bytes.fromhex(m.group(2).replace(" ", ""))


def real(ascii16: str) -> bytes:
    """A notification captured from the user's V3.10 pulley (16 visible chars + CR)."""
    return ascii16.encode() + b"\r"


# Captured 2026-10-01 from a V3.10 pulley, rowing at ~3:35/500 m.
C_349 = real("c@@@BH  0  349FD")
C_372 = real("c@@PCA  0  37203")
D_266 = real("d@@@BH266   3-0C")
D_230 = real("d@@PCA230   4-0E")
E_337 = real("e@@@BG000337 w72")
E_331 = real("e@@PCB000331 w78")
A_12 = real("a@@@BH00012   FE")
X_CURVE = real("x@@PBE&(*,-.!!10")
# Later in the same session, mid-stroke.
C_45 = real("c@@@GB 45  35815")
D_382 = real("d@@@GB382   9-10")
E_318 = real("e@@@GB318335 w7C")
A_33 = real("a@@@GC00033   01")


class ParserTest(unittest.TestCase):
    def test_power_instant_and_average(self):
        p = SmartRowParser()
        self.assertTrue(p.feed(C_349, now=10.0))  # first stroke: no instantaneous value yet
        self.assertEqual((p.metrics.power_w, p.metrics.avg_power_w), (0, 35))
        p.feed(C_45, now=11.0)
        self.assertEqual((p.metrics.power_w, p.metrics.avg_power_w), (45, 36))
        self.assertEqual(p.metrics.distance_m, 72)

    def test_v3_distance_is_unscrambled(self):
        p = SmartRowParser()
        p.feed(X_CURVE)
        self.assertEqual(p.metrics.distance_m, 25)
        p.feed(C_372)
        self.assertEqual(p.metrics.distance_m, 31)

    def test_stroke_rate_and_count(self):
        p = SmartRowParser()
        p.feed(D_266, now=1.0)
        self.assertAlmostEqual(p.metrics.stroke_rate_spm, 26.6)
        self.assertEqual(p.metrics.stroke_count, 3)
        p.feed(D_230, now=2.0)
        self.assertAlmostEqual(p.metrics.stroke_rate_spm, 23.0)
        self.assertEqual(p.metrics.stroke_count, 4)
        p.feed(D_382, now=3.0)
        self.assertAlmostEqual(p.metrics.stroke_rate_spm, 38.2)
        self.assertEqual(p.metrics.stroke_count, 9)

    def test_pace_instant_and_average(self):
        p = SmartRowParser()
        p.feed(E_337)
        self.assertEqual((p.metrics.pace_s_per_500m, p.metrics.avg_pace_s_per_500m), (0, 217))
        p.feed(E_331)
        self.assertEqual(p.metrics.avg_pace_s_per_500m, 211)
        p.feed(E_318)
        self.assertEqual((p.metrics.pace_s_per_500m, p.metrics.avg_pace_s_per_500m), (198, 215))

    def test_elapsed(self):
        p = SmartRowParser()
        p.feed(A_12)
        self.assertEqual(p.metrics.elapsed_s, 12)
        p.feed(A_33)
        self.assertEqual(p.metrics.elapsed_s, 33)

    @unittest.skipUnless(CAPTURE_2.exists(), "captures/sniff2.log not present")
    def test_full_session_capture(self):
        """Replay the 2½-minute session (easy, hard, steady, then stop) from captures/sniff2.log."""
        p = SmartRowParser()
        records = list(load_capture(CAPTURE_2))
        self.assertGreater(len(records), 1400)
        at = {}
        for t, raw in records:
            self.assertTrue(p.feed(raw, now=t), raw)
            for mark in (40.1, 52.9, 111.9, 150.0):
                if mark not in at and t >= mark:
                    at[mark] = p.snapshot(now=t)
        self.assertEqual(p.bad_checksums, 0)

        hard = at[40.1]  # hard strokes
        self.assertEqual((hard.power_w, hard.pace_s_per_500m), (264, 109))  # 264 W at 1:49
        self.assertAlmostEqual(hard.stroke_rate_spm, 26.4)
        self.assertEqual(at[52.9].elapsed_s, 61)  # "00101" = 1:01, not 101 s
        self.assertEqual(at[111.9].elapsed_s, 120)  # "00200" = 2:00

        idle = at[150.0]  # ~28 s after the last stroke; pulley still repeats "181 W"
        self.assertEqual(p.metrics.power_w, 181)
        self.assertTrue(idle.stopped)
        self.assertEqual((idle.power_w, idle.stroke_rate_spm, idle.pace_s_per_500m), (0, 0.0, 0))
        self.assertEqual((idle.distance_m, idle.stroke_count, idle.avg_power_w), (472, 44, 148))

    def test_force_curve_decoding(self):
        self.assertEqual(protocol.decode_force(b"!$%(+.MU"), [0, 48, 64, 112, 160, 208, 704, 832])

    def test_force_curve_assembled_per_stroke(self):
        p = SmartRowParser()
        strokes = []
        p.on_stroke = strokes.append
        for raw in (real("x@@AAI,18=BFIMB3"), real("y@@AAINNMLJIFB14"), real("z@@AAI>:50,'!!37")):
            p.feed(raw, now=32.7)
        self.assertEqual(strokes, [])  # waits for the stroke's 'b' record
        p.feed(real("b@@ABA 54341232C"), now=32.9)
        self.assertEqual(len(strokes), 1)
        s = strokes[0]
        self.assertEqual(len(s.force_n), protocol.FORCE_SAMPLES)
        self.assertEqual(s.force_n[:3], [176, 256, 368])
        self.assertEqual(max(s.force_n), 720)
        self.assertEqual(s.force_n[-2:], [0, 0])
        self.assertEqual((s.work_j, s.stroke_length_cm, s.time), (543.4, 123, 32.9))
        p.feed(real("b@@ABB 54341232D"), now=33.4)  # repeats of the same 'b' add nothing
        self.assertEqual(len(strokes), 1)

    def test_incomplete_curve_is_dropped(self):
        p = SmartRowParser()
        strokes = []
        p.on_stroke = strokes.append
        p.feed(real("y@@AAINNMLJIFB14"))
        p.feed(real("z@@AAI>:50,'!!37"))
        p.feed(real("b@@ABA 54341232C"))
        self.assertEqual(strokes, [])

    def test_stroke_handler_errors_do_not_break_parsing(self):
        p = SmartRowParser()

        def boom(_):
            raise RuntimeError("screen bug")

        p.on_stroke = boom
        for raw in (real("x@@AAI,18=BFIMB3"), real("y@@AAINNMLJIFB14"), real("z@@AAI>:50,'!!37")):
            p.feed(raw)
        with self.assertLogs("smartrow_bridge.protocol", "ERROR"):
            self.assertTrue(p.feed(real("b@@ABA 54341232C")))
        self.assertEqual(p.metrics.stroke_length_cm, 123)

    @unittest.skipUnless(CAPTURE_2.exists(), "captures/sniff2.log not present")
    def test_force_curves_in_capture(self):
        """Every stroke of the captured session yields a curve whose integral is within ~10 % of its work."""
        p = SmartRowParser()
        strokes = []
        p.on_stroke = strokes.append
        for t, raw in load_capture(CAPTURE_2):
            p.feed(raw, now=t)
        self.assertEqual(len(strokes), 43)
        hard = [s for s in strokes if s.work_j > 500]
        self.assertGreater(len(hard), 5)
        for s in hard:
            integral = sum(s.force_n) * s.stroke_length_cm / 100 / protocol.FORCE_SAMPLES
            self.assertAlmostEqual(integral / s.work_j, 1.0, delta=0.12)

    def test_stop_marker(self):
        p = SmartRowParser()
        p.feed(D_230, now=1.0)
        p.feed(C_45, now=1.1)
        self.assertEqual(p.snapshot(now=1.2).power_w, 45)
        p.feed(real("f@@@@@---00!--15"[:14] + "%02X" % (sum(b"f@@@@@---00!--") & 0xFF)), now=1.5)
        self.assertEqual(p.snapshot(now=1.6).power_w, 0)
        p.feed(D_382, now=2.0)  # a new stroke resumes live values
        self.assertEqual(p.snapshot(now=2.1).power_w, 45)

    def test_bad_checksum_rejected(self):
        corrupted = bytearray(C_349)
        corrupted[11] = ord("9")
        p = SmartRowParser()
        self.assertFalse(p.feed(bytes(corrupted)))
        self.assertEqual(p.bad_checksums, 1)
        self.assertEqual(p.metrics.power_w, 0)

    def test_rejects_short_and_unknown_packets(self):
        p = SmartRowParser()
        self.assertFalse(p.feed(b"c0123"))
        self.assertFalse(p.feed(b"Q" * 17))

    def test_keylock_marks_v3(self):
        p = SmartRowParser()
        self.assertFalse(p.feed(b"\rKEYLOCK=CBFF31C4\r"))
        self.assertTrue(p.v3)

    def test_snapshot_zeroes_when_idle(self):
        p = SmartRowParser()
        p.feed(D_382, now=100.0)  # a stroke
        p.feed(C_45, now=100.1)
        self.assertEqual(p.snapshot(now=101.0).power_w, 45)
        p.feed(D_382, now=104.0)  # same stroke count repeated: no new stroke
        p.feed(C_45, now=104.1)
        idle = p.snapshot(now=110.0)
        self.assertEqual((idle.power_w, idle.avg_power_w), (0, 36))  # averages survive a pause
        self.assertEqual(p.metrics.power_w, 45)  # snapshot must not mutate state


class KeylockTest(unittest.TestCase):
    def test_v3_detected_from_real_version_reply(self):
        # Captured from a V3.10 pulley after the init command.
        reply = bytes.fromhex("0d0d56400d536172 74526f77202756332e313027".replace(" ", ""))
        self.assertTrue(protocol.is_v3_version_reply(reply))
        self.assertFalse(protocol.is_v3_version_reply(b"\r"))

    def test_valid_challenge(self):
        # checksum: sum("KEYLOCK=001A2B") = 0x395 -> "95"; seed 0x1A2B * 17923 / 256 = 0x072810
        self.assertEqual(protocol.keylock_response(b"KEYLOCK=001A2B95\r\n"), b"\r2810\r")

    def test_real_challenge_accepted_by_pulley(self):
        # The pulley streamed data after receiving this answer, so it is known-good.
        self.assertEqual(protocol.keylock_response(b"\rKEYLOCK=CBFF31C4\r"), b"\rca63\r")

    def test_bad_checksum_requests_retry(self):
        self.assertEqual(protocol.keylock_response(b"KEYLOCK=001A2B00"), b"#")


class FtmsTest(unittest.TestCase):
    def test_rower_data_layout(self):
        m = RowingMetrics(distance_m=1234, power_w=187, avg_power_w=150, stroke_rate_spm=24.5,
                          stroke_count=42, pace_s_per_500m=125, avg_pace_s_per_500m=130, elapsed_s=300)
        data = ftms.encode_rower_data(m)
        flags, rate, count = struct.unpack_from("<HBH", data, 0)
        self.assertEqual(flags, 0x087C)
        self.assertEqual(rate, 49)
        self.assertEqual(count, 42)
        self.assertEqual(int.from_bytes(data[5:8], "little"), 1234)
        self.assertEqual(struct.unpack_from("<HHhhH", data, 8), (125, 130, 187, 150, 300))
        self.assertLessEqual(len(data), 20)  # must fit one notification at the default MTU of 23

    def test_control_point(self):
        self.assertEqual(ftms.control_point_response(b"\x00"), b"\x80\x00\x01")
        self.assertEqual(ftms.control_point_response(b"\x05\x10"), b"\x80\x05\x02")
        self.assertIsNone(ftms.control_point_response(b""))


class GattSpecTest(unittest.TestCase):
    def test_short_uuid(self):
        self.assertEqual(short_uuid("00001234-0000-1000-8000-00805F9B34FB"), 0x1234)
        self.assertEqual(short_uuid("00001236-0000-1000-8000-00805f9b34fb"), 0x1236)
        self.assertIsNone(short_uuid("12341234-0000-1000-8000-00805f9b34fb"))
        self.assertIsNone(short_uuid("da2b84f1-6279-48de-bdc0-afbea0226079"))


class AdvertisingTest(unittest.TestCase):
    PULLEY_MFR = {0x1235: b"07"}  # as advertised by the real pulley

    def test_fitness_set_never_mentions_smartrow(self):
        adv, rsp = advertising.fitness("PiRower")
        self.assertLessEqual(len(adv), 31)
        self.assertLessEqual(len(rsp), 31)
        self.assertIn(b"\x03\x03" + struct.pack("<H", 0x1826), adv)  # FTMS only
        self.assertIn(b"\x09PiRower", adv)
        for payload in (adv, rsp):
            self.assertNotIn(b"SmartRow", payload)
            self.assertNotIn(struct.pack("<H", 0x1234), payload)

    def test_smartrow_set_matches_pulley(self):
        adv, rsp = advertising.smartrow("SmartRow", self.PULLEY_MFR)
        self.assertIn(b"\x03\x03" + struct.pack("<H", 0x1234), adv)
        self.assertNotIn(struct.pack("<H", 0x1826), adv)
        self.assertIn(b"\x09SmartRow", adv)
        self.assertIn(b"\x05\xff" + struct.pack("<H", 0x1235) + b"07", rsp)

    def test_smartrow_set_without_pulley_id(self):
        adv, rsp = advertising.smartrow("SmartRow", {})
        self.assertIn(b"\x03\x03" + struct.pack("<H", 0x1234), adv)
        self.assertIn(b"\x09SmartRow", adv)
        for payload in (adv, rsp):
            self.assertNotIn(b"\xff" + struct.pack("<H", 0x1235), payload)

    def test_combined_fallback_fits(self):
        adv, rsp = advertising.combined("SmartRow 12345678901234567890", self.PULLEY_MFR)
        self.assertLessEqual(len(adv), 31)
        self.assertLessEqual(len(rsp), 31)
        self.assertIn(struct.pack("<HH", 0x1826, 0x1234), adv)


if __name__ == "__main__":
    unittest.main()
