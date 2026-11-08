"""Bluetooth addresses for the virtual devices (two, or three with the heart-rate sensor).

Each virtual device needs its own static random address, and it must stay the
same across reboots so the SmartRow app and Peloton recognise the device they
paired with. Unless set in the configuration, the addresses are derived from the
dongle's own MAC: stable for one Pi, different between Pis, so two bridges in
range of each other do not collide.
"""

from __future__ import annotations

import hashlib

# Used only if the dongle's MAC cannot be read.
FALLBACK_CLONE = "F2:53:52:4F:57:31"
FALLBACK_FITNESS = "F2:53:52:4F:57:32"
FALLBACK_HEART_RATE = "F2:53:52:4F:57:33"


def derive(adapter_mac: str, role: str) -> str:
    """Static random address for `role` ("clone", "fitness" or "heart_rate") on the adapter with this MAC."""
    digest = bytearray(hashlib.sha256(f"smartrow-bridge:{role}:{adapter_mac.upper()}".encode()).digest()[:6])
    digest[0] |= 0xC0  # the two top bits mark a static random address
    return ":".join(f"{b:02X}" for b in digest)


def resolve(configured: str | None, adapter_mac: str | None, role: str) -> str:
    """The configured address if there is one, else a derived one, else the fallback."""
    if configured:
        return configured.upper()
    if adapter_mac:
        return derive(adapter_mac, role)
    return {"clone": FALLBACK_CLONE, "heart_rate": FALLBACK_HEART_RATE}.get(role, FALLBACK_FITNESS)
