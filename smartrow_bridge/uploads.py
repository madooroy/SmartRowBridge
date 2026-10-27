# SPDX-License-Identifier: GPL-3.0-only
"""Sends finished sessions to intervals.icu and Strava, in the background. Both optional.

The bridge calls Uploader.tick() once a second. Every minute it looks through the saved
sessions (sessions.py) for one that a set-up destination hasn't had yet, and sends it in
a background thread, one at a time, so the bridge is never held up. What was sent is noted
in the session's file ("uploads"), so nothing is sent twice; failures (offline, a service
down) are retried ten minutes later.

When a session is sent:
- an activity upload (intervals.icu "activity" mode, Strava) once the session is over for
  good: FINAL_AFTER_S without a stroke, past the point where rowing again starts a new
  session (coach.NEW_SESSION_S). An uploaded activity is never replaced;
- an intervals.icu note as soon as the session is complete, and again if it resumes (the
  note is updated, not duplicated).

Only sessions rowed with the bridge after a destination was set up are sent by themselves,
and only in the last SEND_WITHIN_DAYS days. Sessions added from CSV exports
(tools/coach_replay.py --save) are never sent by themselves; send one on purpose with
    python -m smartrow_bridge.uploads --send ID

    python -m smartrow_bridge.uploads --status          recent sessions and where each was sent
    python -m smartrow_bridge.uploads --fit ID OUT      write a session's FIT file (to look at)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import coach, intervals, sessions, strava

log = logging.getLogger(__name__)

CHECK_EVERY_S = 60.0
RETRY_AFTER_S = 600.0
FINAL_AFTER_S = coach.NEW_SESSION_S + 120.0  # rowing again by then is a new session
SEND_WITHIN_DAYS = 7


@dataclass
class Settings:
    session_dir: str = sessions.DEFAULT_DIR
    intervals_key_file: str = intervals.DEFAULT_KEY_FILE
    intervals_upload: str = "activity"  # activity, note or off
    strava_file: str = strava.DEFAULT_FILE
    strava_sport_type: str = "Rowing"

    @classmethod
    def from_env(cls) -> "Settings":
        env = lambda name, default: os.environ.get(name, "").strip() or default  # noqa: E731
        return cls(env("SRB_SESSION_DIR", sessions.DEFAULT_DIR),
                   env("SRB_INTERVALS_KEY_FILE", intervals.DEFAULT_KEY_FILE),
                   env("SRB_INTERVALS_UPLOAD", "activity").lower(),
                   env("SRB_STRAVA_FILE", strava.DEFAULT_FILE),
                   env("SRB_STRAVA_SPORT_TYPE", "Rowing"))


def _mark(record: dict, dest: str) -> dict | None:
    marks = record.get("uploads") or {}
    if dest == "intervals_note" and "intervals_note" not in marks and record.get("intervals_icu"):
        return record["intervals_icu"]  # notes sent before "uploads" existed
    return marks.get(dest)


def _over(record: dict, now: float) -> bool:
    return now - (record["started_at"] + record.get("rowing_s", 0)) >= FINAL_AFTER_S


class Destinations:
    """The set-up destinations and what each one wants from a session."""

    def __init__(self, settings: Settings) -> None:
        self.s = settings

    def configured(self) -> list[str]:
        out = []
        if self.s.intervals_upload in ("activity", "note") and intervals.read_key(self.s.intervals_key_file):
            out.append("intervals_activity" if self.s.intervals_upload == "activity" else "intervals_note")
        if strava.read_credentials(self.s.strava_file):
            out.append("strava")
        return out

    def since(self, dest: str) -> float:
        if dest == "strava":
            return strava.StravaAccount(self.s.strava_file).since
        try:
            return Path(self.s.intervals_key_file).stat().st_mtime
        except OSError:
            return float("inf")

    def wants(self, dest: str, record: dict, now: float, automatic: bool = True) -> bool:
        if not record.get("complete"):
            return False
        mark = _mark(record, dest)
        if automatic:
            if record.get("source") == "replay":
                return False
            if record["started_at"] < self.since(dest) - 60 or record["started_at"] < now - SEND_WITHIN_DAYS * 86400:
                return False
        if dest == "intervals_note":
            return not mark or mark.get("sent_for") != record.get("saved_at")
        return not mark and (_over(record, now) or not automatic)

    def send(self, dest: str, record: dict) -> dict:
        """Send one session to one destination (blocking). Returns what to note in the session's record."""
        if dest == "intervals_note":
            return intervals.send_note(record, intervals.read_key(self.s.intervals_key_file))
        strokes = sessions.read_strokes(self.s.session_dir, record["id"])
        if not strokes:
            raise RuntimeError("no strokes saved for this session, so there is no activity file to upload")
        from . import fitfile  # fit-tool: only needed once there is something to upload
        fit = fitfile.build(record, strokes)
        if dest == "intervals_activity":
            return intervals.upload_activity(record, fit, intervals.read_key(self.s.intervals_key_file))
        return strava.StravaAccount(self.s.strava_file).upload(
            fit, external_id=f"smartrowbridge-{record['id']}", name=intervals.activity_name(record),
            description=intervals.summary_text(record), sport_type=self.s.strava_sport_type)

    def describe_error(self, dest: str, exc: BaseException) -> str:
        return strava.describe_error(exc) if dest == "strava" else intervals.describe_error(exc)


LABELS = {"intervals_activity": "intervals.icu activity", "intervals_note": "intervals.icu note",
          "strava": "Strava"}


def record_sent(directory: str, record: dict, dest: str, result: dict) -> bool:
    """Note the result in the session's file, unless the session was rewritten meanwhile."""
    current = sessions.read_session(directory, record["id"])
    if current is None or current.get("saved_at") != record.get("saved_at"):
        return False
    uploads = dict(current.get("uploads") or {})
    uploads[dest] = {**result, "sent_at": round(time.time()), "sent_for": record.get("saved_at")}
    current["uploads"] = uploads
    sessions.save(directory, current)
    return True


class Uploader:
    """Called once a second by the bridge (from its event loop)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.dests = Destinations(settings)
        self._next_check = 0.0
        self._busy = False
        self._retry_at: dict[str, float] = {}
        self._last_error: dict[str, str] = {}

    def pending(self, now: float | None = None) -> list[tuple[str, dict]]:
        now = time.time() if now is None else now
        mono = time.monotonic()
        configured = [d for d in self.dests.configured() if self._retry_at.get(d, 0) <= mono]
        if not configured:
            return []
        out = []
        for entry in reversed(sessions.list_sessions(self.settings.session_dir)):  # oldest first
            if not entry["complete"]:
                continue
            record = sessions.read_session(self.settings.session_dir, entry["id"])
            if record is None:
                continue
            out += [(d, record) for d in configured if self.dests.wants(d, record, now)]
        return out

    def tick(self, now: float | None = None) -> None:
        mono = time.monotonic() if now is None else now
        if self._busy or mono < self._next_check:
            return
        self._next_check = mono + CHECK_EVERY_S
        work = self.pending()
        if work:
            self._busy = True
            asyncio.get_running_loop().create_task(self._send(*work[0]))

    async def _send(self, dest: str, record: dict) -> None:
        try:
            result = await asyncio.to_thread(self.dests.send, dest, record)
        except Exception as exc:  # noqa: BLE001 - offline, a service down, a revoked key: later
            message = self.dests.describe_error(dest, exc)
            if message != self._last_error.get(dest):
                log.warning("%s for session %s not sent: %s", LABELS[dest], record["id"], message)
            self._last_error[dest] = message
            self._retry_at[dest] = time.monotonic() + RETRY_AFTER_S
        else:
            self._last_error.pop(dest, None)
            record_sent(self.settings.session_dir, record, dest, result)
            what = "already there" if result.get("duplicate") else "sent"
            log.info("Upload: %s for session %s %s", LABELS[dest], record["id"], what)
            self._next_check = 0.0  # more waiting? next tick
        finally:
            self._busy = False


def _status(settings: Settings) -> int:
    dests = Destinations(settings)
    configured = dests.configured()
    print("Set up: " + (", ".join(LABELS[d] for d in configured) or "nothing (tools/setup_intervals.sh, "
                                                                     "tools/setup_strava.sh)"))
    for entry in sessions.list_sessions(settings.session_dir)[:15]:
        record = sessions.read_session(settings.session_dir, entry["id"]) or {}
        marks = []
        for d in ("intervals_activity", "intervals_note", "strava"):
            m = _mark(record, d)
            if m:
                marks.append(f"{LABELS[d]}: " + ("already there" if m.get("duplicate") else "sent"))
            elif d in configured:
                if dests.wants(d, record, time.time()):
                    marks.append(f"{LABELS[d]}: waiting")
                elif record.get("source") == "replay" or record.get("started_at", 0) < dests.since(d) - 60:
                    marks.append(f"{LABELS[d]}: not sent by itself (--send to send)")
        source = " (from a CSV)" if record.get("source") == "replay" else ""
        print(f"{entry['id']}{source}  {entry['distance_m']} m  " + ("; ".join(marks) or "-"))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="smartrow-uploads", description=__doc__.splitlines()[0])
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--status", action="store_true", help="recent sessions and where each was sent")
    group.add_argument("--send", metavar="ID", help="send a saved session now, to every set-up destination")
    group.add_argument("--fit", nargs=2, metavar=("ID", "OUT"), help="write a session's FIT file")
    p.add_argument("--to", choices=("intervals", "strava"), help="with --send: only this destination")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings.from_env()
    if args.status:
        return _status(settings)
    session = args.fit[0] if args.fit else args.send
    record = sessions.read_session(settings.session_dir, session)
    if record is None:
        print(f"No saved session {session} in {settings.session_dir}", file=sys.stderr)
        return 1
    if args.fit:
        strokes = sessions.read_strokes(settings.session_dir, session)
        if not strokes:
            print("No strokes saved for this session.", file=sys.stderr)
            return 1
        from . import fitfile
        Path(args.fit[1]).write_bytes(fitfile.build(record, strokes))
        print(f"Wrote {args.fit[1]}")
        return 0
    dests = Destinations(settings)
    targets = [d for d in dests.configured() if not args.to or d.startswith(args.to)]
    if not targets:
        print("Nothing set up to send to (tools/setup_intervals.sh, tools/setup_strava.sh).", file=sys.stderr)
        return 1
    status = 0
    for dest in targets:
        if _mark(record, dest) and dest != "intervals_note":
            print(f"{LABELS[dest]}: already sent, not again.")
            continue
        try:
            result = dests.send(dest, {**record, "complete": True})
        except Exception as exc:  # noqa: BLE001
            print(f"{LABELS[dest]}: {dests.describe_error(dest, exc)}", file=sys.stderr)
            status = 1
            continue
        record_sent(settings.session_dir, record, dest, result)
        print(f"{LABELS[dest]}: " + ("already there" if result.get("duplicate") else "sent"))
        record = sessions.read_session(settings.session_dir, session) or record
    return status


if __name__ == "__main__":
    sys.exit(main())
