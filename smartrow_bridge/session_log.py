"""Small on-disk record of each bridge run that survives shutdown.

The Pi's journal lives in RAM, so after "Pi Off" nothing is left of the last row.
This keeps only the milestones - pulley and app connections, the once-a-minute
data-flow summary, warnings - in a size-capped file on the SD card: roughly one
short write a minute while rowing. tools/health_check.sh reads it.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler

DEFAULT_PATH = "/var/lib/smartrow-bridge/sessions.log"
MAX_BYTES = 256 * 1024  # ~25 hours of rowing; one older file is kept on rotation
START_MARKER = "---- bridge started"

MILESTONES = (
    "Central -> ",
    "Connected to pulley",
    "Advertising '",
    "SmartRow app subscribed",
    "Rower Data subscribed",
    "Central disconnected",
    "Data flow",
    "Interval ",
    "Session summary saved",
    "Heart rate strap connected",
    "intervals.icu note added",
)


class MilestoneFilter(logging.Filter):
    """Pass warnings and above, plus the INFO lines that prove a session happened."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        message = record.getMessage()
        return any(marker in message for marker in MILESTONES)


def install(path: str, logger_name: str = "smartrow_bridge") -> logging.Handler | None:
    """Attach the session record to the bridge's logger; returns None if the file can't be opened."""
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=1, encoding="utf-8")
    except OSError as exc:
        logging.getLogger(__name__).warning("Session record disabled (%s): %s", path, exc)
        return None

    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    handler.addFilter(MilestoneFilter())
    handler.stream.write(f"{START_MARKER} {datetime.now().isoformat(timespec='seconds')} ----\n")
    handler.flush()
    logging.getLogger(logger_name).addHandler(handler)
    return handler
