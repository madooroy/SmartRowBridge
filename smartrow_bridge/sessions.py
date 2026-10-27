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
from datetime import datetime, timedelta
from pathlib import Path

from .coach import IntervalCoach
from .protocol import RowingMetrics

log = logging.getLogger(__name__)

DEFAULT_DIR = "/var/lib/smartrow-bridge/sessions"
VERSION = 1
MIN_STROKES = 30  # fewer is a few pulls on the handle, not a session
END_AFTER_S = 120.0  # no stroke for this long: the session is over (it reopens if rowing resumes)
REFRESH_S = 300.0  # while rowing, rewrite the file this often
KEEP = 500  # newest session files kept (~10 kB each): a year or two of rowing, for the progress view
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


# ---------------------------------------------------------------- progress over weeks

LONG_EFFORT_S = 90.0  # efforts at least this long are "longer" (where shape slipped first)


def _mean(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _weighted_finish(intervals: list[dict]) -> float | None:
    total = sum(i["duration_s"] for i in intervals)
    return sum(i["finish_pct"] * i["duration_s"] for i in intervals) / total if total else None


def session_point(record: dict) -> dict:
    """One session's numbers for the progress charts."""
    iv = record.get("intervals") or []
    long = [i for i in iv if i["duration_s"] >= LONG_EFFORT_S]
    short = [i for i in iv if i["duration_s"] < LONG_EFFORT_S]
    hr = record.get("heart_rate") or {}
    recoveries = [i.get("hr_recovery") for i in iv if i.get("hr_recovery") is not None]
    return {
        "id": record.get("id"),
        "started_at": record.get("started_at"),
        "rowing_s": record.get("rowing_s", 0),
        "distance_m": record.get("distance_m", 0),
        "avg_power_w": record.get("avg_power_w"),
        "steady_power_w": record.get("steady_power_w"),
        "steady_length_cm": record.get("steady_length_cm"),
        "intervals": len(iv),
        "good": sum(1 for i in iv if i["rating"] == "good" and not i.get("legs_late")),
        "short": sum(1 for i in iv if i["rating"] == "short"),
        "interval_power_w": round(_mean([i["power_w"] for i in iv])) if iv else None,
        "finish_long": round(_weighted_finish(long)) if long else None,
        "finish_short": round(_weighted_finish(short)) if short else None,
        "hr_avg": hr.get("avg"),
        "watts_per_beat": hr.get("watts_per_beat"),
        "drift_pct": hr.get("drift_pct"),
        "hr_recovery": round(_mean(recoveries)) if recoveries else None,
    }


def _trend(points: list[dict], key: str) -> tuple[float, float] | None:
    """Average of the first and of the last third of the sessions that have `key` (oldest first)."""
    values = [p[key] for p in points if p.get(key) is not None]
    if len(values) < 2:
        return None
    third = max(1, len(values) // 3)
    return sum(values[:third]) / third, sum(values[-third:]) / third


def _day(epoch: float) -> str:
    d = datetime.fromtimestamp(epoch)
    return f"{d.day} {d.strftime('%b')}"


def _signed_pct(value: float) -> str:
    """+11%, −15% (a real minus sign, as elsewhere)."""
    return f"{value:+.0f}%".replace("-", "\u2212")


def progress_findings(points: list[dict], weeks: int | None, now: float | None = None) -> list[str]:
    if not points:
        return ["No saved sessions in this period yet."]
    out = []
    km = sum(p["distance_m"] for p in points) / 1000
    span = f"in the last {weeks} weeks" if weeks else "in all"
    # Sessions a week since the first one in the period (not over weeks before you started).
    now = time.time() if now is None else now
    active_weeks = max(1.0, (now - points[0]["started_at"]) / (7 * 86400))
    if weeks:
        active_weeks = min(active_weeks, weeks)
    rate = f" ({len(points) / active_weeks:.1f} a week)"
    out.append(f"{len(points)} session{'s' if len(points) > 1 else ''} and {km:.0f} km {span}{rate}.")
    if len(points) < 2:
        return out

    t = _trend(points, "steady_power_w")
    if t:
        a, b = t
        change = (b - a) / a * 100 if a else 0
        out.append(f"Steady power went from about {a:.0f} W to {b:.0f} W ({_signed_pct(change)})." if abs(change) >= 3
                   else f"Steady power held at about {b:.0f} W.")
    t = _trend(points, "watts_per_beat")
    if t:
        a, b = t
        change = (b - a) / a * 100 if a else 0
        if change >= 3:
            out.append(f"Watts per heartbeat at steady pace rose from {a:.2f} to {b:.2f} ({_signed_pct(change)}): "
                       f"more power for the same heart rate, a sign of growing aerobic fitness.")
        elif change <= -3:
            out.append(f"Watts per heartbeat at steady pace fell from {a:.2f} to {b:.2f} ({_signed_pct(change)}): "
                       f"more heart rate for the same power, which often means fatigue, heat or a hard week.")
        else:
            out.append(f"Watts per heartbeat at steady pace held at about {b:.2f}.")
    long = [p for p in points if p.get("finish_long") is not None]
    if len(long) == 1:
        p = long[0]
        out.append(f"Longer efforts (1:30 or more) so far: finish {_signed_pct(p['finish_long'])} on "
                   f"{_day(p['started_at'])}; the aim is within 5% of steady.")
    elif long:
        a, b = _trend(points, "finish_long")
        if b - a >= 3:
            out.append(f"Finish in longer efforts improved from {_signed_pct(a)} to {_signed_pct(b)}.")
        elif a - b >= 3:
            out.append(f"Finish in longer efforts slipped from {_signed_pct(a)} to {_signed_pct(b)}.")
        else:
            out.append(f"Finish in longer efforts is steady at about {_signed_pct(b)}"
                       + ("." if b >= -5 else ": still the thing to work on."))
    t = _trend(points, "interval_power_w")
    if t and t[0] and abs(t[1] - t[0]) / t[0] >= 0.03:
        out.append(f"Interval power went from about {t[0]:.0f} W to {t[1]:.0f} W.")
    return out


def progress(directory: str, weeks: int | None = None, now: float | None = None) -> dict:
    """Trends over the saved sessions: per session, per week, and a few plain sentences."""
    now = time.time() if now is None else now
    oldest = now - weeks * 7 * 86400 if weeks else 0
    points = []
    for entry in reversed(list_sessions(directory)):  # oldest first
        if not entry["complete"] or (entry.get("started_at") or 0) < oldest:
            continue
        record = read_session(directory, entry["id"])
        if record is not None and record.get("rowing_s", 0) >= 300:
            points.append(session_point(record))
    by_week: dict[str, dict] = {}
    for p in points:
        day = datetime.fromtimestamp(p["started_at"])
        monday = (day - timedelta(days=day.weekday())).strftime("%Y-%m-%d")
        w = by_week.setdefault(monday, {"week": monday, "sessions": 0, "distance_m": 0, "rowing_s": 0})
        w["sessions"] += 1
        w["distance_m"] += p["distance_m"]
        w["rowing_s"] += p["rowing_s"]
    return {"weeks": weeks, "sessions": points, "by_week": sorted(by_week.values(), key=lambda w: w["week"]),
            "findings": progress_findings(points, weeks, now)}
