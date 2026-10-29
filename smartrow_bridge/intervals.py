# SPDX-License-Identifier: GPL-3.0-only
"""Optional: send each session to intervals.icu, as an activity of its own or as a calendar note.

Set up with tools/setup_intervals.sh, which saves your intervals.icu API key (Settings,
Developer settings, API key) in a file only root can read; with no key file, nothing
here runs. The sending itself is done by uploads.py, once a session is over.

SRB_INTERVALS_UPLOAD chooses what is sent:
- "activity" (default): the session's FIT file (fitfile.py), uploaded as an activity, with
  the coach's summary as its description. Like a Garmin or Hammerhead upload, it takes
  the place of the copy that arrives from Strava (intervals.icu treats activities that
  start within a minute of each other, with distance and time within 1 %, as one).
- "note": a note on that day of the calendar instead, with the same summary. For when the
  session reaches intervals.icu another way: activities that come from Strava can't be
  changed through the API (Strava's terms), so the summary can't go on those.
- "off".

    python -m smartrow_bridge.intervals --test          check the key: shows the athlete's name
    python -m smartrow_bridge.intervals --preview ID    print the summary text a session would get
    python -m smartrow_bridge.intervals --mark-efforts ID ACTIVITY   mark a session's efforts on an
                                                        activity already there (e.g. i123456789)

After an upload, the activity's intervals are set to the coach's efforts (Moderate and harder)
through the API, so intervals.icu shows the work intervals only, not the Easy and Recovery
rowing between them (nor its own interval detection).
"""

from __future__ import annotations

import argparse
import base64
import bisect
import json
import logging
import os
import sys
import time
import urllib.error
from datetime import datetime
from pathlib import Path

from . import sessions, webapi
from .coach import SHORTER_CM, bands_text

log = logging.getLogger(__name__)

API = "https://intervals.icu/api/v1"
DEFAULT_KEY_FILE = "/etc/smartrow-bridge/intervals.key"
MODES = ("activity", "note", "off")


def read_key(path: str) -> str | None:
    try:
        key = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return key or None


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"API_KEY:{key}".encode()).decode()}


def _request(method: str, path: str, key: str, body: dict | None = None, params: str = "") -> dict:
    return webapi.request(method, f"{API}{path}{params}", _auth(key), json_body=body)


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, webapi.Rejected):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return f"intervals.icu refused the API key (HTTP {exc.code}); run tools/setup_intervals.sh again"
        detail = webapi.error_body(exc)
        return f"intervals.icu answered HTTP {exc.code}" + (f": {detail}" if detail else "")
    if isinstance(exc, urllib.error.URLError):
        return f"can't reach intervals.icu ({exc.reason})"
    return f"{type(exc).__name__}: {exc}"


FTP_SPORTS = ("Rowing", "VirtualRow")


def rowing_ftp(key: str) -> float | None:
    """Your rowing FTP from intervals.icu's sport settings (the indoor one when set), or None."""
    me = _request("GET", "/athlete/0", key)
    for sport in me.get("sportSettings") or []:
        if any(t in FTP_SPORTS for t in sport.get("types") or []):
            ftp = sport.get("indoor_ftp") or sport.get("ftp")
            return float(ftp) if ftp else None
    return None


def athlete_name(key: str) -> str:
    me = _request("GET", "/athlete/0", key)
    return me.get("name") or me.get("id") or "?"


def _clock(seconds: float) -> str:
    h, rest = divmod(round(seconds), 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60}:{rest % 60:02d}"


def _signed(value: int) -> str:
    return f"{value:+d}".replace("-", "−")


def effort_line(i: dict) -> str:
    """'I3 Moderate · 0:56 at 132 W · finish −7%, strokes 4 cm shorter · HR 126'."""
    notes = []
    if i.get("rating") in ("short", "ok"):
        notes.append(f"finish {_signed(i['finish_pct'])}%")
    if i.get("length_delta_cm", 0) <= SHORTER_CM:
        notes.append(f"strokes {abs(round(i['length_delta_cm']))} cm shorter")
    if i.get("fade_pct", 0) <= -10:
        notes.append(f"faded {abs(i['fade_pct'])}% over the effort")
    if i.get("legs_late"):
        notes.append("late push")
    if i.get("peak_wanders"):
        notes.append("peak wandered")
    parts = [f"I{i['index']}" + (f" {i['intensity']}" if i.get("intensity") else ""),
             f"{_clock(i['duration_s'])} at {i['power_w']} W", ", ".join(notes) or "good"]
    if i.get("hr_peak"):
        parts.append(f"HR {i['hr_peak']}")
    return " \u00b7 ".join(parts)


def note_for(record: dict) -> dict:
    """The calendar note for one saved session (sessions.build_record)."""
    started = datetime.fromtimestamp(record["started_at"])
    intervals = record.get("intervals") or []
    faded = sum(1 for i in intervals if i.get("rating") == "short")
    km = record.get("distance_m", 0) / 1000
    title = f"Row coach: {_clock(record.get('rowing_s', 0))} · {km:.2f} km"
    if intervals:
        title += f" · {len(intervals)} effort{'s' if len(intervals) > 1 else ''}"
        title += f", {faded} faded" if faded else ", all kept their shape"
    lines = ["Rowed using SmartRow", "",
             f"Rowed at {started.strftime('%H:%M')}: {record.get('avg_power_w', 0)} W average, "
             f"Easy rowing {record.get('steady_power_w', 0)} W, {record.get('strokes', 0)} strokes."]
    hr = record.get("heart_rate")
    if hr:
        lines[-1] += f" Heart rate {hr['avg']} average, {hr['max']} max."
    bands = bands_text(record.get("ftp"), record.get("steady_power_w"))
    if bands and intervals:
        source = (f"set from Challenging at {round(record['ftp'])} W" if record.get("ftp")
                  else f"set from Easy rowing at {record.get('steady_power_w')} W")
        lines += ["", f"Intensities, {source}: {bands}."]
    lines += ["", "What the coach saw:"] + [f"- {f}" for f in record.get("findings", [])]
    if intervals:
        lines += ["", "Efforts (finish against your usual stroke at that intensity):"] + [effort_line(i) for i in intervals]
    lines += ["", "Coached by SmartRowBridge."]
    return {
        "category": "NOTE",
        "start_date_local": started.strftime("%Y-%m-%dT00:00:00"),
        "name": title,
        "description": "\n".join(lines),
        "uid": f"smartrowbridge-{record['id']}",
    }


def activity_name(record: dict) -> str:
    """"Morning Row" and so on, as Strava and Garmin name activities."""
    hour = datetime.fromtimestamp(record["started_at"]).hour
    part = ("Night" if hour < 5 else "Morning" if hour < 12 else "Afternoon" if hour < 17
            else "Evening" if hour < 21 else "Night")
    return f"{part} Row"


def _count(n: int) -> str:
    return {1: "one", 2: "both", 3: "all three"}.get(n, "all of them")


def coach_line(record: dict) -> str:
    """One line on what the coach saw: 'Coach: 3 Moderate efforts (145-156 W); the finish eased in all three.'"""
    efforts = record.get("intervals") or []
    if not efforts:
        return "Coach: Easy rowing, no efforts."
    kinds = []
    for k in ("Moderate", "Challenging", "Max"):
        n = sum(1 for i in efforts if (i.get("intensity") or "Moderate") == k)
        if n:
            kinds.append(f"{n} {k}")
    watts = [round(i["power_w"]) for i in efforts]
    span = f"{min(watts)} W" if min(watts) == max(watts) else f"{min(watts)}\u2013{max(watts)} W"
    listed = kinds[0] if len(kinds) == 1 else ", ".join(kinds[:-1]) + " and " + kinds[-1]
    head = f"Coach: {listed} effort{'s' if len(efforts) > 1 else ''} ({span})"
    eased = [i for i in efforts if i.get("rating") in ("ok", "short")]
    shorter = [i for i in efforts if i.get("length_delta_cm", 0) <= SHORTER_CM]
    notes = []
    for label, these in (("the finish eased", eased), ("strokes were shorter", shorter)):
        if these:
            where = (_count(len(these)) if len(these) == len(efforts) else
                     ", ".join(f"I{i['index']}" for i in these))
            notes.append(f"{label} in {where}")
    kept = "it kept its shape" if len(efforts) == 1 else "all kept their shape"
    return head + ("; " + "; ".join(notes) if notes else f"; {kept}") + "."


def summary_text(record: dict) -> str:
    """The intervals.icu activity's description: short, as it shows on the calendar ("Rowed using
    SmartRow" and one line from the coach)."""
    return f"Rowed using SmartRow\n{coach_line(record)}"


def full_text(record: dict) -> str:
    """The Strava activity's description: the coach's full report."""
    return note_for(record)["description"]


def send_note(record: dict, key: str) -> dict:
    """Add (or update: same uid) the session's calendar note."""
    event = _request("POST", "/athlete/0/events", key, note_for(record), params="?upsertOnUid=true")
    return {"event_id": event.get("id")}


WORK_INTENSITIES = ("Moderate", "Challenging", "Max")
STREAM_TRIES, STREAM_WAIT_S = 4, 5.0  # the time stream can take a moment after the upload


def work_intervals(record: dict, strokes: list[dict], times: list[float]) -> list[dict]:
    """The coach's efforts (Moderate and harder) as intervals.icu intervals.

    `times` is the activity's time stream; its first point is the first stroke, whatever
    time base intervals.icu counts from. Easy and Recovery stretches are left out.
    """
    if not times or not strokes:
        return []
    offset = times[0] - strokes[0]["t"]
    out = []
    for i in record.get("intervals") or []:
        if "started_at_s" not in i or i.get("intensity", "Moderate") not in WORK_INTENSITIES:
            continue
        start = bisect.bisect_left(times, i["started_at_s"] + offset)
        end = min(bisect.bisect_left(times, i["started_at_s"] + i["duration_s"] + offset), len(times) - 1)
        if end > start:
            out.append({"start_index": start, "end_index": end, "type": "WORK",
                        "label": i.get("intensity") or "Effort"})
    return out


def _time_stream(activity_id: str, key: str) -> list[float]:
    for attempt in range(STREAM_TRIES):
        answer = _request("GET", f"/activity/{activity_id}/streams.json", key, params="?types=time")
        streams = answer if isinstance(answer, list) else answer.get("streams") or []
        for st in streams:
            if st.get("type") == "time" and st.get("data"):
                return [float(t) for t in st["data"]]
        if attempt + 1 < STREAM_TRIES:
            time.sleep(STREAM_WAIT_S)
    return []


def mark_efforts(activity_id: str, record: dict, strokes: list[dict], key: str) -> int:
    """Replace intervals.icu's own intervals with the coach's efforts. Returns how many were set."""
    marks = work_intervals(record, strokes, _time_stream(activity_id, key))
    if not marks:
        return 0
    webapi.request("PUT", f"{API}/activity/{activity_id}/intervals?all=true", _auth(key), json_body=marks)
    return len(marks)


def upload_activity(record: dict, fit: bytes, key: str, strokes: list[dict] | None = None) -> dict:
    """Upload the session's FIT file as an activity, then give it a name and the coach's summary,
    and mark the efforts as its intervals (only those: not the Easy and Recovery rowing)."""
    body, content_type = webapi.multipart({}, "file", f"smartrow-{record['id']}.fit", fit)
    answer = webapi.request("POST", f"{API}/athlete/0/activities", _auth(key), body=body, content_type=content_type)
    activity_id = answer.get("id") or (answer.get("activities") or [{}])[0].get("id")
    if activity_id:
        try:
            _request("PUT", f"/activity/{activity_id}", key,
                     {"name": activity_name(record), "description": summary_text(record)})
        except Exception as exc:  # noqa: BLE001 - the activity is there; only its text is missing
            log.warning("intervals.icu activity %s uploaded, but its description wasn't set: %s",
                        activity_id, describe_error(exc))
        if strokes and record.get("intervals"):
            try:
                n = mark_efforts(activity_id, record, strokes, key)
                log.info("intervals.icu activity %s: %d efforts marked as intervals", activity_id, n)
            except Exception as exc:  # noqa: BLE001 - intervals.icu keeps the intervals it found itself
                log.warning("intervals.icu activity %s uploaded, but its intervals weren't set: %s",
                            activity_id, describe_error(exc))
    return {"activity_id": activity_id}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="smartrow-intervals", description=__doc__.splitlines()[0])
    p.add_argument("--key-file", default=os.environ.get("SRB_INTERVALS_KEY_FILE", "").strip() or DEFAULT_KEY_FILE)
    p.add_argument("--session-dir", default=os.environ.get("SRB_SESSION_DIR", "").strip() or sessions.DEFAULT_DIR)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--test", action="store_true", help="check the API key")
    group.add_argument("--preview", metavar="ID", help="print the note a saved session would get")
    group.add_argument("--mark-efforts", nargs=2, metavar=("SESSION", "ACTIVITY"),
                       help="set a saved session's efforts as the intervals of an intervals.icu activity "
                            "(its id, e.g. i123456789, from the activity's address)")
    args = p.parse_args(argv)
    if args.preview:
        record = sessions.read_session(args.session_dir, args.preview)
        if record is None:
            print(f"No saved session {args.preview} in {args.session_dir}", file=sys.stderr)
            return 1
        print(f"{activity_name(record)}  ({record['id']})\n\n{summary_text(record)}")
        return 0
    key = read_key(args.key_file)
    if key is None:
        print(f"No API key in {args.key_file}: run tools/setup_intervals.sh", file=sys.stderr)
        return 1
    if args.mark_efforts:
        session, activity_id = args.mark_efforts
        record = sessions.read_session(args.session_dir, session)
        strokes = sessions.read_strokes(args.session_dir, session)
        if record is None or not strokes:
            print(f"No saved session {session} with strokes in {args.session_dir}", file=sys.stderr)
            return 1
        try:
            n = mark_efforts(activity_id, record, strokes, key)
        except Exception as exc:  # noqa: BLE001
            print(describe_error(exc), file=sys.stderr)
            return 1
        print(f"{n} efforts set as the intervals of {activity_id}" if n else
              "No efforts to mark (or intervals.icu has no time stream for that activity yet)")
        return 0 if n else 1
    try:
        print(f"intervals.icu: connected as {athlete_name(key)}")
        ftp = rowing_ftp(key)
        print(f"Rowing FTP: {ftp:.0f} W (the coach uses it unless SRB_COACH_FTP is set)" if ftp else
              "Rowing FTP: none set on intervals.icu (Settings, Rowing); the coach works without it")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(describe_error(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
