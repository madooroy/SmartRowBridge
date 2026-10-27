# SPDX-License-Identifier: GPL-3.0-only
"""Optional: put each session's coach summary on your intervals.icu calendar, as a note.

Set up with tools/setup_intervals.sh, which saves your intervals.icu API key (Settings,
Developer settings, API key) in a file only root can read; with no key file, nothing
here runs. The bridge then adds a note to the day of each completed session: its
numbers, what the coach saw and each interval's finish. Sessions finished while the
Pi was offline are sent once it is back online (up to SEND_WITHIN_DAYS old).

Why a calendar note and not the activity itself: activities that reach intervals.icu
from Strava cannot be read or changed through its API (Strava's terms), whatever the
intervals.icu plan. A note is the athlete's own calendar entry, so it always works.

Each note has a fixed uid per session, sent with upsertOnUid, so sending a session again
(it resumed after a break, or a retry) updates its note instead of adding another.

    python -m smartrow_bridge.intervals --test          check the key: shows the athlete's name
    python -m smartrow_bridge.intervals --send ID       send one saved session now
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from . import sessions

log = logging.getLogger(__name__)

API = "https://intervals.icu/api/v1"
DEFAULT_KEY_FILE = "/etc/smartrow-bridge/intervals.key"
# intervals.icu sits behind Cloudflare, which can challenge unknown clients.
USER_AGENT = "Mozilla/5.0 (X11; Linux aarch64) SmartRowBridge/1.0"
TIMEOUT_S = 20
CHECK_EVERY_S = 60.0  # look for sessions to send this often
RETRY_AFTER_S = 600.0  # after a failure, wait this long
SEND_WITHIN_DAYS = 7


def read_key(path: str) -> str | None:
    try:
        key = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return key or None


def _request(method: str, path: str, key: str, body: dict | None = None, params: str = "") -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API}{path}{params}", data=data, method=method)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"API_KEY:{key}".encode()).decode())
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return f"intervals.icu refused the API key (HTTP {exc.code}); run tools/setup_intervals.sh again"
        return f"intervals.icu answered HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"can't reach intervals.icu ({exc.reason})"
    return f"{type(exc).__name__}: {exc}"


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
    short = sum(1 for i in intervals if i.get("rating") == "short")
    km = record.get("distance_m", 0) / 1000
    title = f"Row coach: {_clock(record.get('rowing_s', 0))} · {km:.2f} km"
    if intervals:
        title += f" · {len(intervals)} interval{'s' if len(intervals) > 1 else ''}"
        title += f", {short} went short" if short else ", all kept their shape"
    lines = [f"Rowed at {started.strftime('%H:%M')}: {record.get('avg_power_w', 0)} W average, "
             f"steady {record.get('steady_power_w', 0)} W, {record.get('strokes', 0)} strokes."]
    hr = record.get("heart_rate")
    if hr:
        lines[0] += f" Heart rate {hr['avg']} average, {hr['max']} max."
    lines += ["", "What the coach saw:"] + [f"- {f}" for f in record.get("findings", [])]
    if intervals:
        lines += ["", "Intervals (finish = force in the back half of the drive against steady):"]
        for i in intervals:
            row = (f"I{i['index']}  {_clock(i['duration_s'])}  {i['power_w']} W  {i['stroke_rate_spm']} spm  "
                   f"finish {_signed(i['finish_pct'])}%  length {_signed(round(i['length_delta_cm']))} cm")
            if i.get("hr_peak"):
                row += f"  HR peak {i['hr_peak']}"
            if i.get("legs_late"):
                row += "  leg drive late"
            lines.append(row)
    lines += ["", "From SmartRowBridge (interval coach)."]
    return {
        "category": "NOTE",
        "start_date_local": started.strftime("%Y-%m-%dT00:00:00"),
        "name": title,
        "description": "\n".join(lines),
        "uid": f"smartrowbridge-{record['id']}",
    }


def send(record: dict, key: str) -> int | None:
    """Add (or update) the session's note. Returns the intervals.icu event id."""
    event = _request("POST", "/athlete/0/events", key, note_for(record), params="?upsertOnUid=true")
    return event.get("id")


def _marked(record: dict) -> bool:
    mark = record.get("intervals_icu") or {}
    return mark.get("sent_for") == record.get("saved_at")  # sent, and not changed since


class IntervalsUploader:
    """Called once a second by the bridge; sends finished sessions in the background."""

    def __init__(self, session_dir: str, key_file: str) -> None:
        self.session_dir = session_dir
        self.key_file = key_file
        self._next_check = 0.0
        self._busy = False
        self._last_error: str | None = None

    def tick(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if self._busy or now < self._next_check:
            return
        self._next_check = now + CHECK_EVERY_S
        key = read_key(self.key_file)
        if key is None:
            return
        pending = self._pending()
        if pending:
            self._busy = True
            asyncio.get_running_loop().create_task(self._send(pending[0], key))

    def _pending(self) -> list[dict]:
        oldest = time.time() - SEND_WITHIN_DAYS * 86400
        out = []
        for entry in sessions.list_sessions(self.session_dir):
            if not entry["complete"] or (entry.get("started_at") or 0) < oldest:
                continue
            record = sessions.read_session(self.session_dir, entry["id"])
            if record is not None and not _marked(record):
                out.append(record)
        return sorted(out, key=lambda r: r["started_at"])  # oldest first

    async def _send(self, record: dict, key: str) -> None:
        try:
            event_id = await asyncio.to_thread(send, record, key)
        except Exception as exc:  # noqa: BLE001 - offline, key revoked...: try again later
            message = describe_error(exc)
            if message != self._last_error:
                log.warning("intervals.icu note for %s not sent: %s", record["id"], message)
            self._last_error = message
            self._next_check = time.monotonic() + RETRY_AFTER_S
        else:
            self._last_error = None
            self._mark(record, event_id)
            log.info("intervals.icu note added for session %s", record["id"])
            self._next_check = 0.0  # more waiting? next tick
        finally:
            self._busy = False

    def _mark(self, record: dict, event_id: int | None) -> None:
        current = sessions.read_session(self.session_dir, record["id"])
        if current is None or current.get("saved_at") != record.get("saved_at"):
            return  # rewritten meanwhile (rowing resumed): it will be sent again
        current["intervals_icu"] = {"event_id": event_id, "sent_at": round(time.time()),
                                    "sent_for": record.get("saved_at")}
        sessions.save(self.session_dir, current)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="smartrow-intervals", description=__doc__.splitlines()[0])
    p.add_argument("--key-file", default=os.environ.get("SRB_INTERVALS_KEY_FILE", "").strip() or DEFAULT_KEY_FILE)
    p.add_argument("--session-dir", default=os.environ.get("SRB_SESSION_DIR", "").strip() or sessions.DEFAULT_DIR)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--test", action="store_true", help="check the API key")
    group.add_argument("--send", metavar="ID", help="send one saved session now (e.g. 2026-10-27T0702)")
    group.add_argument("--preview", metavar="ID", help="print the note a saved session would get")
    args = p.parse_args(argv)
    if args.preview:
        record = sessions.read_session(args.session_dir, args.preview)
        if record is None:
            print(f"No saved session {args.preview} in {args.session_dir}", file=sys.stderr)
            return 1
        note = note_for(record)
        print(f"{note['start_date_local'][:10]}  {note['name']}\n\n{note['description']}")
        return 0
    key = read_key(args.key_file)
    if key is None:
        print(f"No API key in {args.key_file}: run tools/setup_intervals.sh", file=sys.stderr)
        return 1
    try:
        if args.test:
            print(f"intervals.icu: connected as {athlete_name(key)}")
            return 0
        record = sessions.read_session(args.session_dir, args.send)
        if record is None:
            print(f"No saved session {args.send} in {args.session_dir}", file=sys.stderr)
            return 1
        print(f"Note added to your intervals.icu calendar (event {send(record, key)})")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(describe_error(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
