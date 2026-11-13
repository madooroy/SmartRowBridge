# SPDX-License-Identifier: GPL-3.0-only
"""The parts of each stroke, timed with the seat sensor and the pulley's drive flag together.

The seat sensor (seat.py) sees the seat about 100 times a second, on the same clock as the
pulley's records; the drive flag (protocol.FLAG_BIT) is on while the handle is loaded, and its
edges are placed to about +-0.05 s (protocol.StrokeCurve.flag_on_t / flag_off_t). Together:

    catch ........ the seat leaves the front stop (seat sensor)
    load ......... the flag comes on: the handle takes load (pulley)
    legs flat .... the seat reaches the back (seat sensor)
    finish ....... the flag goes off: the load ends as the paddle runs on (pulley). Filmed 10 Nov 2026:
                   the hands then still move 0.1-0.2 s (1-4 % of their travel) to the body; the drive is
                   timed to the load ending (Marc's choice), not to the hands
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
CROSS_CM = 3.0  # the seat is moving once it is this far from where it rests (front or back)
SPEED_S = 0.05  # its speed at a reading: a line through the readings this far either side
V_LO, V_HI = 20.0, 60.0  # cm/s: the speed rising (or falling) through this range is extended to zero
STILL_CMS = 6.0  # cm/s: slower than this the seat is still; a start is never placed before the last such
#                  reading (nor a stop after the first): a slide that never reaches V_HI bends the line
MAX_BACK_S = 0.4  # ... at most this far from where it reached V_LO
REST_BAND_CM = 1.5  # where the seat rests: the median of the readings within this of its extreme
MIN_TRAVEL_CM = 15.0  # less seat travel than this (arms-only drills, a pause): no phases
LOOK_BACK_S = 3.0  # without a previous finish, the catch is looked for this far before the load
AFTER_OFF_S = 0.35  # legs flat is looked for up to this long after the finish
MAX_RECOVERY_S = 6.0  # a longer gap from one finish to the next catch is a pause, not a recovery
MAX_GAP_S = 0.15  # a gap in the seat readings longer than this inside a phase: no phases
SAME_FINISH_S = 0.15  # the previous row's finish must match this stroke's own (flag recovery) this closely

# Stroke-log row positions (coach.IntervalCoach.stroke_log)
I_SPM, I_FLAG_RECOVERY, I_FLAG_ON, I_FLAG_OFF, I_CATCH, I_LEGS, I_KNEES = 3, 9, 10, 11, 12, 13, 14


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


def _rest(cms: list[float], lo: int, hi: int, top: bool) -> float:
    """Where the seat rests at the front (top) or the back: the median of the readings within
    REST_BAND_CM of the extreme, so a centimetre of flicker (46, 45, 47...) doesn't move it."""
    part = cms[lo:hi] or cms
    ext = max(part) if top else min(part)
    near = sorted(c for c in part if abs(c - ext) <= REST_BAND_CM)
    return near[len(near) // 2]


def _speed(ts: list[float], cms: list[float], k: int) -> float:
    """cm/s at reading k: the slope of a line through the readings within SPEED_S of it."""
    idx = [j for j in range(len(ts)) if abs(ts[j] - ts[k]) <= SPEED_S]
    if len(idx) < 3:
        return 0.0
    mt = sum(ts[j] for j in idx) / len(idx)
    mc = sum(cms[j] for j in idx) / len(idx)
    den = sum((ts[j] - mt) ** 2 for j in idx)
    return sum((ts[j] - mt) * (cms[j] - mc) for j in idx) / den if den else 0.0


def _cross_time(ts: list[float], cms: list[float], k: int, level: float) -> float:
    """When the line between readings k-1 and k crosses level."""
    a, b = cms[k - 1], cms[k]
    return ts[k] if a == b else ts[k - 1] + (ts[k] - ts[k - 1]) * (level - a) / (b - a)


def _from_crossing(ts: list[float], cms: list[float], k: int, level: float, leaving: bool) -> float:
    """The moment the seat started moving (leaving) or stopped, around where it crossed `level`
    (between readings k-1 and k): its speed, as it rises from V_LO to V_HI (or falls from V_HI to
    V_LO), follows a line, and the moment is where that line reaches zero. So a centimetre of
    flicker, a slow creep or roll (a few cm/s) and where exactly it rests don't move it; at most
    MAX_BACK_S from where the speed passed V_LO; the crossing itself if the speed can't be read."""
    t_cross = _cross_time(ts, cms, k, level)
    n = len(ts)
    sp = {}

    def v(i: int) -> float:
        if i not in sp:
            sp[i] = abs(_speed(ts, cms, i))
        return sp[i]

    if leaving:  # back from the crossing to where it was still slow, then forward to where it is fast
        lo = k
        while lo > 0 and v(lo - 1) >= V_LO and ts[k] - ts[lo - 1] <= MAX_BACK_S:
            lo -= 1
        hi = lo
        while hi + 1 < n and v(hi) < V_HI and ts[hi + 1] - ts[lo] <= 0.3:
            hi += 1
        idx = [i for i in range(lo, hi + 1) if V_LO * 0.5 <= v(i) <= V_HI * 1.2]
    else:  # forward from the crossing to where it is slow, keeping the falling part
        hi = k
        while hi + 1 < n and v(hi + 1) >= V_LO and ts[hi + 1] - ts[k] <= MAX_BACK_S:
            hi += 1
        lo = hi
        while lo > 0 and v(lo) < V_HI and ts[hi] - ts[lo - 1] <= 0.3:
            lo -= 1
        idx = [i for i in range(lo, hi + 1) if V_LO * 0.5 <= v(i) <= V_HI * 1.2]
    if len(idx) < 3:
        return t_cross
    mt = sum(ts[i] for i in idx) / len(idx)
    mv = sum(v(i) for i in idx) / len(idx)
    den = sum((ts[i] - mt) ** 2 for i in idx)
    slope = sum((ts[i] - mt) * (v(i) - mv) for i in idx) / den if den else 0.0
    if (leaving and slope <= 0) or (not leaving and slope >= 0):
        return t_cross
    t0 = mt - mv / slope  # where the speed line reaches zero
    edge = ts[idx[0]] if leaving else ts[idx[-1]]
    if leaving:
        still = [i for i in range(0, k) if v(i) < STILL_CMS and ts[k] - ts[i] <= MAX_BACK_S + 0.3]
        floor = ts[still[-1]] if still else edge - MAX_BACK_S
        return min(t_cross, max(t0, edge - MAX_BACK_S, floor))
    still = [i for i in range(k, n) if v(i) < STILL_CMS and ts[i] - ts[k] <= MAX_BACK_S + 0.3]
    ceiling = ts[still[0]] if still else edge + MAX_BACK_S
    return max(t_cross, min(t0, edge + MAX_BACK_S, ceiling))


def stroke_events(readings: Sequence[tuple[float, float]], flag_on: float, flag_off: float,
                  prev_off: float | None, prev_legs: float | None) -> dict | None:
    """{"catch", "legs", "knees_before"} for one stroke (clock times), or None.

    readings: (time, cm from the lidar) covering at least prev_off to flag_off + AFTER_OFF_S. The
    lidar is behind the rower, so the seat at the front (the catch) is the largest distance. Each
    moment is timed from where the seat crosses CROSS_CM from rest (_from_crossing), not from where
    it first differs by a reading: the lidar flickers by a centimetre at rest, and the seat creeps
    a little while parked and during the body swing."""
    if not flag_on or not flag_off or flag_off <= flag_on:
        return None
    start = prev_off if prev_off and 0 < flag_on - prev_off <= MAX_RECOVERY_S + 2 else flag_on - LOOK_BACK_S
    w = _window(readings, start, flag_off + AFTER_OFF_S)
    if w is None:
        return None
    ts, cms = w
    before = [i for i, t in enumerate(ts) if t <= flag_off]
    if not before:  # no reading before the finish (the sensor just started)
        return None
    end_drive = before[-1] + 1  # the catch is before the finish
    i_front = max(range(end_drive), key=lambda i: (cms[i], i))  # the last reading at the front-most point
    front = _rest(cms, max(0, i_front - 40), i_front + 1, top=True)
    i_back = min(range(i_front, len(cms)), key=lambda i: cms[i])
    back = _rest(cms, i_back, len(cms), top=False)
    if front - back < MIN_TRAVEL_CM:
        return None
    k = next((i for i in range(i_front + 1, len(cms)) if cms[i] <= front - CROSS_CM), None)
    if k is None:
        return None
    catch = max(_from_crossing(ts, cms, k, front - CROSS_CM, leaving=True), ts[i_front] - MAX_BACK_S)
    j = next((i for i in range(k, len(cms)) if cms[i] <= back + CROSS_CM), None)
    if j is None or j == 0:
        return None
    legs = _from_crossing(ts, cms, j, back + CROSS_CM, leaving=False)
    knees = None
    if prev_legs is not None and prev_off is not None and prev_legs < catch:
        w3 = _window(readings, prev_legs, catch)
        if w3 is not None:
            ts3, cms3 = w3
            i_top = max(range(len(cms3)), key=lambda i: (cms3[i], i))
            rest = _rest(cms3, 0, i_top + 1, top=False)
            # the last time the seat rose through rest + CROSS_CM on its way to this catch
            ups = [i for i in range(1, i_top + 1) if cms3[i - 1] < rest + CROSS_CM <= cms3[i]]
            if ups:
                t = _from_crossing(ts3, cms3, ups[-1], rest + CROSS_CM, leaving=True)
                if t > prev_off - 0.3:
                    knees = t
    return {"catch": catch, "legs": legs, "knees_before": knees}


def previous_finish(prev: Sequence | None, row: Sequence) -> float | None:
    """The previous stroke's finish, if the row before is really the previous stroke: its flag-off
    must match this stroke's own count of the time since the last finish (flag recovery), else a
    stroke is missing from the log in between and nothing is paired across the gap."""
    off = _get(prev, I_FLAG_OFF) if prev is not None else None
    on, rec = _get(row, I_FLAG_ON), _get(row, I_FLAG_RECOVERY)
    if not off:
        return None
    if on and rec:
        return off if abs((on - rec) - off) <= SAME_FINISH_S else None
    return off if on and 0 < on - off <= MAX_RECOVERY_S else None


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
    prev_off = previous_finish(prev, row)
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
    pairs = [d for d in pairs if d]
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
