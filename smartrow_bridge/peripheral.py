"""BLE peripheral (Bumble) on the USB dongle hosting both virtual devices.

One controller has one GATT database, so the "dual peripheral" is a single
GATT server containing:
  * Server 1 - a byte-for-byte clone of the pulley's services (0x1234 ...),
    whose notifications are the untouched pulley packets, and whose writes are
    forwarded to the real pulley;
  * Server 2 - a Fitness Machine Service rower (0x1826 / 0x2AD1).
It is advertised as two devices (two extended advertising sets with their own
addresses): the pulley clone for the SmartRow app, and a generically named FTMS
rower for Peloton. Each set restarts by itself when its central disconnects.
Controllers without extended advertising fall back to one combined advertisement.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from bumble.core import UUID
from bumble.device import AdvertisingEventProperties, AdvertisingParameters, Connection, Device
from bumble.gatt import Characteristic, CharacteristicValue, Service
from bumble.hci import Address
from bumble.transport import open_transport

from . import advertising, ftms
from .gatt_spec import CharSpec, PulleySnapshot, short_uuid
from .protocol import RowingMetrics

log = logging.getLogger(__name__)

MAX_CONNECTIONS = 2
# Bumble's default is one advertisement per second, which phones can take several
# seconds to catch. 100 ms is within Apple's recommended range for accessories and
# gets the devices listed quickly; the Pi is mains powered, so the cost is nil.
ADVERTISING_INTERVAL_MS = 100

_PROPERTIES = {
    "broadcast": Characteristic.Properties.BROADCAST,
    "read": Characteristic.Properties.READ,
    "write-without-response": Characteristic.Properties.WRITE_WITHOUT_RESPONSE,
    "write": Characteristic.Properties.WRITE,
    "notify": Characteristic.Properties.NOTIFY,
    "indicate": Characteristic.Properties.INDICATE,
}

CloneWriteCallback = Callable[[int, bytes], None]  # (pulley handle, data)


def _clone_uuid(uuid: str) -> UUID:
    short = short_uuid(uuid)
    return UUID(uuid) if short is None else UUID.from_16_bits(short)


class BridgePeripheral:
    def __init__(
        self,
        transport: str,
        clone_address: str,
        clone_name: str,
        fitness_address: str,
        fitness_name: str,
        pulley: PulleySnapshot,
        on_clone_write: CloneWriteCallback,
        advertise_pulley_id: bool = True,
    ) -> None:
        self.transport_spec = transport
        self.clone_address = clone_address
        self.clone_name = clone_name
        self.fitness_address = fitness_address
        self.fitness_name = fitness_name
        self.pulley = pulley
        self.on_clone_write = on_clone_write
        # The pulley ID the SmartRow app shows as "SmartRow-<id>". Left out only to test
        # whether the app accepts a clone without it (iOS cannot advertise it).
        self.manufacturer_data = pulley.manufacturer_data if advertise_pulley_id else {}

        self._legacy_advertising = False
        self._transport = None
        self.device: Device | None = None
        self._clone_by_handle: dict[int, Characteristic] = {}
        self._tablet_connections: set[int] = set()
        self._fitness_connections: set[int] = set()
        # Running totals for the bridge's once-a-minute data-flow summary.
        self.forwarded_to_smartrow = 0
        self.ftms_updates_sent = 0
        self._forward_queue: asyncio.Queue[tuple[Characteristic, bytes]] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._rower_data: Characteristic | None = None
        self._control_point: Characteristic | None = None

    @property
    def tablet_connected(self) -> bool:
        """True while the SmartRow app is attached to the clone (subscribed or writing)."""
        return bool(self._tablet_connections)

    @property
    def fitness_connected(self) -> bool:
        """True while a fitness app (Peloton) is subscribed to Rower Data."""
        return bool(self._fitness_connections)

    # ------------------------------------------------------------------ GATT

    def _clone_characteristic(self, spec: CharSpec) -> Characteristic:
        props = Characteristic.Properties(0)
        for name in spec.properties:
            props |= _PROPERTIES.get(name, Characteristic.Properties(0))
        permissions = Characteristic.Permissions(0)
        if "read" in spec.properties:
            permissions |= Characteristic.READABLE
        writable = bool({"write", "write-without-response"} & spec.properties)
        if writable:
            permissions |= Characteristic.WRITEABLE

        if writable:
            cached = spec.value

            def on_write(connection: Connection, value: bytes, handle: int = spec.handle) -> None:
                self._tablet_connections.add(connection.handle)
                self.on_clone_write(handle, bytes(value))

            value = CharacteristicValue(read=lambda _conn: cached, write=on_write)
        else:
            value = spec.value

        char = Characteristic(_clone_uuid(spec.uuid), props, permissions, value)
        if {"notify", "indicate"} & spec.properties:
            char.on("subscription", self._on_clone_subscription)
        return char

    def _on_rower_data_subscription(self, connection: Connection, notify: bool, _indicate: bool) -> None:
        if notify:
            self._fitness_connections.add(connection.handle)
        else:
            self._fitness_connections.discard(connection.handle)
        log.info("Rower Data %s by %s (connection 0x%04X)",
                 "subscribed" if notify else "unsubscribed", connection.peer_address, connection.handle)

    def _on_clone_subscription(self, connection: Connection, notify: bool, indicate: bool) -> None:
        if notify or indicate:
            self._tablet_connections.add(connection.handle)
            log.info("SmartRow app subscribed (connection 0x%04X)", connection.handle)
        else:
            self._tablet_connections.discard(connection.handle)

    def _build_services(self) -> list[Service]:
        services = []
        for svc in self.pulley.services:
            chars = []
            for spec in svc.characteristics:
                char = self._clone_characteristic(spec)
                self._clone_by_handle[spec.handle] = char
                chars.append(char)
            services.append(Service(_clone_uuid(svc.uuid), chars))

        self._rower_data = Characteristic(
            UUID.from_16_bits(ftms.ROWER_DATA_UUID_16),
            Characteristic.Properties.NOTIFY,
            Characteristic.READABLE,
            b"",
        )
        self._rower_data.on("subscription", self._on_rower_data_subscription)
        self._control_point = Characteristic(
            UUID.from_16_bits(ftms.CONTROL_POINT_UUID_16),
            Characteristic.Properties.WRITE | Characteristic.Properties.INDICATE,
            Characteristic.WRITEABLE,
            CharacteristicValue(write=self._on_control_point),
        )
        services.append(
            Service(
                UUID.from_16_bits(ftms.FTMS_SERVICE_UUID_16),
                [
                    Characteristic(
                        UUID.from_16_bits(ftms.FEATURE_UUID_16),
                        Characteristic.Properties.READ,
                        Characteristic.READABLE,
                        ftms.encode_features(),
                    ),
                    self._rower_data,
                    Characteristic(
                        UUID.from_16_bits(ftms.TRAINING_STATUS_UUID_16),
                        Characteristic.Properties.READ | Characteristic.Properties.NOTIFY,
                        Characteristic.READABLE,
                        ftms.TRAINING_STATUS_IDLE,
                    ),
                    self._control_point,
                    Characteristic(
                        UUID.from_16_bits(ftms.MACHINE_STATUS_UUID_16),
                        Characteristic.Properties.NOTIFY,
                        Characteristic.READABLE,
                        b"",
                    ),
                ],
            )
        )
        return services

    def _on_control_point(self, connection: Connection, value: bytes) -> None:
        response = ftms.control_point_response(bytes(value))
        if response is None:
            return
        log.debug("FTMS control point %s -> %s", bytes(value).hex(), response.hex())
        asyncio.get_running_loop().create_task(
            self.device.indicate_subscriber(connection, self._control_point, response)
        )

    # ------------------------------------------------------------ lifecycle

    async def __aenter__(self) -> "BridgePeripheral":
        self._transport = await open_transport(self.transport_spec)
        # The GAP Device Name is shared by both virtual devices. iOS shows it after
        # connecting, so it carries the fitness name that Peloton displays.
        self.device = Device.with_hci(
            self.fitness_name, Address(self.clone_address), self._transport.source, self._transport.sink
        )
        self.device.add_services(self._build_services())
        self.device.on("connection", self._on_connection)
        await self.device.power_on()

        try:
            await self._start_advertising_sets()
        except Exception as exc:  # noqa: BLE001 - controller or Bumble without extended advertising
            log.warning("Extended advertising unavailable (%s); falling back to one combined "
                        "advertisement named %r", exc, self.clone_name)
            self._legacy_advertising = True
            adv, scan_rsp = advertising.combined(self.clone_name, self.manufacturer_data)
            self.device.advertising_data = adv
            self.device.scan_response_data = scan_rsp
            self.device.advertising_interval_min = ADVERTISING_INTERVAL_MS
            self.device.advertising_interval_max = ADVERTISING_INTERVAL_MS
            await self._update_advertising()

        self._tasks.append(asyncio.create_task(self._forward_loop()))
        return self

    async def _start_advertising_sets(self) -> None:
        params = AdvertisingParameters(
            advertising_event_properties=AdvertisingEventProperties(
                is_connectable=True, is_scannable=True, is_legacy=True
            ),
            primary_advertising_interval_min=ADVERTISING_INTERVAL_MS,
            primary_advertising_interval_max=ADVERTISING_INTERVAL_MS,
        )
        sets = (
            ("SmartRow app", self.clone_address, self.clone_name,
             advertising.smartrow(self.clone_name, self.manufacturer_data)),
            ("fitness apps", self.fitness_address, self.fitness_name,
             advertising.fitness(self.fitness_name)),
        )
        for audience, address, name, (adv, scan_rsp) in sets:
            await self.device.create_advertising_set(
                advertising_parameters=params,
                random_address=Address(address),
                advertising_data=adv,
                scan_response_data=scan_rsp,
                auto_restart=True,
            )
            log.info("Advertising %r at %s for the %s", name, address, audience)
        if not self.manufacturer_data:
            log.warning("Pulley ID (manufacturer data) is NOT advertised - SRB_ADVERTISE_PULLEY_ID=0 test mode")

    async def __aexit__(self, *exc) -> None:
        for task in self._tasks:
            task.cancel()
        if self._transport is not None:
            await self._transport.close()

    def _on_connection(self, connection: Connection) -> None:
        own = str(getattr(connection, "self_address", "")).upper()
        if own.startswith(self.clone_address.upper()):
            target = f"to {self.clone_name!r}"
        elif own.startswith(self.fitness_address.upper()):
            target = f"to {self.fitness_name!r}"
        else:
            target = f"at {own or 'unknown address'}"
        log.info("Central connected %s: %s (connection 0x%04X)", target, connection.peer_address, connection.handle)

        def on_disconnection(reason: int) -> None:
            log.info("Central disconnected: %s (reason 0x%02X)", connection.peer_address, reason)
            self._tablet_connections.discard(connection.handle)
            self._fitness_connections.discard(connection.handle)
            if self._legacy_advertising:
                asyncio.get_running_loop().create_task(self._update_advertising())

        connection.on("disconnection", on_disconnection)
        if self._legacy_advertising:
            asyncio.get_running_loop().create_task(self._update_advertising())

    async def _update_advertising(self) -> None:
        """Fallback mode: advertise whenever fewer than two centrals are attached."""
        await asyncio.sleep(0.2)  # let the controller settle after a (dis)connection
        want = len(self.device.connections) < MAX_CONNECTIONS
        try:
            if want and not self.device.is_advertising:
                await self.device.start_advertising(auto_restart=False)
            elif not want and self.device.is_advertising:
                await self.device.stop_advertising()
        except Exception as exc:  # noqa: BLE001
            log.warning("Advertising update failed: %s", exc)

    # ---------------------------------------------------------------- data

    def forward_notification(self, pulley_handle: int, data: bytes) -> None:
        """Queue a raw pulley notification for the clone, preserving order."""
        char = self._clone_by_handle.get(pulley_handle)
        if char is not None:
            self._forward_queue.put_nowait((char, data))

    async def _forward_loop(self) -> None:
        while True:
            char, data = await self._forward_queue.get()
            try:
                if char.properties & Characteristic.Properties.NOTIFY:
                    await self.device.notify_subscribers(char, data)
                else:
                    await self.device.indicate_subscribers(char, data)
                if self._tablet_connections:
                    self.forwarded_to_smartrow += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("Forwarding to SmartRow app failed: %s", exc)

    async def publish_rower_data(self, metrics: RowingMetrics) -> None:
        await self.device.notify_subscribers(self._rower_data, ftms.encode_rower_data(metrics))
        if self._fitness_connections:
            self.ftms_updates_sent += 1
