"""Legacy (31-byte) advertising payloads for the virtual devices on hci1.

Normally the dongle runs one advertising set per device, each with its own address and name:
  * smartrow() - the pulley clone: service 0x1234 + the pulley's name and
    manufacturer data, for the SmartRow app;
  * fitness()  - a generic FTMS rower: service 0x1826 + rower service data and
    its own name, so Peloton never sees "SmartRow".
  * heart_rate() - optional: a standard heart-rate sensor (0x180D) relaying the
    bridge's strap, so a fitness app pairs heart rate with the Pi, not the strap.
combined() is the fallback for controllers without extended advertising.
"""

from __future__ import annotations

import struct

from . import ftms, protocol

MAX_AD_LEN = 31

_FLAGS = 0x01
_COMPLETE_16_BIT_UUIDS = 0x03
_SHORTENED_NAME = 0x08
_COMPLETE_NAME = 0x09
_SERVICE_DATA_16 = 0x16
_APPEARANCE = 0x19
_MANUFACTURER_DATA = 0xFF


def _ad(ad_type: int, data: bytes) -> bytes:
    return bytes([len(data) + 1, ad_type]) + data


_GENERAL_DISCOVERABLE = _ad(_FLAGS, bytes([0x06]))  # LE General Discoverable, BR/EDR not supported
_FTMS_SERVICE_DATA = _ad(
    _SERVICE_DATA_16, struct.pack("<H", ftms.FTMS_SERVICE_UUID_16) + ftms.advertising_service_data()
)


def _with_name(adv: bytes, name: str) -> tuple[bytes, bytes]:
    """Fit as much of the name as possible into adv; the full name goes in the scan response."""
    name_bytes = name.encode("utf-8")
    room = MAX_AD_LEN - len(adv) - 2
    if room > 0:
        short = name_bytes[:room]
        adv += _ad(_COMPLETE_NAME if short == name_bytes else _SHORTENED_NAME, short)
    return adv, _ad(_COMPLETE_NAME, name_bytes[: MAX_AD_LEN - 2])


def _add_manufacturer_data(scan_rsp: bytes, manufacturer_data: dict[int, bytes]) -> bytes:
    for company_id, payload in manufacturer_data.items():
        entry = _ad(_MANUFACTURER_DATA, struct.pack("<H", company_id) + payload)
        if len(scan_rsp) + len(entry) <= MAX_AD_LEN:
            scan_rsp += entry
    return scan_rsp


def smartrow(name: str, manufacturer_data: dict[int, bytes]) -> tuple[bytes, bytes]:
    """The pulley clone, as the SmartRow app expects to find it."""
    adv = _GENERAL_DISCOVERABLE + _ad(_COMPLETE_16_BIT_UUIDS, struct.pack("<H", protocol.SERVICE_UUID_16))
    adv, scan_rsp = _with_name(adv, name)
    return adv, _add_manufacturer_data(scan_rsp, manufacturer_data)


def fitness(name: str) -> tuple[bytes, bytes]:
    """A generic FTMS rower for Peloton and other fitness apps."""
    adv = (
        _GENERAL_DISCOVERABLE
        + _ad(_COMPLETE_16_BIT_UUIDS, struct.pack("<H", ftms.FTMS_SERVICE_UUID_16))
        + _FTMS_SERVICE_DATA
    )
    return _with_name(adv, name)


HEART_RATE_SERVICE_UUID_16 = 0x180D
APPEARANCE_HEART_RATE_BELT = 0x0341  # GAP appearance "Heart Rate Sensor: Heart Rate Belt"


def heart_rate(name: str) -> tuple[bytes, bytes]:
    """A standard heart-rate sensor, as the fitness apps' heart-rate lists look for."""
    adv = (
        _GENERAL_DISCOVERABLE
        + _ad(_COMPLETE_16_BIT_UUIDS, struct.pack("<H", HEART_RATE_SERVICE_UUID_16))
        + _ad(_APPEARANCE, struct.pack("<H", APPEARANCE_HEART_RATE_BELT))
    )
    return _with_name(adv, name)


def combined(name: str, manufacturer_data: dict[int, bytes]) -> tuple[bytes, bytes]:
    """Single advertisement carrying both services (fallback without extended advertising)."""
    adv = (
        _GENERAL_DISCOVERABLE
        + _ad(_COMPLETE_16_BIT_UUIDS, struct.pack("<HH", ftms.FTMS_SERVICE_UUID_16, protocol.SERVICE_UUID_16))
        + _FTMS_SERVICE_DATA
    )
    adv, scan_rsp = _with_name(adv, name)
    return adv, _add_manufacturer_data(scan_rsp, manufacturer_data)
