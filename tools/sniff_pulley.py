#!/usr/bin/env python3
"""Standalone SmartRow pulley discovery + raw packet sniffer (bleak, central adapter only).

  * scans on the central adapter (hci0 by default) for the pulley and logs its address,
    RSSI and advertisement contents;
  * connects, maps every GATT service / characteristic / descriptor (handles,
    properties, readable values);
  * subscribes to every notify/indicate characteristic and prints each raw
    packet as hex + ASCII with a timestamp;
  * disconnects cleanly on Ctrl+C or after --duration seconds.

The pulley accepts one connection at a time, so stop the bridge first:
    sudo systemctl stop smartrow-bridge

The pulley only streams after an init command, periodic polls and (V3) a KEYLOCK
answer - normally sent by the SmartRow app. --drive (default on) sends them so
this script sees live data on its own.

--flash lights the Display HAT Mini's screen while the pulley's drive flag is on (dark
otherwise), so a filmed row shows the flag next to the body, frame by frame. Stop the screen's
own service first: sudo systemctl stop smartrow-display.

Usage:
    sudo ~/venv/bin/python sniff_pulley.py [--adapter hci0] [--address MAC] [--duration 120]
                                            [--log sniff.log] [--map gatt_map.json] [--no-drive] [--flash]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
from datetime import datetime

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

SMARTROW_SERVICE = "00001234-0000-1000-8000-00805f9b34fb"
SMARTROW_WRITE = "00001235-0000-1000-8000-00805f9b34fb"
SMARTROW_NOTIFY = "00001236-0000-1000-8000-00805f9b34fb"
INIT_COMMAND = bytes([0x24, 0x0D, 0x56, 0x40, 0x0D])  # "$\rV@\r"
POLL_COMMAND = b"$"


def keylock_response(notification: bytes) -> bytes:
    """V3 challenge answer (same algorithm as smartrow_bridge.protocol)."""
    challenge = notification.strip()
    idx = challenge.find(b"KEYLOCK=")
    if idx >= 0:
        challenge = challenge[idx:]
    if len(challenge) < 16 or f"{sum(challenge[:14]) & 0xFF:02X}".encode() != challenge[14:16].upper():
        return b"#"
    try:
        seed = int(challenge[10:14], 16)
    except ValueError:
        return b"#"
    return b"\r" + f"{(seed * 17923) // 256 & 0xFFFF:04x}".encode() + b"\r"


class Flash:
    """The Display HAT's screen as a light: white and lit while the drive flag is on, dark otherwise."""

    def __init__(self) -> None:
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from PIL import Image

        from smartrow_bridge.display.hat import DisplayHat

        self.hat = DisplayHat(buttons=False, led=False)
        self.hat.show(Image.new("RGB", (320, 240), (255, 255, 255)))
        self.on = None
        self.set(False)

    def set(self, on: bool) -> None:
        if on != self.on:
            self.hat.backlight(on)
            self.on = on

    def feed(self, raw: bytes) -> None:
        # bit 0x10 of byte 3 on a V3 record (high nibble 4 or 5); see smartrow_bridge.protocol.drive_flag
        if len(raw) == 17 and raw[3] & 0xE0 == 0x40:
            self.set(bool(raw[3] & 0x10))


def ascii_view(data: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)


class Output:
    def __init__(self, path: str | None) -> None:
        self.file = open(path, "a", encoding="utf-8") if path else None

    def __call__(self, line: str = "") -> None:
        print(line, flush=True)
        if self.file:
            self.file.write(line + "\n")
            self.file.flush()


async def find_pulley(args, out: Output):
    loop = asyncio.get_running_loop()
    found = loop.create_future()

    def detected(device, adv):
        if found.done():
            return
        name = adv.local_name or device.name or ""
        if args.address:
            match = device.address.upper() == args.address.upper()
        else:
            match = SMARTROW_SERVICE in (u.lower() for u in adv.service_uuids) or args.name.lower() in name.lower()
        if match:
            found.set_result((device, adv))

    out(f"Scanning on {args.adapter} for up to {args.scan_timeout:.0f} s (pull the handle to wake the pulley)...")
    async with BleakScanner(detected, adapter=args.adapter):
        return await asyncio.wait_for(found, args.scan_timeout)


async def dump_gatt(client: BleakClient, out: Output) -> list[dict]:
    gatt_map = []
    out("\n=== GATT map ===")
    for service in client.services:
        out(f"Service {service.uuid}  (handle 0x{service.handle:04X})  {service.description}")
        svc = {"uuid": service.uuid, "handle": service.handle, "characteristics": []}
        for char in service.characteristics:
            entry = {"uuid": char.uuid, "handle": char.handle, "properties": list(char.properties),
                     "value": None, "descriptors": []}
            line = f"  Char {char.uuid}  (handle 0x{char.handle:04X})  [{', '.join(char.properties)}]  {char.description}"
            if "read" in char.properties:
                try:
                    value = bytes(await client.read_gatt_char(char))
                    entry["value"] = value.hex()
                    line += f"\n      value: {value.hex(' ') or '(empty)'}  |{ascii_view(value)}|"
                except Exception as exc:  # noqa: BLE001
                    line += f"\n      value: <read failed: {exc}>"
            out(line)
            for desc in char.descriptors:
                d = {"uuid": desc.uuid, "handle": desc.handle, "value": None}
                try:
                    value = bytes(await client.read_gatt_descriptor(desc.handle))
                    d["value"] = value.hex()
                    out(f"      Desc {desc.uuid}  (handle 0x{desc.handle:04X})  {value.hex(' ')}  |{ascii_view(value)}|")
                except Exception as exc:  # noqa: BLE001
                    out(f"      Desc {desc.uuid}  (handle 0x{desc.handle:04X})  <read failed: {exc}>")
                entry["descriptors"].append(d)
            svc["characteristics"].append(entry)
        gatt_map.append(svc)
    return gatt_map


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--adapter", default="hci0", help="central adapter to bind to (default hci0)")
    ap.add_argument("--address", help="pulley MAC (default: first device advertising 0x1234 or named SmartRow)")
    ap.add_argument("--name", default="SmartRow", help="name substring to match when no --address is given")
    ap.add_argument("--scan-timeout", type=float, default=60.0)
    ap.add_argument("--duration", type=float, default=0, help="stop after N seconds of sniffing (0 = until Ctrl+C)")
    ap.add_argument("--log", help="also append all output to this file")
    ap.add_argument("--map", help="write the GATT map + advertisement as JSON to this file")
    ap.add_argument("--flash", action="store_true",
                    help="light the Display HAT screen while the drive flag is on (stop smartrow-display first)")
    ap.add_argument("--no-drive", dest="drive", action="store_false",
                    help="do not send init/poll/KEYLOCK; only listen")
    args = ap.parse_args()

    out = Output(args.log)
    flash = None
    if args.flash:
        try:
            flash = Flash()
        except Exception as exc:  # noqa: BLE001
            print(f"--flash: the screen isn't available ({exc}); is smartrow-display stopped?", file=sys.stderr)
            return 1
    out(f"# SmartRow sniff started {datetime.now().isoformat(timespec='seconds')} on {args.adapter}")

    try:
        device, adv = await find_pulley(args, out)
    except asyncio.TimeoutError:
        out("No pulley found. Is it awake, in range, and not connected to the bridge or the SmartRow app?")
        return 1

    out(f"\nFound pulley: {device.address}  name={adv.local_name or device.name!r}  RSSI={adv.rssi} dBm")
    out(f"  service UUIDs : {adv.service_uuids}")
    out(f"  manufacturer  : { {hex(k): v.hex(' ') for k, v in adv.manufacturer_data.items()} }")
    out(f"  service data  : { {k: v.hex(' ') for k, v in adv.service_data.items()} }")
    out(f"  tx power      : {adv.tx_power}")


    user_stop = asyncio.Event()  # Ctrl+C, or --duration reached
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, user_stop.set)

    start = time.monotonic()
    deadline = start + args.duration if args.duration > 0 else None
    count = 0
    first = True

    async def session(target) -> None:
        """One connection: map, subscribe, drive the pulley, log; returns when it drops or the user stops."""
        nonlocal count, flash, first
        dropped = asyncio.Event()
        async with BleakClient(target, adapter=args.adapter, timeout=20.0,
                               disconnected_callback=lambda _: dropped.set()) as client:
            out(f"Connected (MTU {client.mtu_size})")
            if first:
                gatt_map = await dump_gatt(client, out)
                if args.map:
                    with open(args.map, "w", encoding="utf-8") as f:
                        json.dump({"address": device.address, "name": adv.local_name or device.name,
                                   "manufacturer_data": {str(k): v.hex() for k, v in adv.manufacturer_data.items()},
                                   "service_uuids": adv.service_uuids, "services": gatt_map}, f, indent=2)
                    out(f"GATT map written to {args.map}")

            write_char = next((c for s in client.services for c in s.characteristics if c.uuid == SMARTROW_WRITE), None)
            has_write = write_char is not None
            with_response = has_write and "write-without-response" not in write_char.properties

            # Commands go out one byte per write, in order, like the pulley's own clients do.
            commands: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()
            challenge_requested = False

            def send(label: str, data: bytes) -> None:
                if args.drive and has_write:
                    commands.put_nowait((label, data))

            def notified(char: BleakGATTCharacteristic, data: bytearray) -> None:
                nonlocal count, challenge_requested, flash
                count += 1
                raw = bytes(data)
                out(f"{time.monotonic() - start:9.3f}  0x{char.handle:04X} {char.uuid[4:8]}  len={len(raw):2d}  "
                    f"{raw.hex(' ')}  |{ascii_view(raw)}|")
                if flash:
                    try:
                        flash.feed(raw)
                    except Exception as exc:  # noqa: BLE001 - the log matters more than the light
                        out(f"# --flash stopped: {exc!r}")
                        flash = None
                if b"V3." in raw and not challenge_requested:
                    challenge_requested = True
                    send("V3 firmware -> request KEYLOCK challenge", b"#")
                if b"KEYLOCK" in raw:
                    send("answer KEYLOCK challenge", keylock_response(raw))

            if first:
                out("\n=== Notifications (seconds  handle uuid  length  hex  |ascii|) ===")
                first = False
            subscribed = []
            for service in client.services:
                for char in service.characteristics:
                    if {"notify", "indicate"} & set(char.properties):
                        await client.start_notify(char, notified)
                        subscribed.append(char)
                        out(f"Subscribed to {char.uuid} (handle 0x{char.handle:04X})")

            async def writer() -> None:
                while True:
                    label, data = await commands.get()
                    if label:
                        out(f"{time.monotonic() - start:9.3f}  -> {label}: {data.hex(' ')}  |{ascii_view(data)}|")
                    for i in range(len(data)):
                        await client.write_gatt_char(SMARTROW_WRITE, data[i:i + 1], response=with_response)
                        await asyncio.sleep(0.03)

            async def poller() -> None:
                send("init", INIT_COMMAND)
                while True:
                    await asyncio.sleep(2)
                    if commands.empty():
                        send("", POLL_COMMAND)  # keep-alive; not logged

            tasks = [asyncio.create_task(writer()), asyncio.create_task(poller())]
            waits = [asyncio.create_task(dropped.wait()), asyncio.create_task(user_stop.wait())]
            try:
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks + waits:
                    task.cancel()
                if client.is_connected:
                    for char in subscribed:
                        try:
                            await client.stop_notify(char)
                        except Exception:  # noqa: BLE001
                            pass

    async def until_deadline() -> None:
        if deadline is not None:
            await asyncio.sleep(max(0.0, deadline - time.monotonic()))
            user_stop.set()

    timer = asyncio.create_task(until_deadline())
    target = device
    while not user_stop.is_set():
        try:
            await session(target)
        except Exception as exc:  # noqa: BLE001 - a failed connect: try again
            out(f"# {time.monotonic() - start:.1f} s: connection failed ({exc!r})")
        if user_stop.is_set():
            break
        # The pulley dropped the link (it happened once 2 s in, before any data): reconnect and carry on.
        out(f"# {time.monotonic() - start:.1f} s: disconnected; reconnecting (pull the handle if it's asleep)")
        await asyncio.sleep(1.0)
        try:
            target, _ = await find_pulley(args, out)
        except asyncio.TimeoutError:
            out("# pulley not found again; stopping")
            break
    timer.cancel()
    out(f"\nDisconnecting cleanly after {time.monotonic() - start:.1f} s, {count} notifications")
    if flash:
        flash.set(True)  # leave the screen lit for its own service
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
