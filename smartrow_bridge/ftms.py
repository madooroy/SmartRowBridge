"""Bluetooth Fitness Machine Service (FTMS 1.0) encoding for a rower."""

from __future__ import annotations

import struct

from .protocol import RowingMetrics

FTMS_SERVICE_UUID_16 = 0x1826
FEATURE_UUID_16 = 0x2ACC
ROWER_DATA_UUID_16 = 0x2AD1
TRAINING_STATUS_UUID_16 = 0x2AD3
CONTROL_POINT_UUID_16 = 0x2AD9
MACHINE_STATUS_UUID_16 = 0x2ADA

# Rower Data flags. Bit 0 ("More Data") cleared means Stroke Rate + Stroke Count are present.
_TOTAL_DISTANCE = 1 << 2
_INSTANT_PACE = 1 << 3
_AVERAGE_PACE = 1 << 4
_INSTANT_POWER = 1 << 5
_AVERAGE_POWER = 1 << 6
_HEART_RATE = 1 << 9
_ELAPSED_TIME = 1 << 11
ROWER_DATA_FLAGS = (
    _TOTAL_DISTANCE | _INSTANT_PACE | _AVERAGE_PACE | _INSTANT_POWER | _AVERAGE_POWER | _ELAPSED_TIME
)

# Fitness Machine Feature bits.
_FEAT_CADENCE = 1 << 1
_FEAT_TOTAL_DISTANCE = 1 << 2
_FEAT_PACE = 1 << 5
_FEAT_HEART_RATE = 1 << 10
_FEAT_ELAPSED_TIME = 1 << 12
_FEAT_POWER = 1 << 14
MACHINE_FEATURES = _FEAT_CADENCE | _FEAT_TOTAL_DISTANCE | _FEAT_PACE | _FEAT_ELAPSED_TIME | _FEAT_POWER

_MACHINE_TYPE_ROWER = 1 << 4

# Control point.
_CP_RESPONSE = 0x80
_CP_SUCCESS = 0x01
_CP_NOT_SUPPORTED = 0x02
_CP_ACCEPTED = frozenset({0x00, 0x01, 0x07, 0x08})  # request control, reset, start/resume, stop/pause

TRAINING_STATUS_IDLE = bytes([0x00, 0x01])


def _clamp(value: float, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(round(value))))


def encode_rower_data(m: RowingMetrics, heart_rate: bool = False) -> bytes:
    """Rower Data (0x2AD1). With heart_rate and a reading, the heart rate field is included."""
    with_hr = heart_rate and m.heart_rate_bpm > 0
    flags = ROWER_DATA_FLAGS | (_HEART_RATE if with_hr else 0)
    return (
        struct.pack(
            "<HBH",
            flags,
            _clamp(m.stroke_rate_spm * 2, 0, 0xFF),  # 0.5 stroke/min resolution
            _clamp(m.stroke_count, 0, 0xFFFF),
        )
        + _clamp(m.distance_m, 0, 0xFFFFFF).to_bytes(3, "little")
        # Fields follow flag-bit order: inst pace, avg pace, inst power, avg power, [heart rate], elapsed.
        + struct.pack(
            "<HHhh",
            _clamp(m.pace_s_per_500m, 0, 0xFFFF),
            _clamp(m.avg_pace_s_per_500m, 0, 0xFFFF),
            _clamp(m.power_w, -0x8000, 0x7FFF),
            _clamp(m.avg_power_w, -0x8000, 0x7FFF),
        )
        + (bytes([_clamp(m.heart_rate_bpm, 0, 0xFF)]) if with_hr else b"")
        + struct.pack("<H", _clamp(m.elapsed_s, 0, 0xFFFF))
    )


def encode_features(heart_rate: bool = False) -> bytes:
    return struct.pack("<II", MACHINE_FEATURES | (_FEAT_HEART_RATE if heart_rate else 0), 0)


def advertising_service_data() -> bytes:
    """FTMS service data: flags (machine available) + machine type (rower)."""
    return bytes([0x01]) + struct.pack("<H", _MACHINE_TYPE_ROWER)


def control_point_response(request: bytes) -> bytes | None:
    if not request:
        return None
    op = request[0]
    result = _CP_SUCCESS if op in _CP_ACCEPTED else _CP_NOT_SUPPORTED
    return bytes([_CP_RESPONSE, op, result])
