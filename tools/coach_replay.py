#!/usr/bin/env python3
"""Replay SmartRow CSV exports through the interval coach, to see what it would have said.

Export a session from the SmartRow app (the CSV with one row per stroke and force
columns #0-#24) and run this on any computer with Python 3.10+; nothing else is
needed. It prints the intervals the coach finds, each interval's summary line (as in
the session record) and how much of each interval each live cue was showing.

Use it to check the coach against your own rowing before relying on it, and to try
settings: the options are the same as the bridge's SRB_COACH_* settings.

With --save DIR it also writes each session's summary file, as the bridge does on the
Pi, so the web page can show it: on the Pi, --save /var/lib/smartrow-bridge/sessions
(with sudo) adds a session rowed before the coach was turned on.

Usage:
    python3 tools/coach_replay.py SESSION.csv [MORE.csv ...]
    python3 tools/coach_replay.py --start-pct 15 --sensitivity high SESSION.csv
    sudo python3 tools/coach_replay.py --save /var/lib/smartrow-bridge/sessions SESSION.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartrow_bridge import sessions  # noqa: E402
from smartrow_bridge.coach import CoachSettings, IntervalCoach  # noqa: E402
from smartrow_bridge.protocol import FORCE_SAMPLES, StrokeCurve  # noqa: E402

CUE_NAMES = {"good": "good", "finish": "FINISH", "legs": "LEGS", "length": "LENGTH", None: "first strokes"}


def _number(text: str | None) -> int:
    """A whole number from a CSV cell; blank or missing (no heart rate strap) is 0."""
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return 0


def strokes_from_csv(path: Path):
    """StrokeCurve per row of a SmartRow app export. Force column #0 is always 0 and skipped."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            yield StrokeCurve(
                time=float(row["Second (#)"]),
                force_n=[int(float(row[f"#{i} (N)"])) for i in range(1, FORCE_SAMPLES + 1)],
                power_w=int(float(row["Actual power (W)"])),
                stroke_rate_spm=float(row["Stroke rate (SPM)"]),
                stroke_length_cm=int(float(row["Stroke length (cm)"])),
                work_j=float(row["Work (J)"]),
                stroke_count=int(float(row["Stroke (#)"])),
                heart_rate_bpm=_number(row.get("Heart rate (bpm)")),
                distance_m=_number(row.get("Distance (m)")),
            )


def export_totals(path: Path) -> dict:
    """Start time (from the export's UTC timestamps), distance and elapsed time of a session."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    first, last = rows[0], rows[-1]
    started = datetime.strptime(first["Timestamp (UTC)"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return {"started_at": started.timestamp() - float(first["Second (#)"]),
            "distance_m": int(float(last["Distance (m)"])), "elapsed_s": int(float(last["Second (#)"]))}


def replay(path: Path, settings: CoachSettings, save_dir: Path | None = None) -> dict:
    """Run one export through a fresh coach. Returns its intervals and live-cue counts."""
    coach = IntervalCoach(settings)
    cues: dict[int, Counter] = {}
    for stroke in strokes_from_csv(path):
        coach.add(stroke)
        live = coach.state(stroke.time).get("live")
        if live:
            cues.setdefault(live["index"], Counter())[live["cue"]] += 1
    coach.state(float("inf"))  # an effort still running at the end of the file
    saved = None
    if save_dir is not None:
        totals = export_totals(path)
        record = sessions.build_record(coach, totals["started_at"], totals["started_at"] + totals["elapsed_s"],
                                       True, totals["distance_m"], totals["elapsed_s"], source="replay")
        saved = sessions.save(save_dir, record, sessions.stroke_rows(coach))
    return {"intervals": coach.intervals, "cues": cues, "steady_power_w": coach.steady_power(),
            "strokes": coach.last_count, "saved": saved}


def describe(path: Path, result: dict) -> str:
    lines = [f"{path.name}: {result['strokes']} strokes, steady about {result['steady_power_w']:.0f} W"]
    if not result["intervals"]:
        lines.append("  no intervals")
    for summary in result["intervals"]:
        lines.append(f"  {summary.log_line()}")
        counts = result["cues"].get(summary.index, Counter())
        total = sum(counts.values())
        if total:
            shares = ", ".join(f"{CUE_NAMES.get(cue, cue)} {n * 100 // total}%"
                               for cue, n in counts.most_common())
            lines.append(f"      live cue: {shares}")
    if result.get("saved"):
        lines.append(f"  summary saved: {result['saved']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("csv", nargs="+", type=Path, help="SmartRow app CSV export(s)")
    p.add_argument("--start-pct", type=float, default=20.0,
                   help="an interval is at least this %% above steady power (default 20)")
    p.add_argument("--max-min", type=float, default=5.0,
                   help="longest effort, in minutes, to count as an interval (default 5)")
    p.add_argument("--sensitivity", choices=("low", "normal", "high"), default="normal",
                   help="how readily the technique cues speak up (default normal)")
    p.add_argument("--save", type=Path, metavar="DIR",
                   help="also write each session's summary file into DIR, as the bridge does")
    args = p.parse_args(argv)
    logging.disable(logging.INFO)  # the summary lines are printed below instead
    settings = CoachSettings(start_pct=args.start_pct, max_effort_s=args.max_min * 60,
                             sensitivity=args.sensitivity)
    status = 0
    for path in args.csv:
        try:
            print(describe(path, replay(path, settings, args.save)))
        except (OSError, KeyError, ValueError) as exc:
            print(f"{path.name}: can't read it as a SmartRow export ({exc})", file=sys.stderr)
            status = 1
        print()
    return status


if __name__ == "__main__":
    sys.exit(main())
