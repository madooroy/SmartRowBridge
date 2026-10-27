# SPDX-License-Identifier: GPL-3.0-only
"""What the activity files (fitfile.py, tcxfile.py) share: the session's laps.

Laps run back to back over the whole session: steady rowing, each interval the coach found,
steady rowing... so the efforts show up as laps (intervals) in intervals.icu and Strava.
Times are seconds from the first stroke.
"""

from __future__ import annotations


def laps(record: dict, strokes: list[dict]) -> list[dict]:
    """Contiguous laps: steady rowing, each interval, steady rowing... (times from the first stroke)."""
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
        if start > cursor:
            out.append({"start": cursor, "end": start, "interval": None})
        out.append({"start": start, "end": stop, "interval": index})
        cursor = stop
    if end > cursor or not out:
        out.append({"start": cursor, "end": end, "interval": None})
    if len(out) > 1 and out[-1]["interval"] is None and out[-1]["end"] - out[-1]["start"] < 1:
        out[-2]["end"] = out.pop()["end"]  # a sliver at the end belongs to the lap before
    return out


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
