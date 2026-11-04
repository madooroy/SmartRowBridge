# SPDX-License-Identifier: GPL-3.0-only
"""Rhythm: how long the drive and the recovery take, and their ratio.

The pulley doesn't send drive time, but its drive flag (protocol.FLAG_BIT) is on while the handle
is loaded. Filmed at 60 fps with the Display HAT screen lit by the flag (sniff_pulley --flash,
4 Nov 2026, 147 strokes at 18-30 spm, gentle and sharp catches):
- the flag goes off at the finish (within 0.02 s, every rate);
- it comes on 0.3-0.6 s after the catch, once the handle takes load: the missing part of the
  drive is all at the catch, longer at low rates and with a gentle catch.
So drive = flag + LAG_A - LAG_B x spm (0.48 s at 20 spm, 0.26 s at 30; per stroke within about
0.08 s; checked against both filmed rows that day). The recovery is the rest of the stroke: the
flag's on + off time is one whole stroke. Only medians over many strokes are reported.

A SmartRow handle's export measures drive time itself (the stroke log's drive time); when a stroke
has it, that is used instead and the recovery is the stroke period less the drive.

Works from the coach's stroke log: (time, distance, power, spm, heart rate, length, force,
drive time, flag drive, flag recovery).
"""

from __future__ import annotations

import statistics

LAG_A, LAG_B = 0.92, 0.022  # catch to flag on, s: LAG_A - LAG_B x spm (4 Nov 2026 flash video)
LAG_MIN_S = 0.15
WARMUP_S = 120.0  # Easy rhythm from here on (the first minutes are settling in)
AFTER_EFFORT_S = 35.0  # strokes this soon after an effort are recovery from it, not Easy rowing
MIN_STROKES = 10  # fewer strokes with a drive time: no rhythm reported
PERIOD_TOL = 0.25  # a stroke whose flag cycle and stroke rate disagree by more than this is skipped
AIM_RATIO = 2.0  # the usual aim at low rates: recovery about twice the drive
LOW_RATE_SPM = 24.0  # the aim is given only at or below this rate
NEAR_AIM = 1.7  # from here the rhythm counts as close to the aim
ROUGH_ABOVE_SPM = 32.0  # pulley estimates above this rate are marked rough (calibrated at 18-30 spm)


def drive_from_flag(flag_s: float, spm: float) -> float:
    return flag_s + max(LAG_MIN_S, LAG_A - LAG_B * spm)


def stroke_rhythm(row: tuple) -> tuple[float, float, str] | None:
    """(drive s, recovery s, source) for one stroke-log row, or None when it has no usable drive time."""
    spm = row[3]
    handle = row[7] if len(row) > 7 else 0.0
    flag, flag_rec = (row[8], row[9]) if len(row) > 9 else (0.0, 0.0)
    if handle and spm:
        period, drive, source = 60.0 / spm, handle, "handle"
    elif flag and flag_rec:
        period = flag + flag_rec
        drive, source = drive_from_flag(flag, 60.0 / period), "pulley"
        if spm and abs(period * spm / 60.0 - 1) > PERIOD_TOL:
            return None  # e.g. a pause or a missed record between the two runs
    else:
        return None
    recovery = period - drive
    if recovery <= 0:
        return None
    return drive, recovery, source


def summarise(rows: list[tuple]) -> dict | None:
    """Medians over the rows that have a drive time; None when too few do."""
    got = [(r, x) for r in rows if (x := stroke_rhythm(r))]
    if len(got) < MIN_STROKES:
        return None
    drive = statistics.median(x[0] for _, x in got)
    recovery = statistics.median(x[1] for _, x in got)
    return {"strokes": len(got), "spm": round(statistics.median(r[3] for r, _ in got), 1),
            "drive_s": round(drive, 2), "recovery_s": round(recovery, 2),
            "ratio": round(recovery / drive, 2), "source": got[0][1][2]}


def session_rhythm(log: list[tuple], intervals: list[dict]) -> dict | None:
    """Rhythm of the Easy rowing and of each effort. intervals: the session's efforts (as_dict with
    started_at_s, seconds after the first stroke)."""
    if not log:
        return None
    t0 = log[0][0]
    spans = [(i["started_at_s"], i["started_at_s"] + i["duration_s"]) for i in intervals if "started_at_s" in i]
    easy = [r for r in log if r[0] - t0 >= WARMUP_S
            and not any(a - 5 <= r[0] - t0 <= b + AFTER_EFFORT_S for a, b in spans)]
    efforts = []
    for i, (a, b) in zip((i for i in intervals if "started_at_s" in i), spans):
        s = summarise([r for r in log if a <= r[0] - t0 <= b])
        if s:
            efforts.append({"index": i["index"], **s})
    out = {"easy": summarise(easy), "efforts": efforts}
    return out if out["easy"] or efforts else None


def _rough(x: dict) -> bool:
    return x.get("source") == "pulley" and x["spm"] > ROUGH_ABOVE_SPM


def _ratio(x: float) -> str:
    return f"1:{x:.0f}" if x == round(x) else f"1:{x:.1f}"


def rhythm_findings(rh: dict | None) -> list[str]:
    """One or two sentences: the Easy rhythm (with the aim, at low rates) and the efforts'."""
    if not rh:
        return []
    out = []
    easy = rh.get("easy")
    estimated = " (drive estimated from the pulley)" if any(
        x and x.get("source") == "pulley" for x in [easy, *rh.get("efforts", [])]) else ""
    if easy:
        text = (f"Rhythm in Easy rowing at {easy['spm']:.0f} spm{estimated}: drive about {easy['drive_s']:.1f} s, "
                f"recovery {easy['recovery_s']:.1f} s, {_ratio(easy['ratio'])}{' (rough at this rate)' if _rough(easy) else ''}.")
        estimated = ""
        if easy["spm"] <= LOW_RATE_SPM:
            if easy["ratio"] >= NEAR_AIM:
                text += f" Close to the usual aim of {_ratio(AIM_RATIO)} at this rate."
            else:
                period = 60.0 / easy["spm"]
                drive = period / (1 + AIM_RATIO)
                text += (f" At {easy['spm']:.0f} spm a stroke takes {period:.1f} s, so the usual aim of "
                         f"{_ratio(AIM_RATIO)} means a drive of about {drive:.1f} s: a firmer, quicker push "
                         "from the catch, not only a slower slide.")
        out.append(text)
    efforts = rh.get("efforts") or []
    if efforts:
        if len(efforts) <= 4:
            parts = ", ".join(f"I{e['index']} {_ratio(e['ratio'])} at {e['spm']:.0f} spm "
                              f"(drive {e['drive_s']:.1f} s{', rough' if _rough(e) else ''})" for e in efforts)
        else:
            lo, hi = min(e["ratio"] for e in efforts), max(e["ratio"] for e in efforts)
            slo, shi = min(e["spm"] for e in efforts), max(e["spm"] for e in efforts)
            parts = (f"{_ratio(lo)} to {_ratio(hi)} at {slo:.0f}–{shi:.0f} spm, "
                     f"drive {min(e['drive_s'] for e in efforts):.1f}–{max(e['drive_s'] for e in efforts):.1f} s")
        out.append(f"Rhythm in efforts{estimated}: {parts}.")
    return out
