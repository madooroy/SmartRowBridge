# SPDX-License-Identifier: GPL-3.0-only
"""The parts of each stroke, timed with the seat sensor and the pulley's drive flag together.

The seat sensor (seat.py) sees the seat about 100 times a second, on the same clock as the
pulley's records; the drive flag (protocol.FLAG_BIT) is on while the handle is loaded, and its
edges are placed to about +-0.05 s (protocol.StrokeCurve.flag_on_t / flag_off_t). Together:

    catch ........ the seat leaves the front stop (seat sensor)
    load ......... the flag comes on: the handle takes load (pulley)
    legs flat .... the seat reaches the back (seat sensor)
    finish ....... the flag goes off (pulley; within 0.02 s of the finish on film, 4 Nov 2026)
    knees rise ... the seat starts forward again (seat sensor)
    next catch

Drive = catch to finish, in three parts: catch to load (negative when the handle is loaded before
the seat moves: arms or back first), legs (catch to legs flat), body and arms (legs flat to the
finish). Recovery = finish to the next catch, in two parts: parked (finish to knees rise: hands
away and body over happen here) and the slide (knees rise to the catch).

A stroke is worked out when its stroke record arrives (about 0.4 s after the finish), from the seat
readings since the previous finish; the recovery before it and the previous stroke's knees rise
come with it. Each stroke-log row keeps its own catch and legs-flat times and the knees rise of
the recovery before it (coach.py), and durations() turns two neighbouring rows into seconds.
"""

from __future__ import annotations

import math
import statistics
from typing import Sequence

SMOOTH = 5  # readings averaged (about 0.05 s): the lidar reads whole centimetres
CATCH_FIT_CM = 3.0  # the catch: the turning point of a parabola through the readings this close to the front
CATCH_FIT_S = 0.35  # ... and no further than this either side of the front-most one
FLAT_TOL_CM = 1.0  # legs flat: the first moment the seat is this close to its back-most point
MIN_TRAVEL_CM = 15.0  # less seat travel than this (arms-only drills, a pause): no phases
LOOK_BACK_S = 3.0  # without a previous finish, the catch is looked for this far before the load
AFTER_OFF_S = 0.35  # legs flat is looked for up to this long after the finish
MAX_RECOVERY_S = 6.0  # a longer gap from one finish to the next catch is a pause, not a recovery
MAX_GAP_S = 0.15  # a gap in the seat readings longer than this inside a phase: no phases

# Stroke-log row positions (coach.IntervalCoach.stroke_log)
I_SPM, I_FLAG_ON, I_FLAG_OFF, I_CATCH, I_LEGS, I_KNEES = 3, 10, 11, 12, 13, 14


def _smooth(cms: Sequence[float]) -> list[float]:
    n, half = len(cms), SMOOTH // 2
    out = []
    for i in range(n):
        a, b = max(0, i - half), min(n, i + half + 1)
        out.append(sum(cms[a:b]) / (b - a))
    return out


def _window(readings: Sequence[tuple[float, float]], a: float, b: float) -> tuple[list[float], list[float]] | None:
    """Times and smoothed cm of the readings from a to b; None if the readings have a gap."""
    sel = [(t, c) for t, c in readings if a - 0.1 <= t <= b + 0.1]  # a little either side for the smoothing
    if len(sel) < 10:
        return None
    ts = [t for t, _ in sel]
    if any(y - x > MAX_GAP_S for x, y in zip(ts, ts[1:])):
        return None
    cms = _smooth([c for _, c in sel])
    keep = [i for i, t in enumerate(ts) if a <= t <= b]
    if len(keep) < 5:
        return None
    return [ts[i] for i in keep], [cms[i] for i in keep]


def _turning_point(ts: list[float], cms: list[float], i: int) -> float:
    """When the seat turned at the front: the vertex of a parabola fitted (least squares) to the
    readings near the front-most one. The lidar reads whole centimetres, so the front looks flat for
    a while; the parabola finds the middle of the turn, where the seat stops coming forward and starts
    going back. Falls back to the middle of the readings at the very front."""
    front = cms[i]
    while i + 1 < len(cms) and cms[i + 1] >= front:  # the last reading at the very front
        i += 1
    j = i
    while j + 1 < len(cms) and cms[j + 1] >= front - 0.5:
        j += 1
    fallback = ts[j]  # the seat leaving the front (a long wait at the front, e.g. before the first stroke)
    if all(c >= front - 0.5 for t, c in zip(ts, cms) if ts[i] - CATCH_FIT_S <= t <= ts[i]):
        return fallback  # it sat at the front: no turn to fit
    near = [k for k in range(len(ts)) if abs(ts[k] - ts[i]) <= CATCH_FIT_S and cms[k] >= front - CATCH_FIT_CM]
    if len(near) < 7:
        return fallback
    t0 = ts[i]
    xs = [ts[k] - t0 for k in near]
    ys = [cms[k] for k in near]
    n = len(xs)
    s1, s2, s3, s4 = (sum(x ** p for x in xs) for p in (1, 2, 3, 4))
    y0, y1, y2 = sum(ys), sum(x * y for x, y in zip(xs, ys)), sum(x * x * y for x, y in zip(xs, ys))
    # normal equations for y = a x^2 + b x + c
    m = [[s4, s3, s2, y2], [s3, s2, s1, y1], [s2, s1, n, y0]]
    for col in range(3):  # Gaussian elimination
        piv = max(range(col, 3), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            return fallback
        m[col], m[piv] = m[piv], m[col]
        for r in range(3):
            if r != col:
                f = m[r][col] / m[col][col]
                m[r] = [a - f * b for a, b in zip(m[r], m[col])]
    a, b = m[0][3] / m[0][0], m[1][3] / m[1][1]
    if a >= 0:  # not a peak
        return fallback
    vertex = t0 - b / (2 * a)
    return vertex if min(xs) <= vertex - t0 <= max(xs) else fallback


def stroke_events(readings: Sequence[tuple[float, float]], flag_on: float, flag_off: float,
                  prev_off: float | None, prev_legs: float | None) -> dict | None:
    """{"catch", "legs", "knees_before"} for one stroke (clock times), or None.

    readings: (time, cm from the lidar) covering at least prev_off to flag_off + AFTER_OFF_S. The
    lidar is behind the rower, so the seat at the front (the catch) is the largest distance."""
    if not flag_on or not flag_off or flag_off <= flag_on:
        return None
    start = prev_off if prev_off and 0 < flag_on - prev_off <= MAX_RECOVERY_S + 2 else flag_on - LOOK_BACK_S
    w = _window(readings, start, flag_off)
    if w is None:
        return None
    ts, cms = w
    front = max(cms)
    catch = _turning_point(ts, cms, cms.index(front))
    w2 = _window(readings, catch, flag_off + AFTER_OFF_S)
    if w2 is None:
        return None
    ts2, cms2 = w2
    back = min(cms2)
    if front - back < MIN_TRAVEL_CM:
        return None
    legs = next(t for t, c in zip(ts2, cms2) if c <= back + FLAT_TOL_CM)
    knees = None
    if prev_legs is not None and prev_off is not None and prev_legs < catch:
        w3 = _window(readings, prev_legs, catch)
        if w3 is not None:
            ts3, cms3 = w3
            rest = min(cms3)
            near = [t for t, c in zip(ts3, cms3) if c <= rest + FLAT_TOL_CM]
            if near and near[-1] > prev_off - 0.3:
                knees = near[-1]
    return {"catch": catch, "legs": legs, "knees_before": knees}


def _get(row: Sequence, i: int) -> float | None:
    return row[i] if len(row) > i and row[i] else None


def durations(prev: Sequence | None, row: Sequence) -> dict | None:
    """Seconds of each part of this stroke (and of the recovery before it) from two stroke-log rows,
    or None when this stroke has no seat-timed catch. Recovery parts are None after a pause."""
    on, off, catch, legs = (_get(row, i) for i in (I_FLAG_ON, I_FLAG_OFF, I_CATCH, I_LEGS))
    if not (on and off and catch and legs):
        return None
    out = {"load": on - catch, "legs": legs - catch, "body": off - legs, "drive": off - catch,
           "recovery": None, "parked": None, "slide": None}
    prev_off = _get(prev, I_FLAG_OFF) if prev is not None else None
    if prev_off and 0 < catch - prev_off <= MAX_RECOVERY_S:
        out["recovery"] = catch - prev_off
        knees = _get(row, I_KNEES)
        if knees and prev_off - 0.3 <= knees <= catch:  # slightly before the finish: knees early
            out["parked"], out["slide"] = knees - prev_off, catch - knees
    return out


PARTS = ("load", "legs", "body", "drive", "parked", "slide", "recovery")


def medians(pairs: list[dict]) -> dict:
    """Median of each part over strokes (None where none had it), seconds to 2 decimals."""
    out = {}
    for k in PARTS:
        v = [d[k] for d in pairs if d.get(k) is not None]
        out[k] = round(statistics.median(v), 2) if v else None
    return out


def rows_durations(rows: Sequence[Sequence]) -> list[tuple[Sequence, dict]]:
    """(row, durations) for every row that has them, each with the row before it."""
    out = []
    for i, r in enumerate(rows):
        d = durations(rows[i - 1] if i else None, r)
        if d is not None:
            out.append((r, d))
    return out


def fmt(s: float | None) -> str:
    return "–" if s is None or math.isnan(s) else f"{s:.1f}"
