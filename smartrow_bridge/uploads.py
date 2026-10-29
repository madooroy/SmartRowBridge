# SPDX-License-Identifier: GPL-3.0-only
"""Sends finished sessions to intervals.icu and Strava, in the background. Both optional.

The bridge calls Uploader.tick() once a second. Every minute it looks through the saved
sessions (sessions.py) for one that a set-up destination hasn't had yet, and sends it, one at a time;
the looking and the sending both happen in a background thread, so the bridge is never held
up. What was sent is noted in the session's file ("uploads"), so nothing is sent twice.
Failures: when a destination can't be reached (offline, a service down, a refused key), it
is tried again ten minutes later; when one session fails, the others still go, and that one
is tried again later. A session a service turns down for good (a file it can't read) is
noted as failed and not tried again by itself (--send tries it again).

A session the Pi was switched off in the middle of (no clean stop, so never saved as
complete) counts as finished once its file has gone untouched for ABANDONED_AFTER_S.

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
    python -m smartrow_bridge.uploads --fit ID OUT      write a session's FIT file (intervals.icu's)
    python -m smartrow_bridge.uploads --tcx ID OUT      write a session's TCX file (Strava's)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
import urllib.error
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import coach, intervals, sessions, strava, webapi

log = logging.getLogger(__name__)

CHECK_EVERY_S = 60.0
RETRY_AFTER_S = 600.0
FINAL_AFTER_S = coach.NEW_SESSION_S + 120.0  # rowing again by then is a new session
SEND_WITHIN_DAYS = 7
ABANDONED_AFTER_S = sessions.REFRESH_S + FINAL_AFTER_S  # while rowing, the file is rewritten every REFRESH_S
GIVE_UP_AFTER = 5  # one session failing this often (for reasons other than the connection): noted as failed
ACTIVITIES = ("intervals_activity", "strava")


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


def _ended_at(record: dict) -> float:
    return record.get("ended_at") or record["started_at"] + record.get("rowing_s", 0)


def _over(record: dict, now: float) -> bool:
    """Over for good: you said so (Y on the screen), or no stroke for FINAL_AFTER_S."""
    return bool(record.get("finished")) or now - _ended_at(record) >= FINAL_AFTER_S


def finished(record: dict, now: float) -> bool:
    """Saved as complete, or left incomplete by a Pi switched off mid-session."""
    return bool(record.get("complete")) or now - record.get("saved_at", now) >= ABANDONED_AFTER_S


def connection_trouble(exc: BaseException) -> bool:
    """Trouble with the destination as a whole (not this session): wait before trying it again."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (401, 403, 408, 429) or exc.code >= 500
    return isinstance(exc, (urllib.error.URLError, OSError))


def turned_down(exc: BaseException) -> bool:
    """The service won't take this session, however often it is sent."""
    if isinstance(exc, webapi.Rejected):
        return True
    return isinstance(exc, urllib.error.HTTPError) and exc.code in (400, 404, 409, 413, 415, 422)


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

    def eligible(self, dest: str, record: dict, now: float, since: float | None = None) -> bool:
        """Sent by itself at all: rowed live, after the destination was set up, in the last week."""
        since = self.since(dest) if since is None else since
        return (record.get("source") != "replay" and record["started_at"] >= since - 60
                and record["started_at"] >= now - SEND_WITHIN_DAYS * 86400)

    def wants(self, dest: str, record: dict, now: float, automatic: bool = True,
              since: float | None = None) -> bool:
        if not finished(record, now):
            return False
        mark = _mark(record, dest)
        if automatic and not self.eligible(dest, record, now, since):
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
            raise webapi.Rejected("no strokes saved for this session, so there is no activity file to upload")
        if dest == "intervals_activity":
            from . import fitfile  # fit-tool: only needed once there is something to upload
            fit = fitfile.build(record, strokes)
            return intervals.upload_activity(record, fit, intervals.read_key(self.s.intervals_key_file), strokes)
        from . import tcxfile  # Strava gets TCX, like the SmartRow app's uploads (see strava.py)
        return strava.StravaAccount(self.s.strava_file).upload(
            tcxfile.build(record, strokes), external_id=f"smartrowbridge-{record['id']}",
            name=intervals.activity_name(record),
            description=intervals.summary_text(record), sport_type=self.s.strava_sport_type)

    def describe_error(self, dest: str, exc: BaseException) -> str:
        return strava.describe_error(exc) if dest == "strava" else intervals.describe_error(exc)


LABELS = {"intervals_activity": "intervals.icu activity", "intervals_note": "intervals.icu note",
          "strava": "Strava"}


def record_sent(directory: str, record: dict, dest: str, result: dict) -> bool:
    """Note the result in the session's file (also if it was rewritten meanwhile: sent is sent).

    sent_for is the version that was sent, so a note is sent again for a newer one."""
    current = sessions.read_session(directory, record["id"])
    if current is None:
        return False
    uploads = dict(current.get("uploads") or {})
    uploads[dest] = {**result, "sent_at": round(time.time()), "sent_for": record.get("saved_at")}
    current["uploads"] = uploads
    sessions.save(directory, current)
    return True


def recent_sessions(directory: str, now: float, days: float = SEND_WITHIN_DAYS + 1) -> list[dict]:
    """Oldest first: the sessions saved in the last few days (only their files are read)."""
    oldest = datetime.fromtimestamp(now - days * 86400).strftime("%Y-%m-%dT%H%M")
    out = []
    for path in sorted(Path(directory).glob("*.json")):
        if sessions.ID_PATTERN.match(path.stem) and path.stem >= oldest:
            record = sessions.read_session(directory, path.stem)
            if record is not None:
                out.append(record)
    return out


def has_strokes(directory: str, session: str) -> bool:
    return (Path(directory) / f"{session}.strokes.json").exists()


class Uploader:
    """Called once a second by the bridge (from its event loop)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.dests = Destinations(settings)
        self._next_check = 0.0
        self._busy = False
        self._retry_at: dict = {}  # destination, or (destination, session): monotonic time
        self._failures: dict[tuple[str, str], int] = {}
        self._last_error: dict[str, str] = {}
        self._sending: str | None = None  # the destination being sent to now
        self._ack: str | None = None  # a "Done" request: echoed once the next look has been taken
        self._acked: str | None = None
        self._snapshot: dict = {"configured": [], "sending": None, "waiting": 0, "retrying": 0, "error": None,
                                "latest": None, "ack": None}

    def check_now(self, token: str | None = None) -> None:
        """A session was just finished by hand: look (and send) at the next tick, not in a minute."""
        self._next_check = 0.0
        self._ack = token

    def latest_waiting(self) -> str | None:
        """The newest session whose uploads wait for FINAL_AFTER_S (to finish it by hand), if any."""
        latest = self._snapshot.get("latest") or {}
        return latest.get("id") if "later" in (latest.get("dests") or {}).values() else None

    def snapshot(self) -> dict:
        """For the status file (the screen): what is being sent, what is still to go, and how the
        newest session stands with each destination."""
        return {**self._snapshot, "sending": LABELS.get(self._sending) if self._sending else None,
                "ack": self._acked}

    def pending(self, now: float | None = None) -> list[tuple[str, dict]]:
        """What to send now, oldest first. Reads files: called in a background thread. Also takes
        stock for snapshot()."""
        now = time.time() if now is None else now
        mono = time.monotonic()
        ack = self._ack
        all_configured = self.dests.configured()
        configured = [d for d in all_configured if self._retry_at.get(d, 0) <= mono]
        since = {d: self.dests.since(d) for d in all_configured}
        directory = self.settings.session_dir
        out, waiting, retrying, latest = [], 0, 0, None
        records = recent_sessions(directory, now) if all_configured else []
        for record in records:
            states = {}
            for d in all_configured:
                mark = _mark(record, d)
                if mark and mark.get("failed"):
                    states[LABELS[d]] = "failed"
                    continue
                if mark and d != "intervals_note":
                    states[LABELS[d]] = "sent"
                    continue
                if not self.dests.eligible(d, record, now, since[d]):
                    continue
                if d in ACTIVITIES and not has_strokes(directory, record["id"]):
                    continue  # saved before activity files existed
                if not finished(record, now):
                    states[LABELS[d]] = "rowing"
                    continue
                if self._retry_at.get(d, 0) > mono or self._retry_at.get((d, record["id"]), 0) > mono:
                    states[LABELS[d]] = "retrying"
                    retrying += 1
                    continue
                if self.dests.wants(d, record, now, since=since[d]):
                    if d in configured:
                        out.append((d, record))
                    states[LABELS[d]] = "waiting"
                    waiting += 1
                elif d == "intervals_note" and mark:
                    states[LABELS[d]] = "sent"
                elif not _over(record, now):
                    states[LABELS[d]] = "later"  # goes FINAL_AFTER_S after the last stroke
                    waiting += 1
            latest = {"id": record["id"], "dests": states}
        errors = [self._last_error[d] for d in all_configured if d in self._last_error]
        self._snapshot = {"configured": [LABELS[d] for d in all_configured], "waiting": waiting,
                          "retrying": retrying, "error": errors[0] if errors else None, "latest": latest}
        self._acked = ack
        return out

    def tick(self, now: float | None = None) -> None:
        mono = time.monotonic() if now is None else now
        if self._busy or mono < self._next_check:
            return
        self._next_check = mono + CHECK_EVERY_S
        self._busy = True
        asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        try:
            work = await asyncio.to_thread(self.pending)
            if work:
                await self._send(*work[0])
        except Exception:  # noqa: BLE001 - never stops the bridge
            log.exception("Uploads failed")
        finally:
            self._busy = False

    async def _send(self, dest: str, record: dict) -> None:
        key = (dest, record["id"])
        self._sending = dest
        try:
            result = await asyncio.to_thread(self.dests.send, dest, record)
        except Exception as exc:  # noqa: BLE001 - offline, a service down, a revoked key, a bad file
            message = self.dests.describe_error(dest, exc)
            if message != self._last_error.get(dest):
                log.warning("%s for session %s not sent: %s", LABELS[dest], record["id"], message)
            self._last_error[dest] = message
            later = time.monotonic() + RETRY_AFTER_S
            if connection_trouble(exc):
                self._retry_at[dest] = later  # the whole destination
                self._next_check = 0.0  # take stock again, so the screen shows it
                return
            self._failures[key] = self._failures.get(key, 0) + 1
            self._retry_at[key] = later  # this session; the others still go
            if turned_down(exc) or self._failures[key] >= GIVE_UP_AFTER:
                await asyncio.to_thread(record_sent, self.settings.session_dir, record, dest, {"failed": message})
            self._next_check = 0.0
        else:
            self._last_error.pop(dest, None)
            self._failures.pop(key, None)
            noted = await asyncio.to_thread(record_sent, self.settings.session_dir, record, dest, result)
            what = "already there" if result.get("duplicate") else "sent"
            log.info("Upload: %s for session %s %s%s", LABELS[dest], record["id"], what,
                     "" if noted else " (but its file is gone, so this isn't noted)")
            self._next_check = 0.0  # more waiting? next tick
        finally:
            self._sending = None


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
            if m and m.get("failed"):
                marks.append(f"{LABELS[d]}: failed ({m['failed']}; --send to try again)")
            elif m:
                marks.append(f"{LABELS[d]}: " + ("already there" if m.get("duplicate") else "sent"))
            elif d in configured:
                if dests.wants(d, record, time.time()):
                    marks.append(f"{LABELS[d]}: waiting")
                elif d in ACTIVITIES and not has_strokes(settings.session_dir, entry["id"]):
                    marks.append(f"{LABELS[d]}: no activity file (saved before uploads existed)")
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
    group.add_argument("--fit", nargs=2, metavar=("ID", "OUT"), help="write a session's FIT file (intervals.icu's)")
    group.add_argument("--tcx", nargs=2, metavar=("ID", "OUT"), help="write a session's TCX file (Strava's)")
    p.add_argument("--to", choices=("intervals", "strava"), help="with --send: only this destination")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = Settings.from_env()
    if args.status:
        return _status(settings)
    out_file = args.fit or args.tcx
    session = out_file[0] if out_file else args.send
    record = sessions.read_session(settings.session_dir, session)
    if record is None:
        print(f"No saved session {session} in {settings.session_dir}", file=sys.stderr)
        return 1
    if out_file:
        strokes = sessions.read_strokes(settings.session_dir, session)
        if not strokes:
            print("No strokes saved for this session.", file=sys.stderr)
            return 1
        if args.fit:
            from . import fitfile as maker
        else:
            from . import tcxfile as maker
        Path(out_file[1]).write_bytes(maker.build(record, strokes))
        print(f"Wrote {out_file[1]}")
        return 0
    dests = Destinations(settings)
    targets = [d for d in dests.configured() if not args.to or d.startswith(args.to)]
    if not targets:
        print("Nothing set up to send to (tools/setup_intervals.sh, tools/setup_strava.sh).", file=sys.stderr)
        return 1
    status = 0
    for dest in targets:
        mark = _mark(record, dest)
        if mark and not mark.get("failed") and dest != "intervals_note":
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
