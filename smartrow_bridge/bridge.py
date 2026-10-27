"""Wires the pulley central (hci0) to the dual peripheral (hci1)."""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
from typing import IO, TYPE_CHECKING

from . import adapters, addresses, coach, protocol, sessions, status
from .central import PulleyLink
from .config import Config

if TYPE_CHECKING:  # imported for real in run(), in the background (see _load_peripheral_module)
    from .peripheral import BridgePeripheral

log = logging.getLogger(__name__)


def _load_peripheral_module():
    """Import the Bumble side (~1 s on a Pi 4). Runs in a thread while the pulley scan runs."""
    started = time.monotonic()
    module = importlib.import_module(".peripheral", __package__)
    log.info("Peripheral stack loaded in %.1f s (in the background)", time.monotonic() - started)
    return module

POLL_INTERVAL_S = 2.0
FTMS_INTERVAL_S = 1.0
STATS_INTERVAL_S = 60.0
STATUS_INTERVAL_S = 1.0


class Bridge:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.parser = protocol.SmartRowParser()
        self.coach: coach.IntervalCoach | None = None
        if cfg.coach and cfg.status_file.lower() != "off":
            self.coach = coach.IntervalCoach(coach.CoachSettings(
                start_pct=cfg.coach_start_pct, max_effort_s=cfg.coach_max_min * 60,
                sensitivity=cfg.coach_sensitivity))
            self.parser.on_stroke = self.coach.add
        self.hr_link = None  # heartrate.HeartRateLink, created once the pulley's adapter is known
        self.recorder: sessions.SessionRecorder | None = None
        if self.coach and cfg.session_dir.lower() != "off":
            self.recorder = sessions.SessionRecorder(cfg.session_dir, self.coach)
        self.uploader = None
        if self.recorder is not None:
            from .uploads import Settings, Uploader
            self.uploader = Uploader(Settings(cfg.session_dir, cfg.intervals_key_file, cfg.intervals_upload,
                                              cfg.strava_file, cfg.strava_sport_type))
        self.peripheral: BridgePeripheral | None = None
        self.link: PulleyLink | None = None
        self._driver_initialized = False
        self._challenge_requested = False
        self._pulley_ready = False
        self._pulley_packets = 0
        self._sniff: IO[str] | None = None
        self._stage = status.STARTING
        self._pulley_label: str | None = None
        self._clone_name: str | None = None

    # The pulley only streams after the "$\rV@\r" init, periodic "$" polls and,
    # on V3 hardware, a KEYLOCK answer. The SmartRow app does all of that through
    # the clone; the bridge only takes over while the app is not connected.
    @property
    def _bridge_drives_pulley(self) -> bool:
        return self.cfg.drive_without_tablet and not (self.peripheral and self.peripheral.tablet_connected)

    def _on_notify(self, uuid: str, handle: int, data: bytes) -> None:
        # 1. Locked bypass: forward the untouched bytes first.
        if self.peripheral is not None:
            self.peripheral.forward_notification(handle, data)

        if uuid != protocol.NOTIFY_CHAR_UUID:
            return
        self._pulley_packets += 1
        if self._sniff is not None:
            self._sniff.write(f"{data.hex(' ')}  {data!r}\n")
            self._sniff.flush()

        # 2. Translation path works on its own copy.
        if self._bridge_drives_pulley:
            if protocol.is_v3_version_reply(data) and not self._challenge_requested:
                log.info("V3 pulley (%s); requesting KEYLOCK challenge", data.decode("ascii", "replace").strip())
                self._challenge_requested = True
                self.link.write_uuid(protocol.WRITE_CHAR_UUID, protocol.CHALLENGE_REQUEST, bytewise=True)
            if b"KEYLOCK" in data:
                log.info("Answering V3 KEYLOCK challenge")
                self.link.write_uuid(protocol.WRITE_CHAR_UUID, protocol.keylock_response(data), bytewise=True)
        self.parser.feed(data)

    def _on_heart_rate(self, bpm: int) -> None:
        self.parser.metrics.heart_rate_bpm = bpm

    def _on_pulley_connected(self) -> None:
        """Called once notifications are subscribed, so replies to our commands are seen."""
        self._driver_initialized = False
        self._challenge_requested = False
        self._pulley_ready = True

    async def _driver_loop(self) -> None:
        while True:
            if not self.link.connected:
                self._pulley_ready = False
            elif self._pulley_ready:
                if not self._bridge_drives_pulley:
                    self._driver_initialized = True  # the SmartRow app has it in hand
                elif not self._driver_initialized:
                    log.info("No SmartRow app attached; initialising pulley from the bridge")
                    self.link.write_uuid(protocol.WRITE_CHAR_UUID, protocol.INIT_COMMAND, bytewise=True)
                    self._driver_initialized = True
                else:
                    self.link.write_uuid(protocol.WRITE_CHAR_UUID, protocol.POLL_COMMAND)
            await asyncio.sleep(POLL_INTERVAL_S)

    async def _ftms_loop(self) -> None:
        while True:
            await asyncio.sleep(FTMS_INTERVAL_S)
            snap = self.parser.snapshot()
            try:
                await self.peripheral.publish_rower_data(snap)
            except Exception as exc:  # noqa: BLE001
                log.warning("FTMS notify failed: %s", exc)
            log.debug("FTMS %d W, %.1f spm, %d m, %d s/500m", snap.power_w, snap.stroke_rate_spm,
                      snap.distance_m, snap.pace_s_per_500m)

    async def _stats_loop(self) -> None:
        """Once a minute, while anything is flowing, log proof that data reaches both apps."""
        last = (0, 0, 0)
        while True:
            await asyncio.sleep(STATS_INTERVAL_S)
            p = self.peripheral
            now = (self._pulley_packets, p.forwarded_to_smartrow, p.ftms_updates_sent)
            pulley, smartrow, ftms_sent = (n - o for n, o in zip(now, last))
            last = now
            if pulley or smartrow or ftms_sent:
                m = self.parser.metrics
                log.info(
                    "Data flow, last %d s: %d pulley packets in, %d forwarded to SmartRow app, "
                    "%d FTMS updates to fitness app | %d m, %d strokes",
                    STATS_INTERVAL_S, pulley, smartrow, ftms_sent, m.distance_m, m.stroke_count,
                )
            if self.parser.bad_checksums:
                log.warning("%d pulley packets had bad checksums", self.parser.bad_checksums)
                self.parser.bad_checksums = 0

    def _status(self) -> dict:
        stage = self._stage
        if stage == status.CONNECTED and not (self.link and self.link.connected):
            stage = status.RECONNECTING
        p = self.peripheral
        snap = self.parser.snapshot()
        return status.build(
            stage=stage,
            pulley=self._pulley_label,
            smartrow_app=bool(p and p.tablet_connected),
            fitness_app=bool(p and p.fitness_connected),
            clone_name=self._clone_name,
            fitness_name=self.cfg.fitness_name,
            metrics=snap,
            rowing=snap.stroke_rate_spm > 0 or snap.power_w > 0,
            coach=self.coach.state(time.monotonic()) if self.coach else None,
            heart_rate=({"name": self.hr_link.name, "connected": self.hr_link.connected}
                        if self.hr_link else None),
        )

    async def _status_loop(self) -> None:
        """Keep the status file (read by the optional screen) up to date."""
        path = self.cfg.status_file
        failed = False
        while True:
            if self.hr_link is not None and not self.hr_link.fresh():
                self.parser.metrics.heart_rate_bpm = 0  # strap off or out of range: no reading
            try:
                status.write(path, self._status())
                failed = False
            except Exception as exc:  # noqa: BLE001 - the status file must never stop the bridge
                if not failed:
                    log.warning("Could not write status file %s: %s", path, exc)
                failed = True
            if self.recorder is not None:
                try:
                    self.recorder.update(self.parser.metrics)
                except Exception:  # noqa: BLE001 - nor may the session summary
                    log.exception("Session summary failed")
            if self.uploader is not None:
                try:
                    self.uploader.tick()
                except Exception:  # noqa: BLE001 - nor the uploads
                    log.exception("Uploads failed")
            await asyncio.sleep(STATUS_INTERVAL_S)

    async def run(self) -> None:
        status_task = None
        if self.cfg.status_file.lower() != "off":
            status_task = asyncio.create_task(self._status_loop())
        try:
            await self._run()
        finally:
            if status_task is not None:
                status_task.cancel()
            if self.recorder is not None:
                try:
                    self.recorder.close(self.parser.metrics)  # stopping: save the session as it is
                except Exception:  # noqa: BLE001
                    log.exception("Session summary failed")

    async def _run(self) -> None:
        # Bumble is only needed once the pulley is connected, so load it in a thread
        # while the radios are prepared and the pulley scan runs.
        peripheral_module = asyncio.create_task(asyncio.to_thread(_load_peripheral_module))

        central_hci, peripheral_hci = adapters.resolve(
            self.cfg.central_adapter, self.cfg.peripheral_adapter, self.cfg.central_mac, self.cfg.peripheral_mac
        )
        await adapters.wait_until_ready([central_hci, peripheral_hci])
        await adapters.prepare_central(central_hci)
        adapters.release_from_bluez(peripheral_hci)
        transport = self.cfg.peripheral_transport or f"hci-socket:{adapters.index_of(peripheral_hci)}"

        # The virtual devices keep the same addresses on every boot (so the apps recognise
        # them) but differ from one Pi to the next.
        dongle_mac = adapters.read_mac(adapters.index_of(peripheral_hci))
        clone_address = addresses.resolve(self.cfg.clone_address, dongle_mac, "clone")
        fitness_address = addresses.resolve(self.cfg.fitness_address, dongle_mac, "fitness")

        if self.cfg.sniff_log:
            self._sniff = open(self.cfg.sniff_log, "a", encoding="utf-8")

        self.link = PulleyLink(
            adapter=central_hci,
            address=self.cfg.pulley_address,
            name_filter=self.cfg.pulley_name,
            exclude_addresses=(clone_address, fitness_address),
            on_notify=self._on_notify,
        )
        self.link.on_connected = self._on_pulley_connected
        if self.cfg.heart_rate.lower() not in ("", "off", "0", "no"):
            from .heartrate import HeartRateLink
            self.hr_link = HeartRateLink(central_hci, self.cfg.heart_rate, self.link.scan_lock,
                                         on_reading=self._on_heart_rate)
        self._stage = status.SEARCHING
        snapshot = await self.link.connect_first()
        self._pulley_label = status.pulley_label(snapshot.name, snapshot.manufacturer_data)
        self._clone_name = self.cfg.clone_name or snapshot.name
        BridgePeripheral = (await peripheral_module).BridgePeripheral

        try:
            async with BridgePeripheral(
                transport=transport,
                clone_address=clone_address,
                clone_name=self.cfg.clone_name or snapshot.name,
                fitness_address=fitness_address,
                fitness_name=self.cfg.fitness_name,
                pulley=snapshot,
                advertise_pulley_id=self.cfg.advertise_pulley_id,
                on_clone_write=self.link.write,
                heart_rate=self.hr_link is not None and self.cfg.heart_rate_to_fitness,
            ) as peripheral:
                self.peripheral = peripheral
                self._stage = status.CONNECTED
                tasks = [self.link.run(), self._driver_loop(), self._ftms_loop(), self._stats_loop()]
                if self.hr_link is not None:
                    tasks.append(self.hr_link.run())  # only now: the pulley is connected first
                await asyncio.gather(*tasks)
        finally:
            if self._sniff is not None:
                self._sniff.close()
