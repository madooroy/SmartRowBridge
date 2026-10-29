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


WEB_UNIT = Path("/etc/systemd/system/smartrow-web.service")


def _address() -> str:
    """For the status screen's corner: the web page's address once it is set up
    (tools/setup_web.sh), by name and by IP address, as Android does not always find
    .local names; otherwise the host name and IP address, so you can find the Pi for SSH."""
    host = socket.gethostname()
    try:
        ips = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=2).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        ips = []
    ipv4 = [ip for ip in ips if "." in ip]
    return address_lines(host, ipv4[0] if ipv4 else None, _env("SRB_WEB_PORT", "8080") if WEB_UNIT.exists() else None)


def address_lines(host: str, ip: str | None, web_port: str | None) -> str:
    if web_port:
        lines = [f"http://{host}.local:{web_port}"]
        if ip:
            lines.append(f"http://{ip}:{web_port}")
        return "\n".join(lines)
    return f"{host}  {ip}" if ip else host


def _system_stopping() -> bool:
    try:
        state = subprocess.run(["systemctl", "is-system-running"], capture_output=True, text=True,
                               timeout=2).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return False
    return state == "stopping"


def _record(message: str) -> None:
    """Also note it in the bridge's session record, which survives shutdown (the journal does not)."""
    path = _env("SRB_SESSION_LOG", "/var/lib/smartrow-bridge/sessions.log")
    if path.lower() == "off":
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} WARNING smartrow_bridge.display: {message}\n")
    except OSError:
        pass


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
    first = {name: hat.pressed(name) for name in ("A", "B", "X", "Y")}
    log.info("Buttons at start (True = pressed): %s", first)
    if first["A"]:
        log.warning("Button A reads as pressed at start; it is ignored until it is released")
        _record("Button A read as pressed when the screen started; ignored until released")

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
            if frame.request:  # Y (done) or A with uploads to go: ask the bridge to finish the session
                try:
                    status_file.write(status_file.request_path(path), frame.request)
                except OSError as exc:
                    log.warning("Could not pass the request to the bridge: %s", exc)
            hat.show(frame.image)
            hat.set_led(frame.led)
            last_draw = now
            if frame.switch_off:
                log.info("A held: switching off")
                _record("Button A held: switching the Pi off")
                subprocess.Popen(["systemctl", "poweroff"])
        last_buttons = buttons
        time.sleep(TICK_S)

    hat.set_led(None)
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
    hat.set_led(None)
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

    addr = address_lines("raspberrypi", "192.168.1.23", "8080")
    rowing = dict(distance_m=1834, power_w=168, stroke_rate_spm=24.5, stroke_count=212,
                  pace_s_per_500m=118, elapsed_s=455)
    # Interval coach, with average curves from a real 45-minute session.
    steady = [131, 210, 286, 344, 389, 422, 445, 465, 467, 455, 430, 401, 375, 340, 295, 246, 196, 148, 104, 68, 32,
              7, 0, 0]
    short = [176, 295, 414, 512, 593, 656, 696, 714, 701, 666, 613, 550, 492, 416, 339, 269, 203, 146, 95, 51, 14, 2,
             0, 0]
    full = [143, 232, 325, 401, 467, 520, 566, 593, 612, 604, 575, 537, 502, 461, 402, 337, 272, 211, 153, 100, 63,
            28, 0, 0]

    faded = [round(v * (0.7 if 11 <= i <= 19 else 1)) for i, v in enumerate(full)]  # the finish fading
    late = [40, 80, 131, 210, 286, 344, 389, 422, 445, 465, 467, 455, 430, 401, 375, 340, 295, 246, 196, 148, 104,
            68, 32, 7]  # steady shape, pushed 3 samples later: a slow push off the catch

    # A steady stroke a little different from the average steady shape, as real strokes are.
    steady_stroke = [round(v * (1.06 if i < 9 else 0.95)) for i, v in enumerate(steady)]

    def coached(live=None, last=None, show_summary=False):
        status = st("connected", "SmartRow-34", True, True, True, **rowing)
        status["coach"] = {"mode": "interval" if live else "steady", "steady_power_w": 111, "intervals": 2,
                           "live": live, "last": last, "show_summary": show_summary,
                           "stroke_curve": steady_stroke, "steady_curve": steady, "intensity": "Easy", "cue": "good"}
        return status

    def live(cue, curve, finish_pct=None, length_delta=None, balance=None, length_balance=None):
        return {"index": 3, "elapsed_s": 72, "strokes": 25, "power_w": 154, "length_cm": 112, "cue": cue,
                "finish_pct": finish_pct, "length_delta_cm": length_delta, "curve": curve, "steady_curve": steady,
                "balance": balance, "length_balance": length_balance, "intensity": "Moderate"}

    good_last = {"index": 2, "duration_s": 62, "strokes": 22, "power_w": 150, "stroke_rate_spm": 21.5,
                 "length_cm": 118.7, "length_delta_cm": 4.6, "peak_n": 623, "finish_pct": 0, "rating": "good",
                 "fade_pct": -3, "curve": full, "steady_curve": steady, "balance": 0.2, "length_balance": 1.5,
                 "intensity": "Moderate"}
    short_last = {**good_last, "index": 7, "duration_s": 182, "strokes": 64, "power_w": 156, "length_cm": 112.5,
                  "length_delta_cm": -3.6, "peak_n": 719, "finish_pct": -15, "rating": "short", "curve": short,
                  "balance": -1.4, "length_balance": -1.2}
    legs_last = {**good_last, "index": 4, "finish_pct": 14, "curve": late, "length_delta_cm": 0.0,
                 "drive_late": 4.3, "legs_late": True, "peak_at_pct": 52, "steady_peak_at_pct": 41,
                 "balance": 1.4, "length_balance": 0.0}
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
        "07a-interval-starting": screens.interval_screen(coached(live(None, full)), now, up),
        "07b-interval-good": screens.interval_screen(coached(live("good", full, 2, 4, 0.2, 1.5)), now, up),
        "07c-interval-finish-cue": screens.interval_screen(coached(live("finish", faded, -15, 0, -1.5, 0.0)), now, up),
        "07h-interval-legs-cue": screens.interval_screen(coached(live("legs", late, 14, 0, 1.4, 0.0)), now, up),
        "07i-summary-legs-late": screens.summary_screen(coached(last=legs_last, show_summary=True), now, up),
        "07d-interval-length-cue": screens.interval_screen(coached(live("length", short, -1, -5, -0.3, -1.7)), now, up),
        "07e-summary-good": screens.summary_screen(coached(last=good_last, show_summary=True), now, up),
        "07f-summary-short": screens.summary_screen(coached(last=short_last, show_summary=True), now, up),
        "07g-rowing-with-last": screens.rowing_screen(coached(last=short_last), now, up),
        "07j-rowing-coach-steady": screens.rowing_screen(coached(), now, up),
        "07k-rowing-coach-warmup": screens.rowing_screen({**coached(), "coach": {**coached()["coach"], "mode": "warmup",
                                                                              "steady_curve": [], "cue": None}}, now, up),
        "07l-rowing-easy-finish-cue": screens.rowing_screen({**coached(), "coach": {**coached()["coach"], "cue": "finish",
                                                                                 "stroke_curve": short}}, now, up),
        "07m-rowing-recovery": screens.rowing_screen({**coached(), "coach": {**coached()["coach"],
                                                                          "intensity": "Recovery"}}, now, up),
        "09-hold-to-switch-off": screens.hold_screen(0.6),
        "10-switching-off": screens.switching_off_screen(),
        "11-safe-to-unplug": screens.final_screen("poweroff"),
        "12-bridge-not-running": screens.status_screen(None, now, up, addr),
        "13-both-apps-no-web-page": screens.status_screen(st("connected", "SmartRow-34", True, True), now, up,
                                                          address_lines("raspberrypi", "192.168.1.23", None)),
        "14-strap-waiting": screens.status_screen({**st("searching"), "heart_rate": {"name": None,
                                                                                     "connected": False}}, now, up, addr),
        "15-strap-looking": screens.status_screen({**st("connected", "SmartRow-34", True, True),
                                                   "heart_rate": {"name": None, "connected": False}}, now, up, addr),
        "16-strap-connected": screens.status_screen({**st("connected", "SmartRow-34", True, True, heart_rate_bpm=96),
                                                     "heart_rate": {"name": "COROS HRM 8A2F", "connected": True}},
                                                    now, up, addr),
        "17-strap-no-web-page": screens.status_screen({**st("connected", "SmartRow-34", True, False, heart_rate_bpm=96),
                                                       "heart_rate": {"name": "COROS HRM 8A2F", "connected": True}},
                                                      now, up, address_lines("raspberrypi", "192.168.1.23", None)),
    }
    # Y (done) and A with uploads to go.
    from .app import Controller

    def with_session(st, strokes=420):
        st["coach"] = {**(st.get("coach") or {}), "session_strokes": strokes}
        return st

    def uploads(states, sending=None):
        return {"configured": list(states), "sending": sending, "waiting": 1, "retrying": 0, "error": None,
                "latest": {"id": "2026-10-28T0702", "dests": states}, "ack": "t"}

    c = Controller()
    rowing_y = c.step(with_session(st("connected", "SmartRow-34", True, True, True, **rowing)), now, up,
                      {"A": False, "B": False, "X": False, "Y": False}).image
    interval_y = screens.add_tab(screens.interval_screen(coached(live("good", full, 2, 4, 0.2, 1.5)), now, up),
                                 "Y", "check", "Hold: done")
    fin = {"token": "t", "session": "2026-10-28T0702"}
    going = {**st("connected"), "finish": fin,
             "uploads": uploads({"intervals.icu activity": "sent", "Strava": "waiting"}, sending="Strava")}
    done = {**st("connected"), "finish": fin,
            "uploads": uploads({"intervals.icu activity": "sent", "Strava": "sent"})}
    offline = {**st("connected"), "finish": fin,
               "uploads": {**uploads({"intervals.icu activity": "sent", "Strava": "retrying"}), "retrying": 1}}
    shots.update({
        "18-rowing-y-done": rowing_y,
        "19-interval-y-done": interval_y,
        "20-hold-y": screens.hold_screen(0.6, "Keep holding: done rowing"),
        "21-finished-uploading": screens.finish_screen(going, fin["session"], True),
        "22-finished-sent": screens.finish_screen(done, fin["session"], True),
        "23-off-uploading-first": screens.upload_off_screen(going, False),
        "24-off-closing-offline": screens.upload_off_screen(offline, True),
    })
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
