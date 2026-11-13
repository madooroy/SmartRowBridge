# SPDX-License-Identifier: GPL-3.0-only
"""What the activity files (fitfile.py, tcxfile.py) share: the session's laps.

Laps run back to back over the whole session: Easy rowing, each effort the coach found, Easy
rowing... so the efforts show up as laps (intervals) in intervals.icu and Strava. The rowing
between efforts is split into Easy and Recovery laps by power (Recovery: under 44 % of FTP, or
under 80 % of Easy without one; at least RECOVERY_MIN_S long). Each lap carries its intensity
("kind": Recovery, Easy, Moderate, Challenging or Max). Times are seconds from the first stroke.
"""

from __future__ import annotations

import statistics

MIN_GAP_S = 5.0  # a shorter gap between two efforts is no lap of its own
RECOVERY_MIN_S = 15.0  # shorter dips stay part of the Easy rowing around them
SMOOTH = 5  # strokes (median) to judge each stroke's power by


def laps(record: dict, strokes: list[dict], split_easy: bool = False) -> list[dict]:
    """Contiguous laps: the rowing before the first effort, each effort, the rowing between...
    (times from the first stroke). The rowing between efforts is one lap ("Easy"), or with
    split_easy, Easy and Recovery laps of their own."""
    end = strokes[-1]["t"]
    cuts = []  # (start, end, is_interval, index)
    for i in record.get("intervals") or []:
        start = i["started_at_s"] if "started_at_s" in i else None
        if start is None:
            continue
        cuts.append((max(0.0, start), min(end, start + i["duration_s"]), True, i["index"]))
    cuts.sort()
    out, cursor = [], 0.0
    for start, stop, _, index in cuts:
        start = max(start, cursor)
        if stop <= start:
            continue
        if out and out[-1]["interval"] is not None and start - cursor < MIN_GAP_S:
            start = cursor  # an effort split at an intensity step: the parts touch, no lap between
        if start > cursor:
            out.append({"start": cursor, "end": start, "interval": None})
        out.append({"start": start, "end": stop, "interval": index})
        cursor = stop
    if end > cursor or not out:
        out.append({"start": cursor, "end": end, "interval": None})
    if len(out) > 1 and out[-1]["interval"] is None and out[-1]["end"] - out[-1]["start"] < 1:
        out[-2]["end"] = out.pop()["end"]  # a sliver at the end belongs to the lap before
    kinds = {i["index"]: i.get("intensity") or "Moderate" for i in record.get("intervals") or []}
    recovery_w = _recovery_w(record)
    split = []
    for lap in out:
        if lap["interval"] is not None:
            split.append({**lap, "kind": kinds.get(lap["interval"], "Moderate")})
        elif split_easy:
            split += _easy_and_recovery(lap, strokes, recovery_w)
        else:
            split.append({**lap, "kind": "Easy"})
    return split


def _recovery_w(record: dict) -> float:
    """Below this power, rowing between efforts is Recovery (coach.FTP_BOUNDS_PCT / EASY_BOUNDS)."""
    if record.get("ftp"):
        return record["ftp"] * 0.44
    return (record.get("steady_power_w") or 0) * 0.80


def _easy_and_recovery(lap: dict, strokes: list[dict], recovery_w: float) -> list[dict]:
    """A stretch between efforts as Easy and Recovery laps (Recovery at least RECOVERY_MIN_S)."""
    inside = [s for s in strokes if lap["start"] <= s["t"] < lap["end"]]
    if not inside or not recovery_w:
        return [{**lap, "kind": "Easy"}]
    powers = [s["power_w"] for s in inside]
    low = [statistics.median(powers[max(0, i - SMOOTH // 2):i + SMOOTH // 2 + 1]) < recovery_w
           for i in range(len(inside))]
    # runs of the same kind, as (kind, start time)
    runs = []
    for s, is_low in zip(inside, low):
        kind = "Recovery" if is_low else "Easy"
        if not runs or runs[-1][0] != kind:
            runs.append([kind, s["t"]])
    runs[0][1] = lap["start"]
    # too-short Recovery dips stay Easy; then merge neighbours of the same kind
    bounds = [r[1] for r in runs[1:]] + [lap["end"]]
    runs = [[("Easy" if k == "Recovery" and end - start < RECOVERY_MIN_S else k), start]
            for (k, start), end in zip(runs, bounds)]
    merged = []
    for kind, start in runs:
        if not merged or merged[-1][0] != kind:
            merged.append([kind, start])
    bounds = [r[1] for r in merged[1:]] + [lap["end"]]
    return [{"start": start, "end": end, "interval": None, "kind": kind}
            for (kind, start), end in zip(merged, bounds)]


def lap_strokes(lap_list: list[dict], strokes: list[dict]) -> list[list[dict]]:
    """Each stroke in exactly one lap: from its start up to (not including) its end; the last lap
    includes its end."""
    out = [[] for _ in lap_list]
    i = 0
    for s in strokes:
        while i < len(lap_list) - 1 and s["t"] >= lap_list[i]["end"]:
            i += 1
        out[i].append(s)
    return out
