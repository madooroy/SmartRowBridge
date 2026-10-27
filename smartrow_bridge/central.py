"""BLE central (bleak / BlueZ) that holds the connection to the physical SmartRow pulley."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from . import protocol
from .gatt_spec import CharSpec, PulleySnapshot, ServiceSpec

log = logging.getLogger(__name__)

# Services the peripheral host provides itself.
_SKIP_SERVICES = {
    "00001800-0000-1000-8000-00805f9b34fb",  # Generic Access
    "00001801-0000-1000-8000-00805f9b34fb",  # Generic Attribute
}

RETRY_DELAY_S = 5
CONNECT_TIMEOUT_S = 10.0
WRITE_GAP_S = 0.03  # spacing between single-byte command writes

NotifyCallback = Callable[[str, int, bytes], None]  # (char uuid, handle, raw data)


def _describe(exc: BaseException) -> str:
    """Timeouts and some BlueZ errors have an empty message; name the type instead."""
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


class PulleyLink:
    def __init__(
        self,
        adapter: str,
        address: str | None,
        name_filter: str,
        exclude_addresses: tuple[str, ...],
        on_notify: NotifyCallback,
    ) -> None:
        self.adapter = adapter
        self.address = address.upper() if address else None
        self.name_filter = name_filter.lower()
        self.exclude_addresses = {a.upper() for a in exclude_addresses}
        self.on_notify = on_notify
        self.on_connected: Callable[[], None] = lambda: None
        self.scan_lock = asyncio.Lock()  # shared with the heart rate strap: one scan at a time

        self._client: BleakClient | None = None
        self._disconnected = asyncio.Event()
        self._writes: asyncio.Queue[tuple[int, bytes, bool]] = asyncio.Queue()
        self._write_props: dict[int, bool] = {}  # handle -> use write-with-response
        self._handle_by_uuid: dict[str, int] = {}

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    def _matches(self, device: BLEDevice, adv: AdvertisementData) -> bool:
        if device.address.upper() in self.exclude_addresses:
            return False  # our own virtual devices, advertising from the other adapter
        if self.address:
            return device.address.upper() == self.address
        if protocol.SERVICE_UUID in (u.lower() for u in adv.service_uuids):
            return True
        name = adv.local_name or device.name or ""
        return bool(self.name_filter) and self.name_filter in name.lower()

    async def _scan(self) -> tuple[BLEDevice, AdvertisementData]:
        found: asyncio.Future[tuple[BLEDevice, AdvertisementData]] = asyncio.get_running_loop().create_future()

        def detected(device: BLEDevice, adv: AdvertisementData) -> None:
            if not found.done() and self._matches(device, adv):
                found.set_result((device, adv))

        log.info("Scanning on %s for the SmartRow pulley...", self.adapter)
        async with self.scan_lock:
            async with BleakScanner(detected, adapter=self.adapter):
                return await found

    async def _connect(self, device: BLEDevice) -> BleakClient:
        self._disconnected.clear()
        client = BleakClient(
            device,
            disconnected_callback=lambda _: self._disconnected.set(),
            adapter=self.adapter,
            timeout=CONNECT_TIMEOUT_S,
        )
        await client.connect()
        self._client = client
        log.info("Connected to pulley %s", device.address)
        return client

    async def _subscribe(self, client: BleakClient) -> None:
        for service in client.services:
            for char in service.characteristics:
                if {"notify", "indicate"} & set(char.properties):
                    await client.start_notify(char, self._notified)
                    log.debug("Subscribed to %s (handle %d)", char.uuid, char.handle)

    def _notified(self, char: BleakGATTCharacteristic, data: bytearray) -> None:
        self.on_notify(char.uuid.lower(), char.handle, bytes(data))

    async def connect_first(self) -> PulleySnapshot:
        """Find and connect to the pulley, then describe its GATT table for cloning."""
        while True:
            try:
                device, adv = await self._scan()
                client = await self._connect(device)
                break
            except Exception as exc:  # noqa: BLE001 - keep retrying until the pulley shows up
                log.warning("Pulley connection failed (%s); retrying in %d s", _describe(exc), RETRY_DELAY_S)
                await asyncio.sleep(RETRY_DELAY_S)
        self.address = device.address.upper()  # reconnect to this exact pulley from now on

        # In handle order, so the clone's layout is the pulley's on every run: BlueZ lists
        # characteristics in no fixed order, and iOS remembers a device's layout.
        services: list[ServiceSpec] = []
        for service in sorted(client.services, key=lambda s: s.handle):
            if service.uuid.lower() in _SKIP_SERVICES:
                continue
            spec = ServiceSpec(uuid=service.uuid.lower())
            for char in sorted(service.characteristics, key=lambda c: c.handle):
                props = frozenset(char.properties)
                value = b""
                if "read" in props:
                    try:
                        value = bytes(await client.read_gatt_char(char))
                    except Exception as exc:  # noqa: BLE001
                        log.warning("Could not read %s: %s", char.uuid, exc)
                spec.characteristics.append(CharSpec(char.uuid.lower(), char.handle, props, value))
                self._handle_by_uuid.setdefault(char.uuid.lower(), char.handle)
                if {"write", "write-without-response"} & props:
                    self._write_props[char.handle] = "write-without-response" not in props
            services.append(spec)
            log.info("Pulley service %s: %s", spec.uuid,
                     ", ".join(f"{c.uuid[4:8]}[{'/'.join(sorted(c.properties))}]" for c in spec.characteristics))

        return PulleySnapshot(
            address=device.address,
            name=adv.local_name or device.name or "SmartRow",
            manufacturer_data=dict(adv.manufacturer_data),
            services=services,
        )

    def write(self, handle: int, data: bytes, bytewise: bool = False) -> None:
        """Queue a write to the pulley; writes are sent strictly in order.

        Writes forwarded from the SmartRow app go out exactly as the app sent
        them. Commands the bridge originates use bytewise=True: one byte per
        write, as the pulley's own protocol implementations do.
        """
        response = self._write_props.get(handle, True)
        chunks = [data[i:i + 1] for i in range(len(data))] if bytewise else [data]
        for chunk in chunks:
            self._writes.put_nowait((handle, chunk, response))

    def write_uuid(self, uuid: str, data: bytes, bytewise: bool = False) -> None:
        handle = self._handle_by_uuid.get(uuid)
        if handle is not None:
            self.write(handle, data, bytewise)

    async def _write_loop(self) -> None:
        while True:
            handle, data, response = await self._writes.get()
            if not self.connected:
                log.debug("Dropping write to handle %d while disconnected", handle)
                continue
            try:
                await self._client.write_gatt_char(handle, data, response=response)
            except Exception as exc:  # noqa: BLE001
                log.warning("Write to pulley failed: %s", exc)
            if len(data) == 1:
                await asyncio.sleep(WRITE_GAP_S)

    async def run(self) -> None:
        """Keep the pulley connected forever, reconnecting whenever it drops."""
        writer = asyncio.create_task(self._write_loop())
        try:
            client = self._client
            while True:
                if client is not None and client.is_connected:
                    await self._subscribe(client)
                    self.on_connected()
                    # BlueZ can fire a disconnect callback right after connecting while the
                    # link stays up (seen on a cold boot: data kept flowing). Only a link
                    # that is actually down counts.
                    while True:
                        await self._disconnected.wait()
                        self._disconnected.clear()
                        if not client.is_connected:
                            break
                        log.info("Ignoring spurious disconnect event; pulley link is still up")
                    log.warning("Pulley disconnected")
                client = None
                await asyncio.sleep(RETRY_DELAY_S)
                try:
                    device, _ = await self._scan()
                    client = await self._connect(device)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Reconnect failed: %s", _describe(exc))
                    client = None
        finally:
            writer.cancel()
            if self._client is not None and self._client.is_connected:
                await self._client.disconnect()
