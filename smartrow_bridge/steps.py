"""Two views of a whole session that the effort-by-effort coach doesn't give.

Ladders: a row of steady steps, each a set power above (or below) the one before (120, 140, 160 W...).
The coach finds efforts by intensity band, so a ladder's steps run together into a few efforts; here
each step is found by its own steady power, and the ladder says how the rower made the extra power
(stroke rate, force, drive time) and how heart rate followed.

W'bal: how much of W' (the work you can do above CP, your FTP here) is left, through the row, by
Skiba's differential model: above CP it drains by (P - CP) a second; below, it refills at a rate
that slows as it fills, (CP - P) x (W' - W'bal) / W'. Needs W' (from intervals.icu, or set).

Both work from the coach's stroke log: (time, distance, power, spm, heart rate, length, force, drive time).
"""

from __future__ import annotations

import statistics

from .coach import back_share, mean_curve, stall_at  # noqa: F401 - back_share kept for callers

BIN_S = 15.0  # power averaged over this, to find steps
STEP_TOL_PCT, STEP_TOL_W = 6.0, 8.0  # a bin this far from the step so far (both ways) starts a new step
STEP_MIN_S = 45.0  # shorter: not a step
LADDER_RISE_PCT = 5.0  # each step at least this much above (or below) the one before
LADDER_MIN_STEPS = 4
LADDER_EVEN = (0.6, 1.67)  # each step's rise, and its length, within this of the ladder's median: planned, not drift
LADDER_SPAN_PCT = 25.0  # first to last step at least this much apart (a warm-up creeping up is not a ladder)
LADDER_GAP_S = 30.0  # steps this far apart at most (a transition between them)
STOP_S = 10.0  # no stroke for this long: power 0 (for W'bal)


def _bins(log):
    t0 = log[0][0]
    bins: dict[int, list] = {}
    for row in log:
        bins.setdefault(int((row[0] - t0) // BIN_S), []).append(row)
    return t0, [(k, rows, statistics.fmean(r[2] for r in rows)) for k, rows in sorted(bins.items())]


def _segments(log):
    """Runs of bins at a steady power: [(first bin, last bin, [rows])]."""
    t0, bins = _bins(log)
    segs, cur = [], []
    for j, (k, rows, p) in enumerate(bins):
        if cur:
            m = statistics.fmean(b[2] for b in cur)
            tol = max(STEP_TOL_W, STEP_TOL_PCT / 100 * m)
            nxt = bins[j + 1] if j + 1 < len(bins) else None
            gap = k != cur[-1][0] + 1
            off = abs(p - m) > tol and (nxt is None or abs(nxt[2] - m) > tol)  # one odd bin: a blip
            if gap or off:
                segs.append(cur)
                cur = []
        cur.append((k, rows, p))
    if cur:
        segs.append(cur)
    out = []
    for seg in segs:
        if len(seg) * BIN_S < STEP_MIN_S:
            continue
        core = seg[1:] if len(seg) >= 4 else seg  # the first bin often holds the change from the last step
        out.append((seg[0][0], seg[-1][0], [r for b in core for r in b[1]]))
    return t0, out


def _step(t0, first, last, rows) -> dict:
    hr_end = [r[4] for r in rows if r[4] and r[0] - t0 >= (last + 1) * BIN_S - 15]
    drives = [r[7] for r in rows if len(r) > 7 and r[7]]
    forces = [r[6] for r in rows if r[6] and max(r[6]) > 0]
    return {"start_s": round(first * BIN_S), "end_s": round((last + 1) * BIN_S),
            "power_w": round(statistics.fmean(r[2] for r in rows), 1),
            "spm": round(statistics.fmean(r[3] for r in rows), 1),
            "hr_end": max(hr_end) if hr_end else None,
            "drive_s": round(statistics.fmean(drives), 3) if len(drives) == len(rows) else None,
            "peak_n": round(statistics.fmean(max(f) for f in forces)) if forces else None,
            "stall_pct": round(100 * sum(stall_at(list(f)) is not None for f in forces) / len(forces)) if forces else None,
            "curve": [round(v) for v in mean_curve(_Force(f) for f in forces)] if forces else []}


class _Force:  # mean_curve wants objects with .force_n
    __slots__ = ("force_n",)

    def __init__(self, f):
        self.force_n = list(f)


def find_ladders(log) -> list[dict]:
    """Ladders in the session: LADDER_MIN_STEPS or more steady steps in a row, each LADDER_RISE_PCT
    above (or all below) the one before. Each: its steps and what changed from the first to the last."""
    if len(log) < 20:
        return []
    t0, segs = _segments(log)
    steps = [_step(t0, a, b, rows) for a, b, rows in segs]
    ladders, run = [], []

    def close():
        best = _even(run)
        if best:
            ladders.append(_ladder(best))

    for st in steps:
        if run:
            prev = run[-1]
            up = st["power_w"] >= prev["power_w"] * (1 + LADDER_RISE_PCT / 100)
            down = st["power_w"] <= prev["power_w"] * (1 - LADDER_RISE_PCT / 100)
            way = None if len(run) < 2 else run[1]["power_w"] > run[0]["power_w"]
            near = st["start_s"] - prev["end_s"] <= LADDER_GAP_S
            if near and ((up and way in (None, True)) or (down and way in (None, False))):
                run.append(st)
                continue
            close()
            run = [prev] if near and (up or down) else []
        run.append(st)
    close()
    return ladders


def _even(run: list[dict]) -> list[dict] | None:
    """The longest stretch of a run of rising (or falling) steps that is evenly spaced: every rise and
    every step's length near the median (LADDER_EVEN), LADDER_MIN_STEPS or more, spanning LADDER_SPAN_PCT."""
    lo_f, hi_f = LADDER_EVEN
    for size in range(len(run), LADDER_MIN_STEPS - 1, -1):
        for i in range(len(run) - size + 1):
            part = run[i:i + size]
            rises = [abs(b["power_w"] - a["power_w"]) for a, b in zip(part, part[1:])]
            lengths = [x["end_s"] - x["start_s"] for x in part]
            mr, ml = statistics.median(rises), statistics.median(lengths)
            ends = sorted((part[0]["power_w"], part[-1]["power_w"]))
            if all(lo_f * mr <= r <= hi_f * mr for r in rises) and all(lo_f * ml <= x <= hi_f * ml for x in lengths) \
                    and ends[1] >= ends[0] * (1 + LADDER_SPAN_PCT / 100):
                return part
    return None


def _ladder(steps: list[dict]) -> dict:
    a, b = steps[0], steps[-1]
    pct = lambda x, y: round((y / x - 1) * 100) if x else None  # noqa: E731
    change = {"power_pct": pct(a["power_w"], b["power_w"]), "spm_pct": pct(a["spm"], b["spm"]),
              "peak_pct": pct(a["peak_n"], b["peak_n"]) if a["peak_n"] and b["peak_n"] else None,
              "drive_s": round(b["drive_s"] - a["drive_s"], 2) if a["drive_s"] and b["drive_s"] else None,
              "hr": [a["hr_end"], b["hr_end"]] if a["hr_end"] and b["hr_end"] else None}
    rate, force = change["spm_pct"] or 0, change["peak_pct"] or 0
    change["by"] = "rate" if rate > 1.5 * max(force, 0) else "force" if force > 1.5 * max(rate, 0) else "both"
    return {"start_s": a["start_s"], "end_s": b["end_s"], "up": b["power_w"] > a["power_w"],
            "steps": steps, "change": change}


def wbal(log, cp: float | None, w_prime: float | None, every_s: int = 5) -> dict | None:
    """W'bal through the session, every every_s seconds as % of W', and its lowest point."""
    if not log or not cp or not w_prime:
        return None
    t0, end = log[0][0], log[-1][0]
    w, low, low_t, series, j = float(w_prime), float(w_prime), 0, [], 0
    for sec in range(int(end - t0) + 1):
        t = t0 + sec
        while j + 1 < len(log) and log[j + 1][0] <= t:
            j += 1
        p = log[j][2] if t - log[j][0] < STOP_S else 0.0
        w = w - (p - cp) if p > cp else w + (cp - p) * (w_prime - w) / w_prime
        if w < low:
            low, low_t = w, sec
        if sec % every_s == 0:
            series.append([sec, round(100 * w / w_prime, 1)])
    return {"cp": round(cp), "w_prime": round(w_prime), "min_pct": round(100 * low / w_prime),
            "min_at_s": low_t, "end_pct": round(100 * w / w_prime), "series": series}


def _clock(seconds: float) -> str:
    m, s = divmod(round(seconds), 60)
    return f"{m}:{s:02d}"


def ladder_findings(ladders: list[dict]) -> list[str]:
    out = []
    for lad in ladders:
        st, c = lad["steps"], lad["change"]
        parts = [f"stroke rate {c['spm_pct']:+d}%"]
        if c["peak_pct"] is not None:
            parts.append(f"peak force {c['peak_pct']:+d}%")
        if c["drive_s"] is not None:
            parts.append(f"drive time {c['drive_s']:+.2f} s")
        if c["hr"]:
            parts.append(f"heart rate {c['hr'][0]}→{c['hr'][1]}")
        how = {"rate": "mostly from stroke rate", "force": "mostly from force",
               "both": "from rate and force alike"}[c["by"]]
        out.append((f"A ladder of {len(st)} steps, {round(st[0]['power_w'])}→{round(st[-1]['power_w'])} W "
                    f"({_clock(lad['start_s'])}–{_clock(lad['end_s'])}): " + ", ".join(parts)
                    + (f"; the extra power came {how}." if lad["up"] else ".")).replace("-", "−"))
    return out


def wbal_findings(w: dict | None) -> list[str]:
    if not w:
        return []
    kj = w["w_prime"] / 1000
    if w["min_pct"] >= 95:
        return [f"W′bal stayed full (above CP {w['cp']} W hardly at all; W′ {kj:.1f} kJ)."]
    return [f"W′bal (CP {w['cp']} W, W′ {kj:.1f} kJ) fell to {w['min_pct']}% at {_clock(w['min_at_s'])} "
            f"and ended at {w['end_pct']}%."]
