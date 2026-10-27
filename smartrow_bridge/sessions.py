# SPDX-License-Identifier: GPL-3.0-only
"""Saved session summaries: one small JSON file per rowing session, for the web page's summary.

The bridge calls SessionRecorder.update() once a second (from its status loop). It
writes the coach's session summary (coach.IntervalCoach.session_summary) plus the
pulley's own totals to <directory>/<id>.json:

- when an interval ends, so nothing is lost if the Pi is switched off mid-session;
- every few minutes while rowing, to keep the totals current;
- once the session is over (no stroke for END_AFTER_S), marked "complete".

The id is the session's start time, e.g. 2026-10-27T0702. Only the newest KEEP files
are kept. The files are world-readable, so the web page (a separate, unprivileged
service) can read them; nothing else about the bridge depends on them.

Heart rate is not here: the strap pairs with the SmartRow app or the fitness app,
never with the pulley, so the bridge does not see it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

from .coach import IntervalCoach
from .protocol import RowingMetrics

log = logging.getLogger(__name__)

DEFAULT_DIR = "/var/lib/smartrow-bridge/sessions"
VERSION = 1
MIN_STROKES = 30  # fewer is a few pulls on the handle, not a session
END_AFTER_S = 120.0  # no stroke for this long: the session is over (it reopens if rowing resumes)
REFRESH_S = 300.0  # while rowing, rewrite the file this often
KEEP = 60  # newest session files kept
ID_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{4,6}$")


def session_id(started_at: float) -> str:
    return datetime.fromtimestamp(started_at).strftime("%Y-%m-%dT%H%M")


class SessionRecorder:
    def __init__(self, directory: str, coach: IntervalCoach) -> None:
        self.directory = Path(directory)
        self.coach = coach
        self._session: int | None = None  # coach.session being recorded
        self._id: str | None = None
        self._started_at = 0.0
        self._written: tuple | None = None  # what the last write contained (intervals, complete)
        self._written_at = 0.0
        self._failed = False

    def update(self, metrics: RowingMetrics, mono_now: float | None = None,
               wall_now: float | None = None) -> Path | None:
        """Save the current session's summary if something worth saving changed. Returns the file written."""
        mono_now = time.monotonic() if mono_now is None else mono_now
        wall_now = time.time() if wall_now is None else wall_now
        c = self.coach
        if c.first_time is None or c.strokes_total < MIN_STROKES:
            return None
        if c.session != self._session:  # a new session: name it after its first stroke
            self._session = c.session
            self._started_at = wall_now - (mono_now - c.first_time)
            self._id = session_id(self._started_at)
            self._written = None
        complete = mono_now - (c.last_time or mono_now) > END_AFTER_S
        if complete and c.effort is not None:
            c.state(mono_now)  # closes an effort that was still open when rowing stopped
        key = (len(c.intervals), complete)
        if key == self._written and (complete or wall_now - self._written_at < REFRESH_S):
            return None
        path = self._write(metrics, complete, wall_now)
        if path is not None:
            self._written, self._written_at = key, wall_now
        return path

    def _write(self, metrics: RowingMetrics, complete: bool, wall_now: float) -> Path | None:
        record = build_record(self.coach, self._started_at, wall_now, complete, metrics.distance_m,
                              metrics.elapsed_s)
        try:
            path = save(self.directory, record)
        except OSError as exc:
            if not self._failed:
                log.warning("Could not save the session summary in %s: %s", self.directory, exc)
            self._failed = True
            return None
        self._failed = False
        if complete:
            log.info("Session summary saved: %s (%d intervals)", path.name, len(record["intervals"]))
        return path


def build_record(coach: IntervalCoach, started_at: float, saved_at: float, complete: bool,
                 distance_m: int, elapsed_s: int) -> dict:
    """What a session file holds: when, the pulley's totals and the coach's session summary."""
    return {
        "version": VERSION,
        "id": session_id(started_at),
        "started_at": round(started_at),
        "saved_at": round(saved_at),
        "complete": complete,
        "distance_m": distance_m,
        "elapsed_s": elapsed_s,
        **coach.session_summary(),
    }


def save(directory: Path | str, record: dict) -> Path:
    """Write one session file in one step (world-readable), then keep only the newest KEEP."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o755)
    path = directory / f"{record['id']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    files = sorted(p for p in directory.glob("*.json") if ID_PATTERN.match(p.stem))
    for old in files[:-KEEP]:
        old.unlink(missing_ok=True)
    return path


def list_sessions(directory: str) -> list[dict]:
    """Newest first: the headline of every saved session (for the web page's history)."""
    out = []
    for path in sorted(Path(directory).glob("*.json"), reverse=True):
        if not ID_PATTERN.match(path.stem):
            continue
        record = read_session(directory, path.stem)
        if record is None:
            continue
        intervals = record.get("intervals") or []
        out.append({
            "id": record.get("id", path.stem),
            "started_at": record.get("started_at"),
            "complete": record.get("complete", False),
            "rowing_s": record.get("rowing_s", 0),
            "distance_m": record.get("distance_m", 0),
            "avg_power_w": record.get("avg_power_w", 0),
            "intervals": len(intervals),
            "short": sum(1 for i in intervals if i.get("rating") == "short"),
        })
    return out


def read_session(directory: str, session: str) -> dict | None:
    """One saved session, or None. The id is checked, so it can't reach outside the directory."""
    if not ID_PATTERN.match(session):
        return None
    try:
        return json.loads((Path(directory) / f"{session}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
