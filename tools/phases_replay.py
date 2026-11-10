#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""The stroke parts (smartrow_bridge/phases.py) of a saved session, from its strokes and seat files.

    python3 tools/phases_replay.py SESSION.strokes.json [SESSION.seat.csv] [--csv OUT.csv] [--lag 0.45]

Prints the medians of each part for the Easy strokes (under --easy-w watts, default 135) and the
efforts, the Easy rowing by thirds, and with --csv writes one line per stroke: clock times of the
catch, load, legs flat, finish and knees rise, and the seconds of each part. For checking the
coach against video (pose_video.py), and for rows saved before the coach timed the parts itself.

Sessions saved from 10 Nov 2026 have the drive flag's edges (flag_on_t / flag_off_t). Older ones
have only the flag's durations: their finish is then placed --lag seconds before the stroke record
(0.38-0.52 s on video), which makes their drive and recovery good to only about 0.1 s.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from smartrow_bridge import phases  # noqa: E402


def load_seat(path: Path) -> tuple[list[float], list[float]]:
    ts, cms = [], []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if int(row.get("strength") or 0) >= 100:  # seat.WEAK_SIGNAL
                ts.append(float(row["t"]))
                cms.append(float(row["cm"]))
    return ts, cms


def replay(strokes: dict, seat: tuple[list[float], list[float]], lag: float) -> tuple[list[tuple], bool]:
    """Stroke-log-like rows (phases.I_* positions) with the seat's events, as the coach makes them."""
    cols = strokes["columns"]
    st, sc = seat
    exact = "flag_off_t" in cols
    rows, prev = [], None
    for r in strokes["rows"]:
        d = dict(zip(cols, r))
        on, off = d.get("flag_on_t"), d.get("flag_off_t")
        if not exact and d.get("flag_drive_s"):
            off = d["t"] - lag
            on = off - d["flag_drive_s"]
        ev = None
        if on is not None and off is not None:
            prev_off = prev[phases.I_FLAG_OFF] if prev else None
            prev_legs = prev[phases.I_LEGS] if prev else None
            start = (prev_off if prev_off and on - prev_off <= phases.MAX_RECOVERY_S + 2 else on - phases.LOOK_BACK_S) - 0.5
            i, j = bisect.bisect_left(st, start), bisect.bisect_right(st, d["t"])
            ev = phases.stroke_events(list(zip(st[i:j], sc[i:j])), on, off, prev_off or None, prev_legs or None)
        row = (d["t"], d.get("distance_m"), d.get("power_w"), d.get("spm"), d.get("heart_rate"), d.get("length_cm"),
               (), d.get("drive_s"), d.get("flag_drive_s"), d.get("flag_recovery_s"), on or 0.0, off or 0.0,
               ev["catch"] if ev else 0.0, ev["legs"] if ev else 0.0, (ev["knees_before"] or 0.0) if ev else 0.0)
        rows.append(row)
        prev = row if on is not None else None
    return rows, exact


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("strokes")
    ap.add_argument("seat", nargs="?", help="default: the .seat.csv beside the strokes file")
    ap.add_argument("--csv", help="write one line per stroke here")
    ap.add_argument("--lag", type=float, default=0.45, help="older files: finish to stroke record, s")
    ap.add_argument("--easy-w", type=float, default=135.0)
    args = ap.parse_args(argv)
    sp = Path(args.strokes)
    seat_path = Path(args.seat) if args.seat else sp.with_name(sp.name.replace(".strokes.json", ".seat.csv"))
    strokes = json.loads(sp.read_text(encoding="utf-8"))
    rows, exact = replay(strokes, load_seat(seat_path), args.lag)
    pairs = phases.rows_durations(rows)
    print(f"{strokes.get('id', sp.stem)}: {len(pairs)} of {len(rows)} strokes timed"
          + ("" if exact else f" (older file: finish placed {args.lag} s before the stroke record, about +-0.1 s)"))
    easy = [(r, d) for r, d in pairs if (r[2] or 0) < args.easy_w]
    hard = [(r, d) for r, d in pairs if (r[2] or 0) >= args.easy_w]
    for name, group in (("Easy", easy), (f"{args.easy_w:.0f} W and up", hard)):
        if not group:
            continue
        m = phases.medians([d for _, d in group])
        ratio = m["recovery"] / m["drive"] if m["recovery"] and m["drive"] else float("nan")
        spm = statistics.median(r[3] for r, _ in group if r[3])
        print(f"  {name} ({len(group)} strokes, {spm:.1f} spm): drive {m['drive']} s = legs {m['legs']} "
              f"(load {m['load']} s in) + body/arms {m['body']}; recovery {m['recovery']} s = parked {m['parked']} "
              f"+ slide {m['slide']}; 1:{ratio:.2f}")
    if len(easy) >= 30:
        n = len(easy)
        for k, label in enumerate(("first third", "middle", "last third")):
            m = phases.medians([d for _, d in easy[k * n // 3:(k + 1) * n // 3]])
            print(f"    {label}: drive {m['drive']} recovery {m['recovery']} parked {m['parked']} slide {m['slide']} "
                  f"load {m['load']} body {m['body']}")
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["t", "power_w", "spm", "catch_t", "load_t", "legs_flat_t", "finish_t", "knees_before_t",
                        *phases.PARTS])
            for r, d in pairs:
                w.writerow([r[0], r[2], r[3], *(round(r[i], 3) if r[i] else "" for i in
                                                (phases.I_CATCH, phases.I_FLAG_ON, phases.I_LEGS, phases.I_FLAG_OFF,
                                                 phases.I_KNEES)),
                            *("" if d[k] is None else round(d[k], 3) for k in phases.PARTS)])
        print(f"  wrote {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
