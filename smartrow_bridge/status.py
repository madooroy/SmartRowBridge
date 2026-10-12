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


def read(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
