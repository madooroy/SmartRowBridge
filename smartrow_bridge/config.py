"""Command-line / environment configuration (env vars come from /etc/default/smartrow-bridge)."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass

from . import session_log, sessions, status


@dataclass
class Config:
    central_adapter: str | None
    peripheral_adapter: str | None
    central_mac: str | None
    peripheral_mac: str | None
    peripheral_transport: str | None
    pulley_address: str | None
    pulley_name: str
    clone_name: str | None
    clone_address: str | None
    fitness_name: str
    fitness_address: str | None
    advertise_pulley_id: bool
    drive_without_tablet: bool
    sniff_log: str | None
    session_log: str
    status_file: str
    gatt_log: bool
    coach: bool
    coach_start_pct: float
    coach_max_min: float
    coach_sensitivity: str
    coach_ftp: str
    session_dir: str
    heart_rate: str
    heart_rate_to_fitness: bool
    intervals_key_file: str
    intervals_upload: str
    strava_file: str
    strava_sport_type: str
    log_level: str


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or default


def parse(argv: list[str] | None = None) -> Config:
    p = argparse.ArgumentParser(prog="smartrow-bridge", description="SmartRow -> SmartRow app + Peloton (FTMS) BLE bridge")
    p.add_argument("--central-adapter", default=_env("SRB_CENTRAL_ADAPTER"),
                   help="adapter connecting to the pulley, e.g. hci0 (default: built-in radio)")
    p.add_argument("--peripheral-adapter", default=_env("SRB_PERIPHERAL_ADAPTER"),
                   help="adapter hosting the virtual devices, e.g. hci1 (default: USB dongle)")
    p.add_argument("--central-mac", default=_env("SRB_CENTRAL_MAC"),
                   help="bind the central to the radio with this BD address (built-in chip)")
    p.add_argument("--peripheral-mac", default=_env("SRB_PERIPHERAL_MAC"),
                   help="bind the peripheral to the radio with this BD address (TP-Link UB500)")
    p.add_argument("--peripheral-transport", default=_env("SRB_PERIPHERAL_TRANSPORT"),
                   help="Bumble transport override, e.g. 'usb:0' (default: hci-socket:<index>)")
    p.add_argument("--pulley-address", default=_env("SRB_PULLEY_ADDRESS"),
                   help="MAC of the physical pulley (default: first device matching service/name)")
    p.add_argument("--pulley-name", default=_env("SRB_PULLEY_NAME", "SmartRow"),
                   help="name substring used to find the pulley")
    p.add_argument("--clone-name", default=_env("SRB_CLONE_NAME"),
                   help="advertised name of the virtual device (default: the pulley's own name)")
    p.add_argument("--clone-address", default=_env("SRB_CLONE_ADDRESS"),
                   help="static random address of the pulley clone, top two bits set "
                        "(default: derived from the dongle's MAC)")
    p.add_argument("--fitness-name", default=_env("SRB_FITNESS_NAME", "PiRower"),
                   help="name Peloton and other FTMS apps see (default: PiRower)")
    p.add_argument("--fitness-address", default=_env("SRB_FITNESS_ADDRESS"),
                   help="static random address of the FTMS rower, different from the clone's "
                        "(default: derived from the dongle's MAC)")
    p.add_argument("--no-pulley-id", dest="advertise_pulley_id", action="store_false",
                   default=_env("SRB_ADVERTISE_PULLEY_ID", "1") not in ("0", "false", "no"),
                   help="test: advertise the clone without the pulley's manufacturer data, "
                        "as a device that cannot send it (iOS) would")
    p.add_argument("--no-drive", dest="drive_without_tablet", action="store_false",
                   default=_env("SRB_DRIVE_WITHOUT_TABLET", "1") not in ("0", "false", "no"),
                   help="never send init/poll/KEYLOCK to the pulley; rely on the SmartRow app")
    p.add_argument("--sniff-log", default=_env("SRB_SNIFF_LOG"),
                   help="append every raw pulley packet (hex + ascii) to this file")
    p.add_argument("--session-log", default=_env("SRB_SESSION_LOG", session_log.DEFAULT_PATH),
                   help="milestone record that survives shutdown ('off' to disable)")
    p.add_argument("--status-file", default=_env("SRB_STATUS_FILE", status.DEFAULT_PATH),
                   help="status file for the optional screen, rewritten every second ('off' to disable)")
    p.add_argument("--gatt-log", action="store_true",
                   default=_env("SRB_GATT_LOG", "0") in ("1", "true", "yes"),
                   help="debugging: log every GATT request the apps make to the virtual devices")
    p.add_argument("--coach", dest="coach", action="store_true",
                   default=_env("SRB_COACH", "0") in ("1", "true", "yes"),
                   help="experimental: interval coach, force-curve feedback on the screen and web page")
    p.add_argument("--no-coach", dest="coach", action="store_false", help="turn the interval coach off")
    p.add_argument("--coach-start-pct", type=float, default=float(_env("SRB_COACH_START_PCT", "20")),
                   help="coach: an interval is at least this %% above your steady power (default 20)")
    p.add_argument("--coach-max-min", type=float, default=float(_env("SRB_COACH_MAX_MIN", "5")),
                   help="coach: longest effort, in minutes, to count as an interval (default 5)")
    p.add_argument("--coach-sensitivity", choices=("low", "normal", "high"),
                   default=_env("SRB_COACH_SENSITIVITY", "normal"),
                   help="coach: how readily the technique cues speak up (default normal)")
    p.add_argument("--coach-ftp", default=_env("SRB_COACH_FTP", ""),
                   help="coach: your rowing FTP in watts; empty = read it from intervals.icu (when set up), "
                        "off = none. With it, an effort is Moderate or harder (67%% of FTP and up), with no time limit")
    p.add_argument("--session-dir", default=_env("SRB_SESSION_DIR", sessions.DEFAULT_DIR),
                   help="coach: where each session's summary is saved, for the web page ('off' to disable)")
    p.add_argument("--heart-rate", default=_env("SRB_HEART_RATE", "off"),
                   help="heart rate strap for the bridge: off, auto (first strap found), a MAC address, "
                        "or part of its name such as COROS")
    p.add_argument("--no-heart-rate-to-fitness", dest="heart_rate_to_fitness", action="store_false",
                   default=_env("SRB_HEART_RATE_TO_FITNESS", "1") not in ("0", "false", "no"),
                   help="don't pass the strap's heart rate on to the fitness app")
    p.add_argument("--intervals-key-file", default=_env("SRB_INTERVALS_KEY_FILE", "/etc/smartrow-bridge/intervals.key"),
                   help="intervals.icu API key (tools/setup_intervals.sh); with it, sessions go to intervals.icu")
    p.add_argument("--intervals-upload", choices=("activity", "note", "off"),
                   default=_env("SRB_INTERVALS_UPLOAD", "activity").lower(),
                   help="what goes to intervals.icu: the session as an activity (default), a calendar note, or off")
    p.add_argument("--strava-file", default=_env("SRB_STRAVA_FILE", "/etc/smartrow-bridge/strava.json"),
                   help="Strava authorization (tools/setup_strava.sh); with it, sessions are uploaded to Strava")
    p.add_argument("--strava-sport-type", default=_env("SRB_STRAVA_SPORT_TYPE", "Rowing"),
                   help="Strava activity type for uploads (Rowing, or VirtualRow)")
    p.add_argument("--log-level", default=_env("SRB_LOG_LEVEL", "INFO"))
    return Config(**vars(p.parse_args(argv)))
