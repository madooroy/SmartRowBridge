# SPDX-License-Identifier: GPL-3.0-only
"""Heart rate from a Bluetooth chest strap, read by the bridge itself.

Optional (SRB_HEART_RATE). Any strap with the standard Heart Rate service works
(0x180D, Heart Rate Measurement 0x2A37); straps that accept several connections at
once (the COROS heart rate monitor takes three) can stay paired with a watch or the
fitness app as well.

The strap shares the built-in radio with the pulley. To keep the pulley first:
- the strap is only looked for once the pulley is connected;
- scans take turns with the pulley's (a shared lock), in short windows with pauses,
  so a pulley that drops is found again straight away;
- a lost strap is looked for again, the same way, until it comes back.

The bridge uses the heart rate for the coach and its session summaries, and passes it
to the fitness app in the FTMS Rower Data (SRB_HEART_RATE_TO_FITNESS).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

log = logging.getLogger(__name__)

HR_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
SCAN_WINDOW_S = 8.0  # look for the strap this long ...
SCAN_PAUSE_S = 20.0  # ... then leave the radio alone this long
CONNECT_TIMEOUT_S = 10.0
STALE_AFTER_S = 5.0  # no reading for this long: heart rate unknown


def parse_measurement(data: bytes) -> int | None:
    """Beats per minute from a Heart Rate Measurement, or None if there is no reading.

    Flags bit 0: value is uint16 (else uint8). Bits 1-2: sensor contact supported (bit 2)
    and detected (bit 1); a strap that supports contact detection but has no contact (off
    the chest) reports nothing useful.
    """
    if len(data) < 2:
        return None
    flags = data[0]
    if flags & 0x01:
        if len(data) < 3:
            return None
        bpm = int.from_bytes(data[1:3], "little")
    else:
        bpm = data[1]
    if flags & 0x04 and not flags & 0x02:
        return None
    return bpm if 25 <= bpm <= 250 else None


def matches(setting: str, device: BLEDevice, adv: AdvertisementData) -> bool:
    """SRB_HEART_RATE: "auto" = the first strap advertising the Heart Rate service; or a
    MAC address; or part of the strap's name (e.g. "COROS")."""
    if setting == "auto":
        return HR_SERVICE_UUID in (u.lower() for u in adv.service_uuids)
    if len(setting) == 17 and setting.count(":") == 5:
        return device.address.upper() == setting.upper()
    name = adv.local_name or device.name or ""
    return setting.lower() in name.lower()


class HeartRateLink:
    def __init__(self, adapter: str, setting: str, scan_lock: asyncio.Lock,
                 on_reading: Callable[[int], None]) -> None:
        self.adapter = adapter
        self.setting = setting.strip()
        self.scan_lock = scan_lock
        self.on_reading = on_reading
        self.name: str | None = None
        self.address: str | None = None  # pinned once found
        self.last_reading = 0.0  # time.monotonic()
        self._client: BleakClient | None = None
        self._lost = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    def fresh(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return self.connected and now - self.last_reading <= STALE_AFTER_S

    async def _find(self) -> BLEDevice | None:
        found: asyncio.Future = asyncio.get_running_loop().create_future()

        def detected(device: BLEDevice, adv: AdvertisementData) -> None:
            if found.done():
                return
            if self.address and device.address.upper() != self.address:
                return
            if self.address or matches(self.setting, device, adv):
                self.name = adv.local_name or device.name or "Heart rate strap"
                found.set_result(device)

        async with self.scan_lock:
            async with BleakScanner(detected, adapter=self.adapter):
                try:
                    return await asyncio.wait_for(found, SCAN_WINDOW_S)
                except asyncio.TimeoutError:
                    return None

    def _measured(self, _char, data: bytearray) -> None:
        bpm = parse_measurement(bytes(data))
        if bpm is not None:
            self.last_reading = time.monotonic()
            self.on_reading(bpm)

    async def run(self) -> None:
        """Keep the strap connected for as long as the bridge runs."""
        try:
            await self._run()
        finally:
            if self._client is not None and self._client.is_connected:
                await self._client.disconnect()

    async def _run(self) -> None:
        announced = False
        while True:
            try:
                device = await self._find()
                if device is None:
                    if not announced:
                        log.info("Looking for the heart rate strap (%s); put it on to wake it", self.setting)
                        announced = True
                    await asyncio.sleep(SCAN_PAUSE_S)
                    continue
                self._lost.clear()
                client = BleakClient(device, adapter=self.adapter, timeout=CONNECT_TIMEOUT_S,
                                     disconnected_callback=lambda _: self._lost.set())
                await client.connect()
                await client.start_notify(HR_MEASUREMENT_UUID, self._measured)
                self._client = client
                self.address = device.address.upper()
                log.info("Heart rate strap connected: %s (%s)", self.name, self.address)
                announced = False
                await self._lost.wait()
                log.warning("Heart rate strap disconnected")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the strap must never disturb the pulley
                log.warning("Heart rate strap: %s", f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)
            finally:
                if self._client is not None and not self._client.is_connected:
                    self._client = None
            await asyncio.sleep(SCAN_PAUSE_S)
