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

With the seat sensor (from 10 Nov 2026) the drive and recovery are measured, not estimated: the
drive from the seat leaving the front to the flag going off, the recovery from there to the next
catch, and each is split into its parts (phases.py). That is used whenever enough strokes have it;
the pulley estimate is only the fallback for a row without the sensor.

Works from the coach's stroke log: (time, distance, power, spm, heart rate, length, force,
drive time, flag drive, flag recovery).
"""

from __future__ import annotations

import statistics

from . import phases

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


def stroke_rhythm(row: tuple, prev: tuple | None = None) -> tuple[float, float, str] | None:
    """(drive s, recovery s, source) for one stroke-log row (prev: the row before it), or None when it
    has no usable drive time. Source "seat" (measured, phases.py), "handle" or "pulley" (estimated)."""
    d = phases.durations(prev, row)
    if d and d["recovery"] and d["drive"] > 0:
        return d["drive"], d["recovery"], "seat"
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


def with_prev(log: list[tuple]) -> list[tuple[tuple | None, tuple]]:
    """Each stroke-log row with the row before it (the seat's recovery needs the previous finish)."""
    return [(log[i - 1] if i else None, r) for i, r in enumerate(log)]


def _measured(pairs: list[tuple[tuple | None, tuple]]) -> list[tuple[tuple, tuple[float, float, str]]]:
    """(row, rhythm) for the strokes with one, from a single source: the seat's when enough strokes
    have it (a row is never part measured, part estimated)."""
    got = [(r, x) for p, r in pairs if (x := stroke_rhythm(r, p))]
    seat = [(r, x) for r, x in got if x[2] == "seat"]
    return seat if len(seat) >= MIN_STROKES else [(r, x) for r, x in got if x[2] != "seat"] or got


def summarise(pairs: list[tuple[tuple | None, tuple]]) -> dict | None:
    """Medians over the strokes that have a drive time; None when too few do. pairs: (row before, row),
    as with_prev gives them."""
    got = _measured(pairs)
    if len(got) < MIN_STROKES:
        return None
    drive = statistics.median(x[0] for _, x in got)
    recovery = statistics.median(x[1] for _, x in got)
    out = {"strokes": len(got), "spm": round(statistics.median(r[3] for r, _ in got), 1),
           "drive_s": round(drive, 2), "recovery_s": round(recovery, 2),
           "ratio": round(recovery / drive, 2), "source": got[0][1][2]}
    if out["source"] == "seat":
        prev_of = {id(r): p for p, r in pairs}
        out["phases"] = phases.medians([phases.durations(prev_of.get(id(r)), r) for r, _ in got])
    return out


LIVE_STROKES = 5  # the screen's bar: median of the last few strokes (single strokes are only good to ~0.1 s)
LIVE_MIN = 3  # of them with a drive time
LIVE_STALE_S = 8.0  # no stroke for this long: no bar


def live(log: list[tuple], now: float) -> dict | None:
    """The Easy screen's rhythm bar: drive and recovery over the last few strokes, at low rates only
    (where 1:2 is the aim); None otherwise."""
    if not log or now - log[-1][0] > LIVE_STALE_S:
        return None
    pairs = with_prev(log[-(LIVE_STROKES + 1):])[1:]
    got = [(r, x) for p, r in pairs if (x := stroke_rhythm(r, p))]
    seat = [(r, x) for r, x in got if x[2] == "seat"]
    got = seat if len(seat) >= LIVE_MIN else [(r, x) for r, x in got if x[2] != "seat"]  # never mixed
    if len(got) < LIVE_MIN:
        return None
    drive = statistics.median(x[0] for _, x in got)
    recovery = statistics.median(x[1] for _, x in got)
    spm = 60.0 / (drive + recovery)
    if spm > LOW_RATE_SPM:
        return None
    out = {"drive_s": round(drive, 2), "recovery_s": round(recovery, 2), "ratio": round(recovery / drive, 2),
           "aim": AIM_RATIO, "source": got[0][1][2]}
    if out["source"] == "seat":  # the parts, for the screens' bar (each a median of the same strokes)
        prev_of = {id(r): p for p, r in pairs}
        out["phases"] = phases.medians([phases.durations(prev_of.get(id(r)), r) for r, _ in got])
    return out


def session_rhythm(log: list[tuple], intervals: list[dict], last: dict | None = None) -> dict | None:
    """Rhythm of the Easy rowing and of each effort. intervals: the session's efforts (as_dict with
    started_at_s, seconds after the first stroke). last: the previous session's Easy rhythm
    (sessions.latest_fresh_easy "rhythm_last"), kept for the comparison in the findings."""
    if not log:
        return None
    t0 = log[0][0]
    pairs = with_prev(log)
    spans = [(i["started_at_s"], i["started_at_s"] + i["duration_s"]) for i in intervals if "started_at_s" in i]
    easy = [(p, r) for p, r in pairs if r[0] - t0 >= WARMUP_S
            and not any(a - 5 <= r[0] - t0 <= b + AFTER_EFFORT_S for a, b in spans)]
    efforts = []
    for i, (a, b) in zip((i for i in intervals if "started_at_s" in i), spans):
        s = summarise([(p, r) for p, r in pairs if a <= r[0] - t0 <= b])
        if s:
            efforts.append({"index": i["index"], **s})
    out = {"easy": summarise(easy), "efforts": efforts}
    if out["easy"] and out["easy"]["source"] == "seat" and len(easy) >= 3 * MIN_STROKES:
        n = len(easy)  # Easy rowing in thirds of the row: what fades or drifts as it goes on
        thirds = [summarise(easy[k * n // 3:(k + 1) * n // 3]) for k in range(3)]
        if all(x and x["source"] == "seat" for x in thirds):
            out["easy"]["thirds"] = [{"drive_s": x["drive_s"], "recovery_s": x["recovery_s"], "ratio": x["ratio"],
                                      "spm": x["spm"], "phases": x["phases"]} for x in thirds]
    if last and out["easy"] and out["easy"]["source"] == "seat" and last.get("source") == "seat":
        out["last"] = {k: last[k] for k in ("session", "drive_s", "recovery_s", "ratio", "spm", "phases") if k in last}
    return out if out["easy"] or efforts else None


def _rough(x: dict) -> bool:
    return x.get("source") == "pulley" and x["spm"] > ROUGH_ABOVE_SPM


def _ratio(x: float) -> str:
    return f"1:{x:.0f}" if x == round(x) else f"1:{x:.1f}"


PART_NAMES = {"load": "catch to load", "legs": "legs", "body": "body and arms", "drive": "drive",
              "parked": "parked after the finish", "slide": "slide", "recovery": "recovery"}
CHANGE_S = 0.1  # a part that moves this much from the first third of the row to the last is mentioned
LAST_CHANGE_S = 0.05  # ... or since the last row (medians of hundreds of strokes: steady to about 0.01 s)
SAME_RATE_SPM = 1.0  # parts are only compared at rates this close: at 20 spm, 1 spm is 0.15 s of stroke
EARLY_LOAD_S = 0.0  # the handle loaded before the seat moved (median): arms or back first. flag_on_t is the
#                    first record with the flag, on average ~0.05 s after the change, so this is already
#                    ~0.05 s of real lead


def phase_findings(easy: dict, last: dict | None = None) -> list[str]:
    """The parts of the Easy stroke (seat sensor), how they moved over the row, and since the last row."""
    ph = easy.get("phases") or {}
    if not ph.get("drive"):
        return []
    f = phases.fmt
    load = (f"the handle taking load {f(ph['load'])} s into it" if ph["load"] is not None and ph["load"] > EARLY_LOAD_S
            else "the handle loaded before the seat moved" if ph["load"] is not None else "")
    text = (f"Stroke parts (Easy): legs {f(ph['legs'])} s{' (' + load + ')' if load else ''}, "
            f"body and arms {f(ph['body'])} s")
    if ph.get("parked") is not None:
        text += (f"; then parked {f(ph['parked'])} s after the finish (hands away and body over before the knees "
                 f"rise) and the slide {f(ph['slide'])} s")
    out = [text + "."]
    if ph["load"] is not None and ph["load"] <= EARLY_LOAD_S:
        out.append("The handle took load before the seat moved: arms or back first. Push with the legs first.")
    thirds = easy.get("thirds")
    if thirds and abs(thirds[-1]["spm"] - thirds[0]["spm"]) > SAME_RATE_SPM:
        out.append(f"Over the row the rate went from {thirds[0]['spm']:.0f} to {thirds[-1]['spm']:.0f} spm, "
                   "so its parts aren't compared.")
    elif thirds:
        moved = []
        for k in ("drive", "recovery", "load", "legs", "body", "parked", "slide"):
            a, b = thirds[0]["phases"].get(k), thirds[-1]["phases"].get(k)
            if a is not None and b is not None and abs(b - a) >= CHANGE_S:
                moved.append(f"{PART_NAMES[k]} {a:.2f} → {b:.2f} s")
        if moved:
            out.append("Over the row (first third to last): " + ", ".join(moved) + ".")
    if last and last.get("phases") and last.get("spm") and abs(last["spm"] - easy["spm"]) > SAME_RATE_SPM:
        out.append(f"Since the last row: rate {last['spm']:.0f} \u2192 {easy['spm']:.0f} spm in Easy rowing, so the "
                   "parts aren't compared.")
    elif last and last.get("phases"):
        since = []
        for k in ("drive", "recovery", "load", "legs", "body", "parked", "slide"):
            a, b = last["phases"].get(k), ph.get(k)
            if a is not None and b is not None and abs(b - a) >= LAST_CHANGE_S:
                since.append(f"{PART_NAMES[k]} {a:.2f} → {b:.2f} s")
        if last.get("ratio") and abs(easy["ratio"] - last["ratio"]) >= 0.1:
            since.insert(0, f"ratio {_ratio(round(last['ratio'], 1))} → {_ratio(round(easy['ratio'], 1))}")
        if since:
            out.append("Since the last row (Easy): " + ", ".join(since) + ".")
        else:
            out.append("Stroke parts as in the last row.")
    return out


def rhythm_findings(rh: dict | None) -> list[str]:
    """The Easy rhythm (with the aim, at low rates), its parts with the seat sensor, and the efforts'."""
    if not rh:
        return []
    out = []
    easy = rh.get("easy")
    def label(src: set) -> str:
        return (" (drive estimated from the pulley)" if "pulley" in src
                else " (timed with the seat sensor)" if "seat" in src else "")

    # each line says how its own strokes were timed (the Easy rowing's, else the efforts')
    estimated = label({easy.get("source")} if easy else {x.get("source") for x in rh.get("efforts", [])})
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
        if easy.get("source") == "seat":
            out.extend(phase_findings(easy, rh.get("last")))
        effort_src = {x.get("source") for x in rh.get("efforts", [])}
        if effort_src and effort_src != {easy.get("source")}:
            estimated = label(effort_src)
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
