# SPDX-License-Identifier: GPL-3.0-only
"""Saved session summaries: one small JSON file per rowing session, for the web page's summary.

The bridge calls SessionRecorder.update() once a second (from its status loop). It
writes the coach's session summary (coach.IntervalCoach.session_summary) plus the
pulley's own totals to <directory>/<id>.json:

- when an interval ends, so nothing is lost if the Pi is switched off mid-session;
- every few minutes while rowing, to keep the totals current;
- once the session is over (no stroke for END_AFTER_S), marked "complete".

The id is the session's start time, e.g. 2026-10-27T0702. Beside it, <id>.strokes.json
holds every stroke (time, distance, power, rate, heart rate, length), from which the
session's activity file is made for uploads (fitfile.py). Only the newest KEEP sessions
are kept. The files are world-readable, so the web page (a separate, unprivileged
service) can read them; nothing else about the bridge depends on them.

If the bridge is stopped (shutdown, restart), the session is saved as complete there and
then (SessionRecorder.close). If the Pi loses power instead, the file keeps what the last
write had; uploads.py treats it as finished once it has gone untouched long enough.
"""

from __future__ import annotations

import json
import logging
import os
import re
import statistics
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
        self._ended_at = 0.0
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
            self._id = session_id(wall_now - (mono_now - c.first_time))
            self._written = None
        # Wall times from the steady clock at every write, so a clock set mid-session (the Pi has
        # no clock of its own; the network sets it) puts the session at its real time.
        self._started_at = wall_now - (mono_now - c.first_time)
        self._ended_at = wall_now - (mono_now - (c.last_time or c.first_time))
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

    def close(self, metrics: RowingMetrics) -> Path | None:
        """The bridge is stopping (shutdown, restart): save the session as it is, finished."""
        c = self.coach
        if self._id is None or c.session != self._session or c.strokes_total < MIN_STROKES:
            return None
        if self._written is not None and self._written[1]:
            return None  # already saved as complete
        if c.effort is not None and c.last_time is not None:
            c.state(c.last_time + END_AFTER_S + 1)  # closes an effort still open
        path = self._write(metrics, True, time.time())
        if path is not None:
            self._written = (len(c.intervals), True)
        return path

    def finish(self, metrics: RowingMetrics) -> str | None:
        """You said you're done (Y on the screen): save the session as complete and finished,
        so it is uploaded straight away. Returns its id, or None when there is no session."""
        c = self.coach
        if self._id is None or c.session != self._session or c.strokes_total < MIN_STROKES:
            return None
        self.close(metrics)
        try:
            mark_finished(str(self.directory), self._id)
        except OSError as exc:
            log.warning("Could not mark session %s finished: %s", self._id, exc)
            return None
        return self._id

    def discard(self) -> tuple[str | None, list[str]]:
        """You don't want this session (X held on the screen): delete its files, so it is neither
        kept nor uploaded. Returns its id (None if it was too short to be saved) and the
        destinations it had already been uploaded to (those copies stay there)."""
        c = self.coach
        if self._id is None or c.session != self._session:
            return None, []
        session, self._id, self._session, self._written = self._id, None, None, None
        record = read_session(str(self.directory), session) or {}
        sent = [d for d, mark in (record.get("uploads") or {}).items() if mark and not mark.get("failed")]
        delete(str(self.directory), session)
        return session, sent

    def _write(self, metrics: RowingMetrics, complete: bool, wall_now: float) -> Path | None:
        rows = stroke_rows(self.coach)
        distance = metrics.distance_m - distance_offset([r[1] for r in rows])
        record = build_record(self.coach, self._started_at, wall_now, complete, max(0, distance),
                              metrics.elapsed_s, session=self._id, ended_at=self._ended_at)
        previous = read_session(str(self.directory), self._id)
        if previous and previous.get("uploads"):
            record["uploads"] = previous["uploads"]  # what was already sent stays sent
        try:
            path = save(self.directory, record, rows)
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
                 distance_m: int, elapsed_s: int, source: str = "bridge", session: str | None = None,
                 ended_at: float | None = None) -> dict:
    """What a session file holds: when, the pulley's totals and the coach's session summary.

    source: "bridge" (rowed live) or "replay" (from a CSV export; never uploaded by itself).
    ended_at: wall time of the last stroke (default: started_at plus the time rowed).
    """
    summary = coach.session_summary()
    if ended_at is None:
        ended_at = started_at + summary.get("rowing_s", 0)
    return {
        "version": VERSION,
        "id": session or session_id(started_at),
        "source": source,
        "started_at": round(started_at),
        "ended_at": round(ended_at),
        "saved_at": round(saved_at),
        "complete": complete,
        "distance_m": distance_m,
        "elapsed_s": elapsed_s,
        **summary,
    }


def distance_offset(distances: list[int]) -> int:
    """What to subtract from the pulley's distance so the session starts at 0.

    The pulley's counter starts at 0 when it is connected; a session that starts later on the
    same connection (after a long pause) starts part-way. The first stroke's own distance is
    taken to be like the second's.
    """
    if not distances or distances[0] < 50:
        return 0
    first = distances[1] - distances[0] if len(distances) > 1 else 0
    return max(0, distances[0] - max(0, first))


# force_n: the stroke's force curve as the pulley sends it (24 samples, N, about every 5.8 cm of
# handle travel from the catch; see coach.py). Files saved before it was added lack the column.
STROKE_COLUMNS = ["t", "distance_m", "power_w", "spm", "heart_rate", "length_cm", "force_n"]


def stroke_rows(coach: IntervalCoach) -> list[list]:
    """The session's strokes, time in seconds from its first stroke (columns STROKE_COLUMNS)."""
    if not coach.stroke_log:
        return []
    t0 = coach.stroke_log[0][0]
    return [[round(t - t0, 2), d, round(p, 1), round(spm, 1), hr, length, list(force)]
            for t, d, p, spm, hr, length, force in coach.stroke_log]


def _write(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")  # the bridge and the command line may both write
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def save(directory: Path | str, record: dict, strokes: list[list] | None = None) -> Path:
    """Write one session file (and its strokes) in one step, world-readable; keep the newest KEEP."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o755)
    path = directory / f"{record['id']}.json"
    if strokes is not None:
        _write(directory / f"{record['id']}.strokes.json",
               {"version": VERSION, "id": record["id"], "started_at": record["started_at"],
                "columns": STROKE_COLUMNS, "rows": strokes})
    _write(path, record)
    files = sorted(p for p in directory.glob("*.json") if ID_PATTERN.match(p.stem))
    for old in files[:-KEEP]:
        old.unlink(missing_ok=True)
        (directory / f"{old.stem}.strokes.json").unlink(missing_ok=True)
    return path


def delete(directory: str, session: str) -> None:
    """Remove a saved session and its strokes."""
    if not ID_PATTERN.match(session):
        return
    for name in (f"{session}.json", f"{session}.strokes.json"):
        (Path(directory) / name).unlink(missing_ok=True)


def mark_finished(directory: str, session: str) -> None:
    """Note that the rower ended this session (uploads then go at once, not FINAL_AFTER_S later)."""
    record = read_session(directory, session)
    if record is not None and not record.get("finished"):
        record["finished"] = True
        save(directory, record)


def read_strokes(directory: str, session: str) -> list[dict] | None:
    """A saved session's strokes as dicts (STROKE_COLUMNS), or None if there are none."""
    if not ID_PATTERN.match(session):
        return None
    try:
        data = json.loads((Path(directory) / f"{session}.strokes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    cols = data.get("columns", STROKE_COLUMNS)
    return [dict(zip(cols, row)) for row in data.get("rows", [])] or None


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


def latest_fresh_easy(directory: str, now: float | None = None, max_age_days: float = 30) -> dict | None:
    """The fresh Easy stroke of the most recent saved session that has one, if it is no older than
    max_age_days (coach.FRESH_MAX_AGE_DAYS): what the next session's Easy rowing is judged against."""
    now = time.time() if now is None else now
    for path in sorted(Path(directory).glob("*.json"), reverse=True):
        if not ID_PATTERN.match(path.stem):
            continue
        record = read_session(directory, path.stem)
        if not record:
            continue
        if now - record.get("started_at", 0) > max_age_days * 86400:
            return None  # newest first: the rest are older still
        fresh = record.get("fresh_easy")
        if fresh and fresh.get("curve"):
            return {**fresh, "session": record.get("id", path.stem), "expect": learned_finish(directory, now)}
    return None


def learned_finish(directory: str, now: float | None = None, days: float = 60, last: int = 20) -> dict:
    """Your usual finish at each intensity, as a share of your Easy one: the median of your last
    `last` efforts at it (within `days`), once there are coach.EXPECT_MIN. Empty without enough."""
    from .coach import EXPECT_MIN, EXPECT_RANGE
    now = time.time() if now is None else now
    seen: dict[str, list[float]] = {}
    for path in sorted(Path(directory).glob("*.json"), reverse=True):
        if not ID_PATTERN.match(path.stem):
            continue
        record = read_session(directory, path.stem)
        if not record:
            continue
        if now - record.get("started_at", 0) > days * 86400:
            break
        for i in reversed(record.get("intervals") or []):
            k, r = i.get("intensity"), i.get("easy_finish_ratio")
            if k and r and len(seen.setdefault(k, [])) < last:
                seen[k].append(r)
    lo, hi = EXPECT_RANGE
    return {k: round(min(hi, max(lo, statistics.median(v))), 3) for k, v in seen.items() if len(v) >= EXPECT_MIN}


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
                   f"{_day(p['started_at'])}; the aim is within 5% of Easy.")
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
