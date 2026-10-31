"""A small status file for anything that wants to show what the bridge is doing.

Once a second the bridge writes /run/smartrow-bridge/status.json (in RAM, so no SD
card wear). The optional screen (smartrow_bridge.display) reads it; the bridge does
not depend on anyone reading it, and a reader never talks to the bridge directly.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict

from .protocol import RowingMetrics

log = logging.getLogger(__name__)

DEFAULT_PATH = "/run/smartrow-bridge/status.json"
VERSION = 1

# Stages the bridge goes through after it starts.
STARTING = "starting"  # finding and preparing the two radios
SEARCHING = "searching"  # scanning for the pulley (it sleeps until the handle is pulled)
CONNECTED = "connected"  # pulley connected, virtual devices advertising
RECONNECTING = "reconnecting"  # pulley link lost, scanning again


def pulley_label(name: str, manufacturer_data: dict[int, bytes]) -> str:
    """The name the SmartRow app shows, e.g. "SmartRow-34" (the pulley ID is the manufacturer data)."""
    pulley_id = manufacturer_data.get(0x1235, b"")
    text = pulley_id.decode("ascii", "replace").strip("\x00 ") if pulley_id else ""
    return f"{name}-{text}" if text.isprintable() and text else name


def build(
    stage: str,
    pulley: str | None,
    smartrow_app: bool,
    fitness_app: bool,
    clone_name: str | None,
    fitness_name: str,
    metrics: RowingMetrics,
    rowing: bool,
    now: float | None = None,
    coach: dict | None = None,
    heart_rate: dict | None = None,
    uploads: dict | None = None,
    finish: dict | None = None,
) -> dict:
    m = asdict(metrics)
    for key in ("last_stroke_time", "stopped"):
        m.pop(key)
    return {
        "version": VERSION,
        "time": time.time() if now is None else now,
        "stage": stage,
        "pulley": pulley,
        "smartrow_app": smartrow_app,
        "fitness_app": fitness_app,
        "clone_name": clone_name,
        "fitness_name": fitness_name,
        "rowing": rowing,
        "metrics": m,
        "coach": coach,  # smartrow_bridge.coach.IntervalCoach.state(), or None when off
        "heart_rate": heart_rate,  # {"name", "connected"} of the bridge's strap, or None when off
        "uploads": uploads,  # smartrow_bridge.uploads.Uploader.snapshot(), or None without session saving
        "finish": finish,  # the last "Done" request handled: {"token", "session"} (see request_path)
    }


def write(path: str, status: dict) -> None:
    """Replace the file in one step, so a reader never sees half of it."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(status, f)
    os.replace(tmp, path)


def request_path(status_path: str) -> str:
    """Where the screen leaves a request for the bridge (button Y: "Done"), beside the status file."""
    return os.path.join(os.path.dirname(status_path) or ".", "request.json")


def send_request(status_path: str, action: str) -> str:
    """Leave a request for the bridge; returns its token (echoed in the status when handled)."""
    token = f"{time.time():.3f}"
    write(request_path(status_path), {"action": action, "token": token})
    return token


def take_request(status_path: str) -> dict | None:
    """The waiting request, if any (removed, so it is handled once)."""
    path = request_path(status_path)
    request = read(path)
    if request is not None:
        try:
            os.unlink(path)
        except OSError:
            pass
    return request


def read(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
