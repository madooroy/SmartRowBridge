#!/usr/bin/env python3
r"""TF-Luna bench test: live distance from the lidar on the laptop.

Wiring (by pin position, not colour; lens side, pins 1-6):
    1 +5V  -> adapter 5V        4 GND -> adapter GND
    3 TXD  -> adapter RXD       2 RXD -> adapter TXD
    5 CFG and 6 MUX left open (open CFG = serial mode)

Needs pyserial once:   python -m pip install pyserial

Run (Windows, from the repo folder; on the Pi use python3 and --port /dev/ttyUSB0,
with the bridge stopped, or the bridge and this script will fight over the port):
    python tools\tfluna_bench.py                 live readout, finds the port by itself
    python tools\tfluna_bench.py --port COM5     if it picks the wrong port
    python tools\tfluna_bench.py --list          list serial ports and stop
    python tools\tfluna_bench.py --log bench.csv also save every reading
    python tools\tfluna_bench.py --rate 250      ask for 250 readings/s (not saved:
                                           back to the default at power-off)
Ctrl+C stops and prints a summary.

Bench checks worth doing: hold a matte white card at 25, 50 and 100 cm
(tape measure) and note the reading and its 'noise' (spread over the last
second); try the foam board and a shiny surface; slide the card back and
forth by hand to see it follow.
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
import time
from collections import deque

FRAME_HEAD = b"\x59\x59"
FRAME_LEN = 9
WEAK_SIGNAL = 100        # Benewake: strength below 100 = distance unreliable
SATURATED = 65535        # strength 65535 = too much light, unreliable
MIN_RANGE_CM = 20        # nearer than this isn't measured properly

# Usual USB-to-serial chips (CH340, CP210x, PL2303, FTDI), by USB vendor id.
ADAPTER_VIDS = {0x1A86, 0x10C4, 0x067B, 0x0403}


# ---------------------------------------------------------------- parsing

class FrameParser:
    """Turns the byte stream into readings. Default 9-byte frame:
    59 59 DistL DistH AmpL AmpH TempL TempH Checksum (sum of the first 8)."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.bad_checksum = 0
        self.bytes_in = 0

    def feed(self, data: bytes) -> list[tuple[int, int, float]]:
        self.bytes_in += len(data)
        self.buf += data
        out = []
        while True:
            i = self.buf.find(FRAME_HEAD)
            if i < 0:
                # keep a trailing 0x59 in case the next byte completes a header
                del self.buf[:-1]
                break
            if i:
                del self.buf[:i]
            if len(self.buf) < FRAME_LEN:
                break
            f = self.buf[:FRAME_LEN]
            if sum(f[:8]) & 0xFF != f[8]:
                self.bad_checksum += 1
                del self.buf[:1]          # resync from the next byte
                continue
            dist = f[2] | f[3] << 8
            amp = f[4] | f[5] << 8
            temp = (f[6] | f[7] << 8) / 8 - 256
            out.append((dist, amp, temp))
            del self.buf[:FRAME_LEN]
        return out


def checksum(cmd: bytes) -> bytes:
    return cmd + bytes([sum(cmd) & 0xFF])


def rate_command(hz: int) -> bytes:
    return checksum(bytes([0x5A, 0x06, 0x03, hz & 0xFF, hz >> 8 & 0xFF]))


VERSION_COMMAND = checksum(b"\x5A\x04\x01")      # 5A 04 01 5F


def find_version(data: bytes) -> str | None:
    """Reply to the version command: 5A 07 01 V1 V2 V3 SUM -> 'V3.V2.V1'."""
    i = data.find(b"\x5A\x07\x01")
    if i < 0 or len(data) < i + 7:
        return None
    r = data[i:i + 7]
    if sum(r[:6]) & 0xFF != r[6]:
        return None
    return f"{r[5]}.{r[4]}.{r[3]}"


def quality(dist: int, amp: int) -> str:
    if amp == SATURATED:
        return "TOO BRIGHT"
    if amp < WEAK_SIGNAL:
        return "WEAK"
    if dist < MIN_RANGE_CM:
        return "TOO CLOSE"
    return "ok"


# ---------------------------------------------------------------- ports

def list_ports():
    from serial.tools import list_ports as lp
    return sorted(lp.comports(), key=lambda p: p.device)


def pick_port() -> str | None:
    ports = list_ports()
    adapters = [p for p in ports if p.vid in ADAPTER_VIDS]
    if len(adapters) == 1:
        return adapters[0].device
    if len(ports) == 1:
        return ports[0].device
    return None


# ---------------------------------------------------------------- simulator

class FakeLidar:
    """Stand-in for the serial port (--simulate): a card moving 25-110 cm."""

    def __init__(self, hz: int = 100) -> None:
        self.hz = hz
        self.t0 = time.perf_counter()
        self.sent = 0
        self.pending = b""
        self.in_waiting = 0

    def write(self, data: bytes) -> None:
        if data == VERSION_COMMAND:
            self.pending += checksum(b"\x5A\x07\x01\x03\x00\x02")
        elif data[:3] == b"\x5A\x06\x03":
            self.hz = data[3] | data[4] << 8
            self.pending += data

    def read(self, n: int) -> bytes:
        time.sleep(0.01)
        due = int((time.perf_counter() - self.t0) * self.hz)
        out, self.pending = self.pending, b""
        while self.sent < due:
            t = self.sent / self.hz
            d = int(67 + 42 * math.sin(2 * math.pi * t / 2.2))
            amp = 2400 - 15 * d
            temp = int((38 + 256) * 8)
            f = bytes([0x59, 0x59, d & 0xFF, d >> 8, amp & 0xFF, amp >> 8,
                       temp & 0xFF, temp >> 8])
            out += checksum(f)
            self.sent += 1
        return out

    def reset_input_buffer(self) -> None:
        pass

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- main

def bar(dist: int, full_cm: int = 150, width: int = 30) -> str:
    n = max(0, min(width, round(dist / full_cm * width)))
    return "#" * n + "." * (width - n)


def summary(stats: dict, parser: FrameParser) -> str:
    n = stats["n"]
    secs = time.perf_counter() - stats["t_start"]
    lines = [f"\n\n{n} readings in {secs:.1f} s"
             + (f" ({n / secs:.0f} per second)" if secs > 0 else "")]
    d = stats["good_dists"]
    if d:
        lines.append(f"Distance (good readings): {min(d)}-{max(d)} cm, "
                     f"mean {statistics.fmean(d):.1f} cm")
    lines.append(f"Good {stats['good']}, weak {stats['weak']}, "
                 f"too bright {stats['bright']}, too close {stats['close']}, "
                 f"bad checksums {parser.bad_checksum}")
    gaps = stats["gaps"]
    if len(gaps) > 10:
        gaps_ms = sorted(g * 1000 for g in gaps)
        lines.append(f"Arrival gaps on the laptop: median "
                     f"{statistics.median(gaps_ms):.1f} ms, 99th pct "
                     f"{gaps_ms[int(len(gaps_ms) * 0.99) - 1]:.1f} ms, "
                     f"longest {gaps_ms[-1]:.1f} ms")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="TF-Luna live distance bench test")
    ap.add_argument("--port", help="serial port, e.g. COM5 (default: find it)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--rate", type=int, help="readings per second to ask for "
                    "(1-250; not saved, back to default at power-off)")
    ap.add_argument("--log", help="save every reading to this CSV file")
    ap.add_argument("--seconds", type=float, help="stop after this long")
    ap.add_argument("--list", action="store_true", help="list ports and stop")
    ap.add_argument("--simulate", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    if a.list:
        ports = list_ports()
        if not ports:
            print("No serial ports. Is the USB adapter plugged in?")
        for p in ports:
            tag = "  <- USB-serial adapter" if p.vid in ADAPTER_VIDS else ""
            print(f"{p.device}: {p.description}{tag}")
        return 0

    if a.simulate:
        port, name = FakeLidar(), "simulated lidar"
    else:
        try:
            import serial
        except ImportError:
            print("pyserial is missing: python -m pip install pyserial")
            return 1
        name = a.port or pick_port()
        if not name:
            print("Couldn't tell which port is the lidar. Ports found:")
            for p in list_ports():
                print(f"  {p.device}: {p.description}")
            print("Run again with --port COMx.")
            return 1
        try:
            port = serial.Serial(name, a.baud, timeout=0.05)
        except serial.SerialException as e:
            print(f"Couldn't open {name}: {e}")
            print("Another program (e.g. a serial monitor) may have it open.")
            return 1
    print(f"Reading {name} at {a.baud} baud. Ctrl+C to stop.")

    parser = FrameParser()
    port.reset_input_buffer()

    # Firmware version: also proves the adapter's TXD -> lidar RXD wire works.
    port.write(VERSION_COMMAND)
    t_end = time.perf_counter() + 0.5
    raw = b""
    version = None
    while time.perf_counter() < t_end and not version:
        raw += port.read(256)
        version = find_version(raw)
    parser.feed(raw)
    if version:
        print(f"TF-Luna firmware {version} (both data wires work)")
    else:
        print("No reply to the version request: readings may still come, "
              "but check pin 2 (RXD) -> adapter TXD.")

    if a.rate:
        if not 1 <= a.rate <= 250:
            print("--rate must be 1-250")
            return 1
        port.write(rate_command(a.rate))
        print(f"Asked for {a.rate} readings per second (until power-off).")

    log_file = writer = None
    if a.log:
        log_file = open(a.log, "w", newline="")
        writer = csv.writer(log_file)
        writer.writerow(["t_s", "distance_cm", "strength", "temp_c", "quality"])

    stats = dict(n=0, good=0, weak=0, bright=0, close=0, good_dists=[],
                 gaps=[], t_start=time.perf_counter())
    window = deque()                 # (t, dist) over the last second
    last_t = None
    last_print = 0.0
    warned = False
    try:
        while True:
            # read what's there now (or wait for 1 byte), so arrival times
            # reflect when frames reach the laptop, not the read timeout
            data = port.read(max(1, port.in_waiting))
            now = time.perf_counter()
            for dist, amp, temp in parser.feed(data):
                t = now - stats["t_start"]
                q = quality(dist, amp)
                stats["n"] += 1
                key = {"ok": "good", "WEAK": "weak", "TOO BRIGHT": "bright",
                       "TOO CLOSE": "close"}[q]
                stats[key] += 1
                if q == "ok":
                    stats["good_dists"].append(dist)
                    window.append((now, dist))
                if last_t is not None and now > last_t:
                    stats["gaps"].append(now - last_t)
                last_t = now
                if writer:
                    writer.writerow([f"{t:.4f}", dist, amp, f"{temp:.1f}", q])
                latest = (dist, amp, temp, q)
            while window and window[0][0] < now - 1.0:
                window.popleft()

            if now - last_print >= 0.1:
                last_print = now
                elapsed = now - stats["t_start"]
                if stats["n"] == 0:
                    if elapsed > 2 and not warned:
                        warned = True
                        if parser.bytes_in == 0:
                            print("\nNothing arriving after 2 s. Check: 5V and "
                                  "GND, pin 3 (TXD) -> adapter RXD, the port "
                                  "(--list), CFG (pin 5) left open.")
                        else:
                            print(f"\n{parser.bytes_in} bytes but no readings: "
                                  "wrong baud rate? (default 115200)")
                    continue
                dist, amp, temp, q = latest
                ds = [d for _, d in window]
                noise = f"{statistics.pstdev(ds):4.1f}" if len(ds) > 5 else "  - "
                rate = len(window)
                # kept under 80 characters: a line wider than the window
                # wraps, and then \r can't overwrite it in place
                line = (f"\r{dist:4d} cm {dist / 2.54:5.1f} in "
                        f"|{bar(dist, width=16)}| str {amp:5d} {q:<6} "
                        f"noise {noise} {rate:3d}/s {temp:3.0f}C ")
                sys.stdout.write(line)
                sys.stdout.flush()
            if a.seconds and now - stats["t_start"] >= a.seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        port.close()
        if log_file:
            log_file.close()
    print(summary(stats, parser))
    if a.log:
        print(f"Saved to {a.log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
