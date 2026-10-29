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
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import urllib.error
from datetime import datetime
from pathlib import Path

from . import sessions, webapi

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
             f"Easy {record.get('steady_power_w', 0)} W, {record.get('strokes', 0)} strokes."]
    if record.get("ftp"):
        lines[-1] += f" FTP {record['ftp']} W."
    hr = record.get("heart_rate")
    if hr:
        lines[-1] += f" Heart rate {hr['avg']} average, {hr['max']} max."
    lines += ["", "What the coach saw:"] + [f"- {f}" for f in record.get("findings", [])]
    if intervals:
        lines += ["", "Efforts (finish = force in the back half of the drive against Easy; peak = where in "
                      "the drive the force peaks, \u00b1 stroke to stroke):"]
        for i in intervals:
            kind = f"{i['intensity']}  " if i.get("intensity") else ""
            row = (f"I{i['index']}  {kind}{_clock(i['duration_s'])}  {i['power_w']} W  {i['stroke_rate_spm']} spm  "
                   f"finish {_signed(i['finish_pct'])}%  length {_signed(round(i['length_delta_cm']))} cm")
            if "peak_at_pct" in i:
                row += f"  peak {i['peak_at_pct']}% \u00b1{round(i['peak_spread_pct'])}"
            if i.get("ftp_pct"):
                row += f"  {i['ftp_pct']}% FTP"
            if i.get("hr_peak"):
                row += f"  HR peak {i['hr_peak']}"
            if i.get("legs_late"):
                row += "  late push"
            lines.append(row)
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


def summary_text(record: dict) -> str:
    """The coach's summary as plain text: the description of an uploaded activity."""
    return note_for(record)["description"]


def send_note(record: dict, key: str) -> dict:
    """Add (or update: same uid) the session's calendar note."""
    event = _request("POST", "/athlete/0/events", key, note_for(record), params="?upsertOnUid=true")
    return {"event_id": event.get("id")}


def upload_activity(record: dict, fit: bytes, key: str) -> dict:
    """Upload the session's FIT file as an activity, then give it a name and the coach's summary."""
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
    return {"activity_id": activity_id}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="smartrow-intervals", description=__doc__.splitlines()[0])
    p.add_argument("--key-file", default=os.environ.get("SRB_INTERVALS_KEY_FILE", "").strip() or DEFAULT_KEY_FILE)
    p.add_argument("--session-dir", default=os.environ.get("SRB_SESSION_DIR", "").strip() or sessions.DEFAULT_DIR)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--test", action="store_true", help="check the API key")
    group.add_argument("--preview", metavar="ID", help="print the note a saved session would get")
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
