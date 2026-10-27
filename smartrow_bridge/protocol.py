# SPDX-License-Identifier: GPL-3.0-only
"""SmartRow pulley BLE protocol: UUIDs, packet decoding and the V3 KEYLOCK handshake.

The pulley exposes one custom service (0x1234) with a write characteristic
(0x1235) and a notify characteristic (0x1236). Notifications are 17-byte ASCII
records whose first byte is the record type, bytes 1-5 are the distance in
metres, bytes 14-15 a hex checksum and byte 16 a CR.

Credit: the service and characteristic IDs, the init command, the record layout
and the V3 handshake come from qdomyos-zwift by Roberto Viola (GPL-3.0),
https://github.com/cagnulein/qdomyos-zwift, file
src/devices/smartrowrower/smartrowrower.cpp; keylock_response() below is a
Python port of its calculateSmartRowV3ChallengeResponse(). The checksum, the
stop marker, the elapsed-time format and the average fields were worked out
from captures of a V3.10 pulley.

Nothing in this module modifies the raw bytes that are forwarded to the
SmartRow app; decoding always works on a copy.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

log = logging.getLogger(__name__)

SERVICE_UUID = "00001234-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID = "00001235-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR_UUID = "00001236-0000-1000-8000-00805f9b34fb"
SERVICE_UUID_16 = 0x1234

INIT_COMMAND = bytes([0x24, 0x0D, 0x56, 0x40, 0x0D])  # "$\rV@\r"
POLL_COMMAND = b"$"
KEYLOCK_RETRY = b"#"
CHALLENGE_REQUEST = b"#"  # V3 firmware: ask for a KEYLOCK challenge after the version reply


def is_v3_version_reply(notification: bytes) -> bool:
    """The init command is answered with e.g. "SmartRow 'V3.10'"; V3 needs the KEYLOCK handshake."""
    return b"V3." in notification

PACKET_LENGTH = 17
PACKET_TYPES = frozenset(b"abcdefxyz")


def _atoi(raw: bytes) -> int:
    """C atoi(): skip leading whitespace, optional sign, then leading digits."""
    s = raw.decode("ascii", "replace").lstrip()
    sign = 1
    if s[:1] in ("+", "-"):
        sign = -1 if s[0] == "-" else 1
        s = s[1:]
    digits = ""
    for ch in s:
        if not ch.isdigit():
            break
        digits += ch
    return sign * int(digits) if digits else 0


def decode_packet(packet: bytes) -> bytes:
    """Undo the V3 obfuscation of the distance field (bytes 1-5).

    V3 pulleys scramble the high nibble of the distance digits. The transform
    is the identity for plain ASCII digits, so it is safe on older pulleys too.
    """
    if len(packet) != PACKET_LENGTH or packet[0] not in PACKET_TYPES:
        return packet
    decoded = bytearray(packet)
    for i in range(1, 6):
        decoded[i] = (packet[i] & 0x0F) | 0x30
    return bytes(decoded)


def checksum_ok(packet: bytes) -> bool:
    """Bytes 14-15 are the hex of sum(raw bytes 0..13) & 0xFF (verified on V3.10 captures)."""
    try:
        return int(packet[14:16], 16) == sum(packet[:14]) & 0xFF
    except ValueError:
        return False


def keylock_response(notification: bytes) -> bytes:
    """Answer a V3 "KEYLOCK=..." challenge; returns b"#" to request a new one.

    Ported from qdomyos-zwift's smartrowrower::calculateSmartRowV3ChallengeResponse (GPL-3.0).
    """
    challenge = notification.strip()
    idx = challenge.find(b"KEYLOCK=")
    if idx >= 0:
        challenge = challenge[idx:]
    if len(challenge) < 16:
        return KEYLOCK_RETRY

    key = challenge[:14]
    checksum = challenge[14:16].upper()
    expected = f"{sum(key) & 0xFF:02X}".encode()
    if expected != checksum:
        return KEYLOCK_RETRY

    try:
        seed = int(challenge[10:14], 16)
    except ValueError:
        return KEYLOCK_RETRY

    response = (seed * 17923) // 256
    return b"\r" + f"{response & 0xFFFF:04x}".encode() + b"\r"


# Force curve: each stroke arrives as three records, 'x', 'y' and 'z', whose
# bytes 6-13 hold 8 samples each, in order from catch to finish (24 in all). A
# sample is one printable character: (code - '!') x 16 N, so '!' is no force.
# Worked out from captures/sniff2.log against a SmartRow app CSV export of the
# same pulley, whose force columns are exactly these values (all multiples of
# 16 N, up to 800 N) after a leading 0. The 'b' record that follows the 'z'
# belongs to the same stroke: its work matches the curve's integral over the
# stroke length to within about 10 %.
FORCE_UNIT_N = 16
FORCE_SAMPLES = 24


def decode_force(payload: bytes) -> list[int]:
    """Force samples (N) from the 8 data bytes of an x/y/z record."""
    return [max(0, c - 0x21) * FORCE_UNIT_N for c in payload]


@dataclass
class StrokeCurve:
    """One completed stroke: its force curve plus the metrics current when it finished."""

    time: float  # time.monotonic() when the stroke's 'b' record arrived
    force_n: list[int] = field(default_factory=list)  # FORCE_SAMPLES values, catch -> finish
    power_w: int = 0
    stroke_rate_spm: float = 0.0
    stroke_length_cm: int = 0
    work_j: float = 0.0
    stroke_count: int = 0
    heart_rate_bpm: int = 0  # from the bridge's own strap connection, 0 = none


@dataclass
class RowingMetrics:
    distance_m: int = 0
    power_w: int = 0
    stroke_rate_spm: float = 0.0
    stroke_count: int = 0
    pace_s_per_500m: int = 0
    avg_power_w: int = 0
    avg_pace_s_per_500m: int = 0
    elapsed_s: int = 0
    stroke_length_cm: int = 0
    last_stroke_time: float = 0.0  # time.monotonic() when the stroke count last went up
    stopped: bool = False  # pulley sent its "not rowing" marker ('f' record with '!')
    heart_rate_bpm: int = 0  # set by the bridge from a heart rate strap (heartrate.py), 0 = none


class SmartRowParser:
    """Turns the raw notification stream into live rowing metrics."""

    def __init__(self) -> None:
        self.metrics = RowingMetrics()
        self.v3 = False
        self.bad_checksums = 0
        self.on_stroke: Callable[[StrokeCurve], None] | None = None
        self._curve_parts: list[list[int]] = []
        self._pending_curve: list[int] | None = None

    def feed(self, packet: bytes, now: float | None = None) -> bool:
        """Consume one notification. Returns True if metrics changed."""
        if b"KEYLOCK" in packet:
            self.v3 = True
            return False
        if len(packet) != PACKET_LENGTH or packet[0] not in PACKET_TYPES:
            return False
        if not checksum_ok(packet):
            self.bad_checksums += 1
            return False

        now = time.monotonic() if now is None else now
        p = decode_packet(packet)
        m = self.metrics
        m.distance_m = _atoi(p[1:6])

        kind = chr(p[0])
        if kind == "a":
            # "00159" -> "00200": minutes [6:9], seconds [9:11]. A variant with ',' at
            # [11] repeats the current time with extra data and is harmless here.
            m.elapsed_s = _atoi(p[6:9]) * 60 + _atoi(p[9:11])
        elif kind == "b":
            # " 2630108": work 263.0 J [6:11], stroke length 108 cm [11:14].
            m.stroke_length_cm = _atoi(p[11:14])
            if self._pending_curve is not None:
                curve, self._pending_curve = self._pending_curve, None
                self._emit_stroke(curve, _atoi(p[6:11]) / 10.0, now)
        elif kind == "c":
            # " 45  358": instantaneous W [6:9], session average W x10 [9:14].
            # Matches the Concept2 pace->watts curve within ~2% from 45 to 264 W.
            m.power_w = _atoi(p[6:9])
            m.avg_power_w = round(_atoi(p[9:14]) / 10)
        elif kind == "d":
            m.stroke_rate_spm = _atoi(p[6:9]) / 10.0
            count = _atoi(p[9:13])
            if count != m.stroke_count:
                m.last_stroke_time = now
                m.stopped = False
            m.stroke_count = count
        elif kind == "e":
            # "318335": instantaneous split 3:18 [6:9], average split 3:35 [9:12] (m ss).
            m.pace_s_per_500m = _atoi(p[6:7]) * 60 + _atoi(p[7:9])
            m.avg_pace_s_per_500m = _atoi(p[9:10]) * 60 + _atoi(p[10:12])
        elif kind == "f":
            # "---00 1720" while rowing, "---00!--15" once the flywheel stops. The pulley
            # keeps repeating the last c/d/e values after that, so this is the stop signal.
            if p[11:12] == b"!":
                m.stopped = True
        elif kind == "x":
            self._curve_parts = [decode_force(p[6:14])]  # a repeated 'x' starts over
        elif kind in "yz" and len(self._curve_parts) == "xyz".index(kind):
            self._curve_parts.append(decode_force(p[6:14]))
            if kind == "z":
                self._pending_curve = [f for part in self._curve_parts for f in part]
                self._curve_parts = []
        return True

    def _emit_stroke(self, curve: list[int], work_j: float, now: float) -> None:
        if self.on_stroke is None or not any(curve):
            return
        m = self.metrics
        stroke = StrokeCurve(time=now, force_n=curve, power_w=m.power_w, stroke_rate_spm=m.stroke_rate_spm,
                             stroke_length_cm=m.stroke_length_cm, work_j=work_j, stroke_count=m.stroke_count,
                             heart_rate_bpm=m.heart_rate_bpm)
        try:
            self.on_stroke(stroke)
        except Exception:  # noqa: BLE001 - coaching must never break parsing
            log.exception("Stroke handler failed")

    def snapshot(self, stale_after: float = 6.0, now: float | None = None) -> RowingMetrics:
        """Current metrics, with live values zeroed once the rower stops.

        Stopped = the pulley's "not rowing" marker, or no new stroke for stale_after
        seconds (c/d records keep arriving while idle, so their arrival proves nothing).
        """
        now = time.monotonic() if now is None else now
        snap = replace(self.metrics)
        if snap.stopped or now - snap.last_stroke_time > stale_after:
            snap.power_w = 0
            snap.stroke_rate_spm = 0.0
            snap.pace_s_per_500m = 0
        return snap
