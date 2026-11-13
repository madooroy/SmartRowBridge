# SPDX-License-Identifier: GPL-3.0-only
"""Optional seat sensor: a Benewake TF-Luna lidar behind the rails, aimed at a target on the seat.

The lidar is on a USB-to-serial adapter (CH340 or similar) plugged into the Pi. It sends
about 100 readings a second (lidar to target, cm), which are timed here on arrival with
time.monotonic(), the clock the strokes are timed with (protocol.StrokeCurve.time). So seat
position lines up with the pulley's strokes and drive flag to within a few ms.

SRB_SEAT_SENSOR: auto (default) = any USB serial device that sends TF-Luna frames; off; or a
port such as /dev/ttyUSB0. Plugged in or out at any time: it is looked for every few seconds.
Nothing else in the bridge depends on it; if it is missing or fails, the bridge carries on.

Each session's readings are saved beside its strokes as <id>.seat.csv (SeatWriter): one line
per reading, t (s from the session's first stroke, as the strokes file's t), cm, strength.
Readings from a few seconds before the first stroke are kept too (t negative).
"""

from __future__ import annotations

import glob
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

BAUD = 115200
FRAME_HEAD = b"\x59\x59"
FRAME_LEN = 9
WEAK_SIGNAL = 100  # Benewake: strength below 100 = distance unreliable
SATURATED = 65535  # too much light: distance unreliable
BUFFER_READINGS = 90_000  # ~15 min at 100 readings/s: covers a session's first MIN_STROKES strokes
PROBE_S = 1.5  # how long a port is listened to before deciding it is not a TF-Luna
PROBE_FRAMES = 3  # good frames needed in that time
RETRY_S = 10.0  # look for the lidar again this often while it isn't there
LEAD_S = 5.0  # readings kept from before a session's first stroke
SEAT_KEEP = 100  # newest seat files kept (~3 MB per 30 min each)
BY_ID = "/dev/serial/by-id/*"


class FrameParser:
    """TF-Luna's default 9-byte frame: 59 59 DistL DistH AmpL AmpH TempL TempH Checksum."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.bad_checksum = 0

    def feed(self, data: bytes) -> list[tuple[int, int]]:
        """(distance_cm, strength) for every complete frame in data (with what came before)."""
        self.buf += data
        out = []
        while True:
            i = self.buf.find(FRAME_HEAD)
            if i < 0:
                del self.buf[:-1]  # a trailing 0x59 may start the next header
                break
            if i:
                del self.buf[:i]
            if len(self.buf) < FRAME_LEN:
                break
            f = self.buf[:FRAME_LEN]
            if sum(f[:8]) & 0xFF != f[8]:
                self.bad_checksum += 1
                del self.buf[:1]
                continue
            out.append((f[2] | f[3] << 8, f[4] | f[5] << 8))
            del self.buf[:FRAME_LEN]
        return out


def good(cm: int, strength: int) -> bool:
    return WEAK_SIGNAL <= strength < SATURATED and cm > 0


def _open_serial(port: str):
    import serial  # pyserial, imported here so the bridge runs without it when the sensor is off
    return serial.Serial(port, BAUD, timeout=0.05)


class SeatSensor:
    """Reads the lidar in its own thread. readings: (seq, monotonic time, cm, strength)."""

    def __init__(self, setting: str = "auto", opener: Callable = _open_serial,
                 ports: Callable[[], list[str]] | None = None) -> None:
        self.setting = setting.strip() or "auto"
        self._open = opener
        self._ports = ports or (lambda: sorted(glob.glob(BY_ID)))
        self.readings: deque[tuple[int, float, int, int]] = deque(maxlen=BUFFER_READINGS)
        self.seq = 0  # readings so far (the last one's seq)
        self.port: str | None = None  # in use, while connected
        self.bad_checksum = 0
        self._lock = threading.Lock()  # the reading thread appends while the bridge reads
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ignored: set[str] = set()  # ports found not to be a TF-Luna (until unplugged)

    @property
    def connected(self) -> bool:
        return self.port is not None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="seat-sensor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def latest(self) -> tuple[int, float, int, int] | None:
        with self._lock:
            return self.readings[-1] if self.readings else None

    def since(self, seq: int, mono_from: float = float("-inf")) -> list[tuple[int, float, int, int]]:
        """Readings after seq (and not before mono_from), oldest first."""
        out = []
        with self._lock:
            for r in reversed(self.readings):  # newest first: stops at the first one already taken
                if r[0] <= seq or r[1] < mono_from:
                    break
                out.append(r)
        out.reverse()
        return out

    # --- the reading thread

    def _candidates(self) -> list[str]:
        if self.setting.lower() != "auto":
            return [self.setting]
        ports = self._ports()
        self._ignored &= set(ports)  # unplugged: look at it again if it comes back
        return [p for p in ports if p not in self._ignored]

    def _probe(self, port: str):
        """Open port and check it sends TF-Luna frames; returns (serial, parser) or None."""
        try:
            ser = self._open(port)
        except Exception as exc:  # noqa: BLE001
            log.debug("Seat sensor: can't open %s: %s", port, exc)
            return None
        parser = FrameParser()
        frames = 0
        end = time.monotonic() + PROBE_S
        try:
            while time.monotonic() < end and frames < PROBE_FRAMES:
                frames += len(parser.feed(ser.read(64)))
        except Exception:  # noqa: BLE001
            frames = 0
        if frames < PROBE_FRAMES:
            ser.close()
            return None
        return ser, parser

    def _run(self) -> None:
        warned = False
        while not self._stop.is_set():
            found = None
            for port in self._candidates():
                found = self._probe(port)
                if found:
                    break
                if self.setting.lower() == "auto":
                    self._ignored.add(port)
                    log.info("Seat sensor: %s is not a TF-Luna lidar; left alone", port)
            if not found:
                if self.setting.lower() != "auto" and not warned:
                    log.warning("Seat sensor: no TF-Luna readings on %s (looking again every %d s)",
                                self.setting, RETRY_S)
                    warned = True
                self._stop.wait(RETRY_S)
                continue
            ser, parser = found
            self.port = port
            warned = False
            log.info("Seat sensor: TF-Luna lidar on %s", os.path.realpath(port))
            try:
                self._read(ser, parser)
            except Exception as exc:  # noqa: BLE001 - unplugged, or the port failed
                log.warning("Seat sensor stopped (%s); looking for it again", exc)
            finally:
                self.port = None
                try:
                    ser.close()
                except Exception:  # noqa: BLE001
                    pass
            self._stop.wait(1.0)

    def _read(self, ser, parser: FrameParser) -> None:
        while not self._stop.is_set():
            waiting = getattr(ser, "in_waiting", 0) or 0
            data = ser.read(max(1, waiting))
            now = time.monotonic()
            frames = parser.feed(data)
            if frames:
                with self._lock:
                    for cm, strength in frames:
                        self.seq += 1
                        self.readings.append((self.seq, now, cm, strength))
            self.bad_checksum = parser.bad_checksum


class SeatWriter:
    """Saves the seat readings of the session being recorded to <session dir>/<id>.seat.csv,
    adding what came in since the last call (the bridge calls update() once a second)."""

    HEADER = "t,cm,strength\n"

    def __init__(self, directory: str, sensor: SeatSensor) -> None:
        self.directory = Path(directory)
        self.sensor = sensor
        self._id: str | None = None
        self._t0 = 0.0
        self._seq = 0  # last reading written
        self._done: dict[str, int] = {}  # last reading written, per session (one may come back)
        self._failed = False

    def path(self, session: str) -> Path:
        return self.directory / f"{session}.seat.csv"

    def update(self, current: tuple[str, float] | None) -> int:
        """current: (session id, monotonic time of its first stroke), or None. Returns rows written."""
        if current is None:
            if self._id is not None:
                self._done[self._id] = self._seq
            self._id = None
            return 0
        session, t0 = current
        if session != self._id:
            if self._id is not None:
                self._done[self._id] = self._seq
            self._id, self._t0, self._seq = session, t0, self._done.get(session, 0)
            if not self._seq:
                self._prune()
        rows = self.sensor.since(self._seq, self._t0 - LEAD_S)
        if not rows:
            return 0
        path = self.path(session)
        try:
            new = not path.exists()
            with open(path, "a", encoding="ascii") as f:
                if new:
                    f.write(self.HEADER)
                f.writelines(f"{mono - self._t0:.3f},{cm},{strength}\n" for _, mono, cm, strength in rows)
            if new:
                os.chmod(path, 0o644)
        except OSError as exc:
            if not self._failed:
                log.warning("Could not save seat readings to %s: %s", path, exc)
            self._failed = True
            return 0
        self._failed = False
        self._seq = rows[-1][0]
        return len(rows)

    def _prune(self) -> None:
        files = sorted(self.directory.glob("*.seat.csv"))
        for old in files[:-SEAT_KEEP]:
            old.unlink(missing_ok=True)
