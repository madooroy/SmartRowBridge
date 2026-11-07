import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from smartrow_bridge import seat, sessions
from smartrow_bridge.protocol import RowingMetrics


def frame(cm: int, strength: int = 3600, temp_c: float = 36.0) -> bytes:
    raw = int((temp_c + 256) * 8)
    f = bytes([0x59, 0x59, cm & 0xFF, cm >> 8, strength & 0xFF, strength >> 8, raw & 0xFF, raw >> 8])
    return f + bytes([sum(f) & 0xFF])


class FakePort:
    """A serial port sending the given bytes, then nothing (or failing, like an unplugged adapter)."""

    def __init__(self, data: bytes, then_fail: bool = False) -> None:
        self.data = data
        self.then_fail = then_fail
        self.closed = False

    @property
    def in_waiting(self) -> int:
        return len(self.data)

    def read(self, n: int) -> bytes:
        if not self.data:
            if self.then_fail:
                raise OSError("device disconnected")
            time.sleep(0.005)
            return b""
        out, self.data = self.data[:n], self.data[n:]
        return out

    def close(self) -> None:
        self.closed = True


class ParserTest(unittest.TestCase):
    def test_frames_split_anywhere_with_noise_and_bad_checksums(self):
        bad = bytearray(frame(10))
        bad[8] ^= 1
        stream = b"\x00\x59" + frame(57) + b"\x59\x59\x01" + frame(300, 50) + bytes(bad) + frame(120, 65535)
        p = seat.FrameParser()
        out = []
        for i in range(len(stream)):  # one byte at a time
            out += p.feed(stream[i:i + 1])
        self.assertEqual(out, [(57, 3600), (300, 50), (120, 65535)])
        self.assertGreaterEqual(p.bad_checksum, 1)
        self.assertTrue(seat.good(57, 3600))
        self.assertFalse(seat.good(300, 50))  # weak
        self.assertFalse(seat.good(120, 65535))  # saturated


class SensorTest(unittest.TestCase):
    def wait_for(self, check, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if check():
                return True
            time.sleep(0.01)
        return False

    def test_finds_the_lidar_and_leaves_other_ports_alone(self):
        ports = {"/dev/serial/by-id/usb-0000_modem": FakePort(b"hello modem\r\n" * 20),
                 "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0":
                     FakePort(b"".join(frame(cm) for cm in range(34, 120)))}
        s = seat.SeatSensor("auto", opener=lambda p: ports[p], ports=lambda: sorted(ports))
        with mock.patch.object(seat, "PROBE_S", 0.2):
            s.start()
            self.assertTrue(self.wait_for(lambda: s.latest() is not None and s.latest()[2] == 119))
            s.stop()
        self.assertIn("1a86", s.port or "")
        self.assertIn("/dev/serial/by-id/usb-0000_modem", s._ignored)
        self.assertTrue(ports["/dev/serial/by-id/usb-0000_modem"].closed)
        readings = s.since(0)
        cms = [r[2] for r in readings]
        self.assertEqual(cms, sorted(cms))  # in order
        self.assertEqual(cms[-1], 119)
        self.assertEqual([r[0] for r in readings], list(range(readings[0][0], s.seq + 1)))

    def test_unplugged_then_looked_for_again(self):
        opened = []

        def opener(port):
            opened.append(port)
            return FakePort(b"".join(frame(50) for _ in range(10)), then_fail=True)

        s = seat.SeatSensor("/dev/ttyUSB0", opener=opener)
        with mock.patch.object(seat, "PROBE_S", 0.2):
            s._stop.wait = lambda t: s._stop.is_set()  # no pauses between attempts
            s.start()
            self.assertTrue(self.wait_for(lambda: len(opened) >= 2))
            s.stop()

    def test_nothing_there(self):
        s = seat.SeatSensor("auto", opener=lambda p: None, ports=lambda: [])
        self.assertEqual(s._candidates(), [])
        self.assertFalse(s.connected)
        self.assertIsNone(s.latest())


class WriterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.sensor = seat.SeatSensor("off")
        self.writer = seat.SeatWriter(str(self.dir), self.sensor)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, mono: float, cm: int, strength: int = 3600):
        self.sensor.seq += 1
        self.sensor.readings.append((self.sensor.seq, mono, cm, strength))

    def rows(self, session: str) -> list[list[str]]:
        lines = self.writer.path(session).read_text().splitlines()
        self.assertEqual(lines[0], "t,cm,strength")
        return [line.split(",") for line in lines[1:]]

    def test_only_from_just_before_the_first_stroke_and_appended(self):
        for i in range(100):  # 10 s of readings before the first stroke at t0 = 1010
            self.add(1000 + i * 0.1, 118)
        t0 = 1010.0
        self.assertEqual(self.writer.update(None), 0)  # no session yet
        self.assertEqual(self.writer.update(("2026-11-07T1430", t0)), 50)  # the last LEAD_S only
        self.assertEqual(self.writer.update(("2026-11-07T1430", t0)), 0)  # nothing new
        self.add(1010.5, 40)
        self.assertEqual(self.writer.update(("2026-11-07T1430", t0)), 1)
        rows = self.rows("2026-11-07T1430")
        self.assertEqual(len(rows), 51)
        self.assertAlmostEqual(float(rows[0][0]), -5.0, places=3)
        self.assertEqual(rows[-1], ["0.500", "40", "3600"])
        self.assertEqual(stat.S_IMODE(os.stat(self.writer.path("2026-11-07T1430")).st_mode), 0o644)

    def test_a_session_that_comes_back_is_not_written_twice(self):
        self.add(1000.0, 100)
        self.writer.update(("2026-11-07T1430", 1000.0))
        self.writer.update(None)  # e.g. between sessions
        self.add(1001.0, 90)
        self.writer.update(("2026-11-07T1430", 1000.0))
        self.assertEqual([r[1] for r in self.rows("2026-11-07T1430")], ["100", "90"])

    def test_old_seat_files_are_pruned(self):
        for i in range(seat.SEAT_KEEP + 3):
            (self.dir / f"2026-01-01T{i:04d}.seat.csv").write_text("t,cm,strength\n")
        self.add(1000.0, 100)
        self.writer.update(("2026-11-07T1430", 1000.0))
        files = sorted(self.dir.glob("*.seat.csv"))
        self.assertEqual(len(files), seat.SEAT_KEEP + 1)  # the newest SEAT_KEEP before it, and it
        self.assertEqual(files[-1].name, "2026-11-07T1430.seat.csv")


class WithRecorderTest(unittest.TestCase):
    def test_seat_file_follows_the_session_and_goes_when_it_is_discarded(self):
        from tests.test_sessions import saturday
        r = saturday()
        with tempfile.TemporaryDirectory() as tmp:
            rec = sessions.SessionRecorder(tmp, r.coach)
            self.assertIsNone(rec.current())  # nothing saved yet
            rec.update(RowingMetrics(distance_m=6000), mono_now=r.t, wall_now=1_800_000_000.0)
            session, t0 = rec.current()
            self.assertEqual(t0, r.coach.stroke_log[0][0])
            strokes = json.loads((Path(tmp) / f"{session}.strokes.json").read_text())
            self.assertEqual(strokes["rows"][0][0], 0)  # the strokes file's t = 0 is t0 too
            sensor = seat.SeatSensor("off")
            sensor.seq = 1
            sensor.readings.append((1, t0 + 2.0, 60, 3600))
            writer = seat.SeatWriter(tmp, sensor)
            writer.update(rec.current())
            self.assertTrue((Path(tmp) / f"{session}.seat.csv").exists())
            rec.discard()
            self.assertIsNone(rec.current())
            self.assertFalse((Path(tmp) / f"{session}.seat.csv").exists())
            self.assertEqual(writer.update(rec.current()), 0)


if __name__ == "__main__":
    unittest.main()
