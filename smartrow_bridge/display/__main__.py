"""Optional screen for the bridge: Pimoroni Display HAT Mini.

    python -m smartrow_bridge.display                 run on the HAT (the smartrow-display service)
    python -m smartrow_bridge.display --preview DIR   save every screen as PNG, no hardware needed
    python -m smartrow_bridge.display --final KIND    last screen before power-off (shutdown hook)

It only reads the bridge's status file, so the bridge runs the same with or without it.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

from .. import status as status_file
from . import screens
from .app import Controller

log = logging.getLogger("smartrow_bridge.display")

TICK_S = 0.05  # button polling
REDRAW_S = 0.5  # screen refresh while no button is in use
ADDRESS_EVERY_S = 15.0


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _uptime() -> float:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 1e9


def _address() -> str:
    """Host name and IP address, so you can find the Pi for SSH."""
    host = socket.gethostname()
    try:
        ips = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=2).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        ips = []
    ipv4 = [ip for ip in ips if "." in ip]
    return f"{host}  {ipv4[0]}" if ipv4 else host


def _system_stopping() -> bool:
    try:
        state = subprocess.run(["systemctl", "is-system-running"], capture_output=True, text=True,
                               timeout=2).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return False
    return state == "stopping"


def run() -> None:
    from .hat import DisplayHat  # hardware libraries only when there is hardware

    path = _env("SRB_STATUS_FILE", status_file.DEFAULT_PATH)
    use_led = _env("SRB_DISPLAY_LED", "1") not in ("0", "false", "no")
    hold_s = float(_env("SRB_DISPLAY_HOLD_S", "2"))

    hat = DisplayHat(led=use_led)
    controller = Controller(hold_s=hold_s)
    stop = False

    def on_term(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    log.info("Display running; status file %s", path)

    address, address_at = "", 0.0
    last_draw, last_buttons = 0.0, {}
    while not stop:
        now = time.monotonic()
        buttons = {name: hat.pressed(name) for name in ("A", "B", "X", "Y")}
        busy = buttons != last_buttons or buttons["A"]
        if busy or now - last_draw >= REDRAW_S:
            if now - address_at >= ADDRESS_EVERY_S:
                address, address_at = _address(), now
            frame = controller.step(status_file.read(path), time.time(), _uptime(), buttons, address)
            hat.show(frame.image)
            hat.set_led(*frame.led)
            last_draw = now
            if frame.switch_off:
                log.info("A held: switching off")
                subprocess.Popen(["systemctl", "poweroff"])
        last_buttons = buttons
        time.sleep(TICK_S)

    hat.set_led(False, False, False)
    if controller.switching_off or _system_stopping():
        # Shutting down (from here, SSH or the iPhone shortcut): say so; the shutdown
        # hook draws the final "Safe to unplug" screen at the very end.
        hat.show(screens.switching_off_screen())
    else:
        hat.show(screens.blank())
        hat.backlight(False)
    log.info("Display stopped")


def final(kind: str) -> None:
    from .hat import DisplayHat

    hat = DisplayHat(buttons=False, led=True)
    hat.set_led(False, False, False)
    hat.show(screens.final_screen(kind))
    # The kernel may reset a GPIO line once its owner exits, which could switch the
    # backlight off. A child keeps the lines open until the Pi stops; the hook (our
    # parent) returns at once so the shutdown is not held up.
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    while True:
        time.sleep(3600)


def preview(directory: str) -> None:
    """Every screen with example data, as PNG files."""
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    now, up = 1_000_000.0, 600.0

    def st(stage, pulley=None, smartrow=False, fitness=False, rowing=False, **metrics):
        m = {"distance_m": 0, "power_w": 0, "stroke_rate_spm": 0.0, "stroke_count": 0, "pace_s_per_500m": 0,
             "avg_power_w": 0, "avg_pace_s_per_500m": 0, "elapsed_s": 0, "stroke_length_cm": 0}
        m.update(metrics)
        return {"version": 1, "time": now, "stage": stage, "pulley": pulley, "smartrow_app": smartrow,
                "fitness_app": fitness, "clone_name": "SmartRow", "fitness_name": "PiRower",
                "rowing": rowing, "metrics": m}

    addr = "raspberrypi  192.168.1.23"
    rowing = dict(distance_m=1834, power_w=168, stroke_rate_spm=24.5, stroke_count=212,
                  pace_s_per_500m=118, elapsed_s=455)
    shots = {
        "01-starting-up": screens.status_screen(None, now, 12.0, addr),
        "02-preparing": screens.status_screen(st("starting"), now, up, addr),
        "03-waiting-for-pulley": screens.status_screen(st("searching"), now, up, addr),
        "04-pulley-connected": screens.status_screen(st("connected", "SmartRow-34"), now, up, addr),
        "05-both-apps": screens.status_screen(st("connected", "SmartRow-34", True, True), now, up, addr),
        "06-pulley-lost": screens.status_screen(st("reconnecting", "SmartRow-34", True, True), now, up, addr),
        "07-rowing": screens.rowing_screen(st("connected", "SmartRow-34", True, True, True, **rowing), now, up),
        "08-paused": screens.rowing_screen(
            st("connected", "SmartRow-34", True, True, False, **{**rowing, "power_w": 0, "stroke_rate_spm": 0,
                                                                "pace_s_per_500m": 0}), now, up),
        "09-hold-to-switch-off": screens.hold_screen(0.6),
        "10-switching-off": screens.switching_off_screen(),
        "11-safe-to-unplug": screens.final_screen("poweroff"),
        "12-bridge-not-running": screens.status_screen(None, now, up, addr),
    }
    for name, image in shots.items():
        image.save(out / f"{name}.png")
    print(f"{len(shots)} screens saved in {out}")


def main() -> None:
    p = argparse.ArgumentParser(prog="smartrow-display", description=__doc__.splitlines()[0])
    p.add_argument("--preview", metavar="DIR", help="save every screen as PNG files (no hardware needed)")
    p.add_argument("--final", metavar="KIND", help="draw the last screen (poweroff, halt or reboot) and exit")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if args.preview:
        preview(args.preview)
    elif args.final:
        final(args.final)
    else:
        run()


if __name__ == "__main__":
    main()
