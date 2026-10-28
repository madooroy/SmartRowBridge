# SPDX-License-Identifier: GPL-3.0-only
"""Interval coach: finds harder efforts automatically and judges their force curves.

Fed one StrokeCurve per completed stroke (see protocol.SmartRowParser.on_stroke).

- Steady state: a rolling reference of your normal rowing (power, stroke length and
  the average force curve), learned from the last STEADY_WINDOW steady strokes.
- Interval detection: an effort starts when the median power of the last few strokes
  rises well above the steady reference, and ends when it falls back. Short bursts
  are ignored, and the strokes just after an effort (recovery) never teach the
  steady reference.
- Live cue, during an effort: compares the last few strokes' curve shape with your
  steady curve, and says what to do in two words, naming a part of the stroke, never a
  muscle: STRONGER FINISH ("finish") when less of the force comes in the back half of the
  drive than in steady rowing, EARLIER PUSH ("legs") when the force builds later in the
  drive, LONGER STROKES ("length") when strokes get clearly shorter, otherwise LOOKS GOOD.
  (The internal names are kept so saved sessions still read.)
- Summary, after each effort: its average curve, peak force, stroke length, finish
  share and where the force peaks (and how consistently), all against the steady
  reference. Also logged, so it lands in the session record. Summaries describe (a late
  push, a fading finish); only the live cues instruct.

The pulley measures the total force on the strap, and different causes look alike (an
early arm pull, for one, can pass for a strong push off the catch), so nothing here says
which muscles did what: a cue flags a change against your own steady rowing, and a video
of the effort shows what changed. (Thanks to Jaap van Ekris, Open Rowing Monitor.)

Stroke power is work x rate / 60 for that stroke. The pulley's own instantaneous
power lags a stroke behind, which would blur where an effort starts and ends.

The force samples are taken at fixed distances along the stroke (about every 5.8 cm of
handle travel), not at fixed times: across 2,952 real strokes the last sample with force
tracks stroke length (correlation 0.97: about sample 17 at 100 cm, 20 at 120 cm), not
stroke rate. So every shape measure works on the stroke's own drive, from the catch to
its last sample with force (drive_end): a shorter stroke then shows as LONGER STROKES
only, not also as a weaker finish. (An earlier version split every stroke at sample 12;
on a 45-minute session three efforts with strokes 2-3 % shorter then read as a -15 %
finish, when against their own length their finish was within 1 % of steady.)

Calibrated on six real sessions (2,952 strokes, 15 efforts):
- finish: the back-half share varies about 6.6 % from stroke to stroke; efforts ranged
  from -9 % to +4 % against steady. Thresholds: cue below -12 %, summary colours at
  -5 % and -12 %.
- push: the centre of force (as % of the drive) varies about 2 % from stroke to stroke;
  no effort's average moved more than 2.3 % from steady. The cue sits at 3 % later,
  untested on a real late stroke so far.
- peak spread: where each stroke's force peaks (the centre of the samples within 10 % of
  its maximum, so a flat top doesn't jump about), as % of the drive, and its standard
  deviation over the interval: 2-5 % in these sessions. Suggested by Jaap van Ekris, who
  found the peak's stroke-to-stroke variability telling in his own rowing.

Stroke length is judged as a percentage of the steady stroke, so tall and short rowers
are treated alike. CoachSettings holds what a rower may want to change (how much harder
an effort must be, the longest effort to count, and how readily the cues speak up);
its defaults are the calibrated values. These all came from one rower's sessions.
"""

from __future__ import annotations

import logging
import statistics
from collections import deque
from dataclasses import dataclass, field

from .protocol import FORCE_SAMPLES, StrokeCurve

log = logging.getLogger(__name__)

STEADY_WINDOW = 60  # strokes in the steady reference
STEADY_MIN = 30  # steady strokes needed before efforts are detected
POWER_RECENT = 30  # the steady power reference is the median of this many recent steady strokes,
#                    so it keeps up with a warm-up that ramps over the first minute or two
SMOOTH = 5  # median of this many strokes decides start and end
RECOVERY_STROKES = 12  # after an effort, not used for the steady reference
EASY_RATIO = 0.80  # strokes this far below steady (easy paddling) are ignored too
GAP_S = 10.0  # no stroke for this long ends an effort
NEW_SESSION_S = 300.0  # no stroke for this long starts over

CUE_WINDOW = 8  # strokes in the live cue
CUE_MIN = 6  # strokes into an effort before the first cue
DRIVE_END_FRACTION = 0.05  # the drive ends at the last sample above this share of the stroke's peak
GOOD_FINISH, OK_FINISH = 0.95, 0.88  # summary colours (fixed, so screens agree across settings)
PEAK_SPREAD_RATIO, PEAK_SPREAD_MIN = 1.5, 3.0  # peak spread flagged above both: x steady, and steady + this (% of drive)
SHORTER_CM = -2.0  # an interval's strokes this much shorter than steady are called out
SUMMARY_SHOW_S = 45.0  # how long the summary is shown after an effort


@dataclass(frozen=True)
class CueThresholds:
    finish_cue: float  # back-half share vs steady: cue below ...
    finish_clear: float  # ... clear above
    legs_cue: float  # centre of force later than steady, % of the drive
    legs_clear: float
    length_cue_pct: float  # strokes shorter than steady by this %
    length_clear_pct: float


SENSITIVITY = {
    "low": CueThresholds(0.84, 0.88, 4.0, 3.0, 3.5, 2.5),
    "normal": CueThresholds(0.88, 0.92, 3.0, 2.3, 2.6, 1.7),  # calibrated
    "high": CueThresholds(0.91, 0.94, 2.3, 1.8, 2.0, 1.3),
}


@dataclass(frozen=True)
class CoachSettings:
    start_pct: float = 20.0  # an effort is at least this % above steady ...
    start_min_w: float = 15.0  # ... and at least this many watts
    end_pct: float = 10.0  # it ends when power falls back below steady + this %
    min_effort_strokes: int = 8  # shorter is a burst, not an interval
    max_effort_s: float = 300.0  # longer is a new steady pace, not an interval
    sensitivity: str = "normal"  # how readily the cues speak up: low, normal or high

    def __post_init__(self) -> None:
        if self.sensitivity not in SENSITIVITY:
            raise ValueError(f"sensitivity must be one of {', '.join(SENSITIVITY)}, not {self.sensitivity!r}")

    @property
    def cues(self) -> CueThresholds:
        return SENSITIVITY[self.sensitivity]

    @property
    def start_ratio(self) -> float:
        return 1 + self.start_pct / 100

    @property
    def end_ratio(self) -> float:
        return 1 + self.end_pct / 100


DEFAULT_SETTINGS = CoachSettings()
# The calibrated values, under their old names (kept for the screens and tests).
FINISH_CUE = DEFAULT_SETTINGS.cues.finish_cue
LEGS_CUE = DEFAULT_SETTINGS.cues.legs_cue
MAX_EFFORT_S = DEFAULT_SETTINGS.max_effort_s


def drive_end(force: list[int] | list[float]) -> int:
    """The last sample of the drive: the last one above DRIVE_END_FRACTION of the peak.

    The samples are spaced by distance, so a longer stroke fills more of them; averaged
    curves trail off gradually, hence a fraction of the peak rather than any force at all.
    """
    top = max(force) if force else 0
    if top <= 0:
        return 0
    return max(i for i, f in enumerate(force) if f > DRIVE_END_FRACTION * top)


def back_share(force: list[int] | list[float]) -> float:
    """Fraction of the stroke's force in the back half of its own drive."""
    end = drive_end(force)
    total = sum(force[:end + 1])
    if not total:
        return 0.0
    mid = end / 2
    back = sum(f for i, f in enumerate(force[:end + 1]) if i > mid)
    if mid == int(mid):
        back += force[int(mid)] / 2  # the middle sample: half each side
    return back / total


def centre_of_force(force: list[int] | list[float]) -> float:
    """Where in the drive the force is centred, as % of the stroke's own drive (catch 0, end 100)."""
    end = drive_end(force)
    total = sum(force[:end + 1])
    if not total or not end:
        return 0.0
    return sum(i * f for i, f in enumerate(force[:end + 1])) / total / end * 100


def drive_lateness(curve: list[float], steady_curve: list[float]) -> float:
    """How much later than steady the force comes, in % of the drive (positive = later)."""
    return centre_of_force(curve) - centre_of_force(steady_curve)


def peak_position(force: list[int] | list[float]) -> float:
    """Where in the drive the force peaks, 0-100 % of that stroke's drive.

    The pulley samples force at fixed distances along the stroke (about every 5.8 cm), so a
    longer stroke fills more of the 24 samples; the drive ends at the last sample with force.
    The peak is the centre of the samples within 10 % of the maximum (a flat top doesn't jump).
    """
    top = max(force) if force else 0
    if top <= 0:
        return 0.0
    near = [i for i, f in enumerate(force) if f >= 0.9 * top]
    return sum(near) / len(near) / max(1, drive_end(force)) * 100


def peak_stats(strokes) -> tuple[float, float]:
    """Mean and standard deviation of the peak position (% of the drive) over strokes."""
    at = [peak_position(s.force_n) for s in strokes if s.force_n]
    if not at:
        return 0.0, 0.0
    return statistics.fmean(at), (statistics.pstdev(at) if len(at) > 1 else 0.0)


def balance(finish_ratio: float, lateness: float, cues: CueThresholds) -> float:
    """Where the force sits in the drive against steady, on one scale for the screens' shape bar.

    0 is your steady stroke; negative is towards the catch (the finish fading), positive
    towards the finish (the push coming late). In units of each cue's threshold, so +-1 is
    where STRONGER FINISH and EARLIER PUSH start, whichever measure moved further.
    """
    toward_catch = (1 - finish_ratio) / (1 - cues.finish_cue) if finish_ratio < 1 else 0.0
    toward_finish = lateness / cues.legs_cue if lateness > 0 else 0.0
    return toward_finish if toward_finish >= toward_catch else -toward_catch


def length_balance(length_delta_pct: float, cues: CueThresholds) -> float:
    """Stroke length against steady for the screens' length bar: -1 is where LONGER STROKES starts."""
    return length_delta_pct / cues.length_cue_pct


def stroke_power(s: StrokeCurve) -> float:
    return s.work_j * s.stroke_rate_spm / 60.0 if s.work_j and s.stroke_rate_spm else float(s.power_w)


def mean_curve(strokes) -> list[float]:
    strokes = list(strokes)
    if not strokes:
        return [0.0] * FORCE_SAMPLES
    return [sum(s.force_n[i] for s in strokes) / len(strokes) for i in range(FORCE_SAMPLES)]


@dataclass
class IntervalSummary:
    index: int
    duration_s: float
    strokes: int
    power_w: float
    stroke_rate_spm: float
    length_cm: float
    peak_n: float
    back_share: float
    steady_back_share: float
    steady_length_cm: float
    curve: list[float]
    steady_curve: list[float]
    first_third_back_share: float
    last_third_back_share: float
    ended_at: float = 0.0
    legs_threshold: float = LEGS_CUE
    cues: CueThresholds = DEFAULT_SETTINGS.cues  # for the screens' bars
    steady_power_w: float = 0.0
    started_at: float = 0.0  # time of its first stroke (same clock as the strokes)
    peak_at_pct: float = 0.0  # where the force peaks, % of the drive (mean over the strokes)
    peak_spread_pct: float = 0.0  # ... and how much that varies from stroke to stroke (std dev)
    steady_peak_at_pct: float = 0.0
    steady_peak_spread_pct: float = 0.0

    @property
    def peak_wanders(self) -> bool:
        """The peak landed clearly less consistently than in steady rowing."""
        return self.peak_spread_pct > max(self.steady_peak_spread_pct * PEAK_SPREAD_RATIO,
                                          self.steady_peak_spread_pct + PEAK_SPREAD_MIN)

    @property
    def lateness(self) -> float:
        return drive_lateness(self.curve, self.steady_curve)

    @property
    def legs_late(self) -> bool:
        return self.lateness > self.legs_threshold

    @property
    def finish_ratio(self) -> float:
        return self.back_share / self.steady_back_share if self.steady_back_share else 1.0

    @property
    def finish_pct(self) -> int:
        return round((self.finish_ratio - 1) * 100)

    @property
    def length_delta_cm(self) -> float:
        return self.length_cm - self.steady_length_cm

    @property
    def length_delta_pct(self) -> float:
        return self.length_delta_cm / self.steady_length_cm * 100 if self.steady_length_cm else 0.0

    @property
    def rating(self) -> str:
        r = self.finish_ratio
        return "good" if r >= GOOD_FINISH else "ok" if r >= OK_FINISH else "short"

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "duration_s": round(self.duration_s),
            "strokes": self.strokes,
            "power_w": round(self.power_w),
            "stroke_rate_spm": round(self.stroke_rate_spm, 1),
            "length_cm": round(self.length_cm, 1),
            "length_delta_cm": round(self.length_delta_cm, 1),
            "peak_n": round(self.peak_n),
            "finish_pct": self.finish_pct,
            "rating": self.rating,
            "steady_power_w": round(self.steady_power_w),
            "drive_late": round(self.lateness, 2),
            "legs_late": self.legs_late,
            "balance": round(balance(self.finish_ratio, self.lateness, self.cues), 2),
            "length_balance": round(length_balance(self.length_delta_pct, self.cues), 2),
            "peak_at_pct": round(self.peak_at_pct),
            "peak_spread_pct": round(self.peak_spread_pct, 1),
            "steady_peak_at_pct": round(self.steady_peak_at_pct),
            "steady_peak_spread_pct": round(self.steady_peak_spread_pct, 1),
            "peak_wanders": self.peak_wanders,
            "fade_pct": round((self.last_third_back_share / self.first_third_back_share - 1) * 100)
            if self.first_third_back_share else 0,
            "curve": [round(v) for v in self.curve],
            "steady_curve": [round(v) for v in self.steady_curve],
        }

    def log_line(self) -> str:
        m, s = divmod(round(self.duration_s), 60)
        return (f"Interval {self.index}: {m}:{s:02d}, {self.strokes} strokes, {self.power_w:.0f} W, "
                f"{self.stroke_rate_spm:.1f} spm, length {self.length_cm:.0f} cm ({self.length_delta_cm:+.0f}), "
                f"peak {self.peak_n:.0f} N at {self.peak_at_pct:.0f}% \u00b1{self.peak_spread_pct:.1f} "
                f"(steady {self.steady_peak_at_pct:.0f}% \u00b1{self.steady_peak_spread_pct:.1f}), "
                f"finish {self.finish_pct:+d}% vs steady ({self.rating})"
                + (f", late push ({self.lateness:+.1f}% of the drive)" if self.legs_late else ""))


@dataclass
class _Effort:
    strokes: list[StrokeCurve] = field(default_factory=list)
    cue: str | None = None


class IntervalCoach:
    def __init__(self, settings: CoachSettings = DEFAULT_SETTINGS) -> None:
        self.settings = settings
        self.session = 0
        self.reset()

    def reset(self) -> None:
        self.steady: deque[StrokeCurve] = deque(maxlen=STEADY_WINDOW)
        self.recent: deque[StrokeCurve] = deque(maxlen=SMOOTH)
        self.effort: _Effort | None = None
        self.recovery_left = 0
        self.intervals: list[IntervalSummary] = []
        self.last_time: float | None = None
        self.last_count = 0
        self.session += 1  # a new number every time the coach starts over
        self.first_time: float | None = None
        self.strokes_total = 0
        self.power_total = 0.0
        self.length_total = 0.0
        self.trace: list[tuple[float, int, float]] = []  # (time, heart rate, power) per stroke
        # Every stroke, for the session's activity file:
        # (time, distance m, power W, strokes/min, heart rate, stroke length cm)
        self.stroke_log: list[tuple[float, int, float, float, int, int]] = []

    # ------------------------------------------------------------ reference

    @property
    def ready(self) -> bool:
        return len(self.steady) >= STEADY_MIN

    def steady_power(self) -> float:
        recent = list(self.steady)[-POWER_RECENT:]
        return statistics.median(stroke_power(s) for s in recent) if recent else 0.0

    def steady_length(self) -> float:
        return statistics.fmean(s.stroke_length_cm for s in self.steady) if self.steady else 0.0

    def steady_back_share(self) -> float:
        return back_share(mean_curve(self.steady))

    # ------------------------------------------------------------ input

    def add(self, s: StrokeCurve) -> IntervalSummary | None:
        """Take one stroke. Returns the summary if this stroke ended an effort."""
        finished = None
        if self.last_time is not None:
            gap = s.time - self.last_time
            if gap > NEW_SESSION_S or s.stroke_count < self.last_count:
                self.reset()
            elif gap > GAP_S:
                finished = self._finish(self.last_time)
                self.recent.clear()
        self.last_time, self.last_count = s.time, s.stroke_count
        if self.first_time is None:
            self.first_time = s.time
        self.strokes_total += 1
        self.power_total += stroke_power(s)
        self.length_total += s.stroke_length_cm
        self.trace.append((s.time, s.heart_rate_bpm, stroke_power(s)))
        self.stroke_log.append((s.time, s.distance_m, stroke_power(s), s.stroke_rate_spm, s.heart_rate_bpm,
                                s.stroke_length_cm))
        self.recent.append(s)
        smoothed = statistics.median(stroke_power(r) for r in self.recent)

        if self.effort is None:
            base = self.steady_power()
            start_at = max(base * self.settings.start_ratio, base + self.settings.start_min_w)
            if self.ready and len(self.recent) >= 3 and smoothed >= start_at:
                # Back-date to the first hard stroke among the recent ones.
                recent = list(self.recent)
                first = next(i for i, r in enumerate(recent) if stroke_power(r) >= start_at)
                self.effort = _Effort(strokes=recent[first:])
                self._drop_from_steady(self.effort.strokes)
                log.debug("Effort started (%.0f W vs %.0f W steady)", smoothed, base)
            else:
                self._learn(s, base)
        else:
            self.effort.strokes.append(s)
            if s.time - self.effort.strokes[0].time > self.settings.max_effort_s:
                self._rebaseline()
            elif smoothed < self.steady_power() * self.settings.end_ratio:
                finished = self._finish(s.time) or finished
            else:
                self._update_cue()
        return finished

    def _learn(self, s: StrokeCurve, base: float) -> None:
        if self.recovery_left > 0:
            self.recovery_left -= 1
            return
        if self.ready and stroke_power(s) < base * EASY_RATIO:
            return
        self.steady.append(s)

    def _rebaseline(self) -> None:
        """Rowing has settled at a higher pace: make it the steady reference, no interval."""
        log.info("Steady pace moved up to about %.0f W; no interval recorded",
                 statistics.median(stroke_power(s) for s in self.effort.strokes))
        self.steady.extend(self.effort.strokes)
        self.effort = None

    def _drop_from_steady(self, strokes: list[StrokeCurve]) -> None:
        ids = {id(s) for s in strokes}
        kept = [s for s in self.steady if id(s) not in ids]
        self.steady.clear()
        self.steady.extend(kept)

    def _finish(self, now: float) -> IntervalSummary | None:
        effort, self.effort = self.effort, None
        if effort is None:
            return None
        end_at = self.steady_power() * self.settings.end_ratio
        strokes = list(effort.strokes)
        while strokes and stroke_power(strokes[-1]) < end_at:  # the easy strokes that ended it
            strokes.pop()
        self.recovery_left = RECOVERY_STROKES
        if len(strokes) < self.settings.min_effort_strokes:
            return None
        summary = self._summarise(strokes, now)
        self.intervals.append(summary)
        log.info(summary.log_line())
        return summary

    def _summarise(self, strokes: list[StrokeCurve], now: float) -> IntervalSummary:
        n = len(strokes)
        period = 60.0 / statistics.fmean(s.stroke_rate_spm for s in strokes)
        third = max(1, n // 3)
        peak_at, peak_spread = peak_stats(strokes)
        steady_at, steady_spread = peak_stats(self.steady)
        return IntervalSummary(
            index=len(self.intervals) + 1,
            duration_s=strokes[-1].time - strokes[0].time + period,
            strokes=n,
            power_w=statistics.fmean(stroke_power(s) for s in strokes),
            stroke_rate_spm=statistics.fmean(s.stroke_rate_spm for s in strokes),
            length_cm=statistics.fmean(s.stroke_length_cm for s in strokes),
            peak_n=statistics.fmean(max(s.force_n) for s in strokes),
            back_share=back_share(mean_curve(strokes)),
            steady_back_share=self.steady_back_share(),
            steady_length_cm=self.steady_length(),
            curve=mean_curve(strokes),
            steady_curve=mean_curve(self.steady),
            first_third_back_share=back_share(mean_curve(strokes[:third])),
            last_third_back_share=back_share(mean_curve(strokes[-third:])),
            ended_at=now,
            legs_threshold=self.settings.cues.legs_cue,
            cues=self.settings.cues,
            steady_power_w=self.steady_power(),
            started_at=strokes[0].time,
            peak_at_pct=peak_at,
            peak_spread_pct=peak_spread,
            steady_peak_at_pct=steady_at,
            steady_peak_spread_pct=steady_spread,
        )

    # ------------------------------------------------------------ live cue

    def _window(self) -> list[StrokeCurve]:
        return self.effort.strokes[-CUE_WINDOW:] if self.effort else []

    def _window_measures(self) -> tuple[float, float, float]:
        """The last few strokes against steady: finish ratio, lateness (% of drive), % shorter."""
        window = self._window()
        curve = mean_curve(window)
        ratio = back_share(curve) / (self.steady_back_share() or 1)
        steady_len = self.steady_length() or 1
        short_by_pct = (steady_len - statistics.fmean(s.stroke_length_cm for s in window)) / steady_len * 100
        return ratio, drive_lateness(curve, mean_curve(self.steady)), short_by_pct

    def _update_cue(self) -> None:
        e = self.effort
        if len(e.strokes) < CUE_MIN:
            e.cue = None
            return
        t = self.settings.cues
        ratio, late, short_by_pct = self._window_measures()
        if ratio < t.finish_cue or (e.cue == "finish" and ratio < t.finish_clear):
            e.cue = "finish"
        elif late > t.legs_cue or (e.cue == "legs" and late > t.legs_clear):
            e.cue = "legs"
        elif short_by_pct > t.length_cue_pct or (e.cue == "length" and short_by_pct > t.length_clear_pct):
            e.cue = "length"
        else:
            e.cue = "good"

    # ------------------------------------------------------------ session summary

    def session_summary(self) -> dict:
        """The whole session so far: totals, steady reference, every interval, and findings."""
        intervals = [i.as_dict() for i in self.intervals]
        for d, i in zip(intervals, self.intervals):  # where it sits in the session, for the activity file
            d["started_at_s"] = round(i.started_at - (self.first_time or i.started_at), 1)
        n = self.strokes_total
        hr = heart_rate_summary(self.trace, self.intervals, self.steady_power())
        if hr:
            for d, h in zip(intervals, hr.pop("intervals")):
                d.update(h)
        return {
            "strokes": n,
            "rowing_s": round((self.last_time or 0) - (self.first_time or 0)),
            "avg_power_w": round(self.power_total / n) if n else 0,
            "avg_length_cm": round(self.length_total / n, 1) if n else 0,
            "steady_power_w": round(self.steady_power()),
            "steady_length_cm": round(self.steady_length(), 1),
            "steady_curve": [round(v) for v in mean_curve(self.steady)],
            "settings": {"start_pct": self.settings.start_pct, "max_effort_min": self.settings.max_effort_s / 60,
                         "sensitivity": self.settings.sensitivity},
            "intervals": intervals,
            "heart_rate": hr,
            "findings": findings(intervals) + heart_rate_findings(hr, intervals),
        }

    # ------------------------------------------------------------ status

    def state(self, now: float) -> dict:
        """What the screen needs, for status.json."""
        if self.effort is not None and self.last_time is not None and now - self.last_time > GAP_S:
            self._finish(self.last_time)
        last = self.intervals[-1] if self.intervals else None
        out: dict = {
            "mode": "warmup" if not self.ready else "interval" if self.effort else "steady",
            "steady_power_w": round(self.steady_power()),
            "intervals": len(self.intervals),
            "last": last.as_dict() if last else None,
            "history": [{k: v for k, v in i.as_dict().items() if k not in ("curve", "steady_curve")}
                        for i in self.intervals[-20:]],
            "show_summary": bool(last and self.effort is None and now - last.ended_at < SUMMARY_SHOW_S),
        }
        if self.effort is not None:
            strokes = self.effort.strokes
            window = self._window()
            first = strokes[0]
            judged = len(strokes) >= CUE_MIN
            ratio, late, short_by_pct = self._window_measures() if judged else (1.0, 0.0, 0.0)
            out["live"] = {
                "index": len(self.intervals) + 1,
                "elapsed_s": round(max(0.0, now - first.time)),
                "strokes": len(strokes),
                "power_w": round(statistics.fmean(stroke_power(s) for s in strokes[-3:])),
                "length_cm": round(statistics.fmean(s.stroke_length_cm for s in window)),
                "cue": self.effort.cue,
                "finish_pct": round((back_share(mean_curve(window)) / (self.steady_back_share() or 1) - 1) * 100)
                if len(strokes) >= CUE_MIN else None,
                "length_delta_cm": round(statistics.fmean(s.stroke_length_cm for s in window) - self.steady_length()),
                # The screens' two bars (see balance()): None until there is enough to judge.
                "balance": round(balance(ratio, late, self.settings.cues), 2) if judged else None,
                "length_balance": round(length_balance(-short_by_pct, self.settings.cues), 2) if judged else None,
                "curve": [round(v) for v in mean_curve(window)],
                "steady_curve": [round(v) for v in mean_curve(self.steady)],
            }
        return out


def _pct(value: int) -> str:
    """+3%, −15% (a real minus sign, as on the screens)."""
    return f"{value:+d}%".replace("-", "\u2212")


def _clock(seconds: float) -> str:
    m, s = divmod(round(seconds), 60)
    return f"{m}:{s:02d}"


def _names(items: list[dict]) -> str:
    names = [f"I{i['index']}" for i in items]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def findings(intervals: list[dict]) -> list[str]:
    """A few plain sentences about the session's intervals (summary dicts, as in as_dict()).

    Only says what the numbers show clearly; nothing when there is nothing to say.
    """
    if not intervals:
        return ["No intervals: steady rowing only, or efforts too short or too close to the steady pace to count."]
    out: list[str] = []
    shorter = [i for i in intervals if i.get("length_delta_cm", 0) <= SHORTER_CM]
    good = [i for i in intervals if i["rating"] == "good" and not i.get("legs_late") and i not in shorter]
    faded = [i for i in intervals if i["rating"] == "short"]  # "short": the finish faded (an old name)
    ok = [i for i in intervals if i["rating"] == "ok"]
    n = len(intervals)

    if len(good) == n:
        out.append(f"All {n} intervals kept their shape and length: finish within 5% of steady.")
    elif good:
        out.append(f"{len(good)} of {n} kept their shape and length ({_names(good)}).")
    if faded:
        lo, hi = min(i["finish_pct"] for i in faded), max(i["finish_pct"] for i in faded)
        span = _pct(lo) if lo == hi else f"{_pct(hi)} to {_pct(lo)}"
        out.append(f"The finish faded in {_names(faded)}: {span} against steady.")
    if ok:
        pcts = ", ".join(_pct(i["finish_pct"]) for i in ok)
        out.append(f"The finish eased a little in {_names(ok)} ({pcts}).")
    if shorter:
        cm = sum(i["length_delta_cm"] for i in shorter) / len(shorter)
        out.append(f"Strokes were shorter than steady in {_names(shorter)} (about {abs(cm):.0f} cm).")

    # Did the longer efforts slip and the shorter ones hold?
    slipped = [i for i in intervals if i in faded or i in shorter]
    if len(slipped) >= 2 and len(good) >= 2:
        d_slip = sum(i["duration_s"] for i in slipped) / len(slipped)
        d_good = sum(i["duration_s"] for i in good) / len(good)
        if d_slip > d_good * 1.3:
            out.append(f"The ones that slipped were the longer efforts (about {_clock(d_slip)} against "
                       f"{_clock(d_good)}).")

    long_fades = [i for i in intervals if i["duration_s"] >= 90 and i.get("fade_pct", 0) <= -5]
    if long_fades:
        worst = min(long_fades, key=lambda i: i["fade_pct"])
        out.append(f"I{worst['index']} faded within itself: finish {_pct(worst['fade_pct'])} from its first third "
                   f"to its last.")

    legs = [i for i in intervals if i.get("legs_late")]
    if legs:
        out.append(f"The push came later in the drive than in steady rowing in {_names(legs)}. The curve can't say "
                   f"why (an early arm pull and a slow push off the catch look alike); a video of an effort can.")

    wander = [i for i in intervals if i.get("peak_wanders")]
    if wander:
        spreads = ", ".join(f"\u00b1{i['peak_spread_pct']:.0f}%" for i in wander)
        steady_spread = wander[0].get("steady_peak_spread_pct", 0)
        out.append(f"The force peak landed less consistently from stroke to stroke in {_names(wander)} "
                   f"({spreads} of the drive, against \u00b1{steady_spread:.0f}% in steady rowing).")

    steady = [i["steady_power_w"] for i in intervals if i.get("steady_power_w")]
    if len(steady) >= 2 and min(steady):
        change = (steady[-1] - steady[0]) / steady[0] * 100
        if abs(change) <= 5:
            out.append(f"Steady pace held at about {round(sum(steady) / len(steady))} W between efforts.")
        else:
            out.append(f"Steady pace went from {steady[0]} W to {steady[-1]} W over the session.")
    return out


# ---------------------------------------------------------------- heart rate (optional)

HR_PEAK_AFTER_S = 30.0  # heart rate peaks a little after an effort ends
HR_RECOVERY_S = 60.0  # recovery: the drop this long after the peak
HR_MIN_COVERAGE = 0.5  # share of strokes with a reading before heart rate is reported
DRIFT_MIN_POINTS = 40  # steady strokes needed in each half for the drift
DRIFT_SKIP_S = 600.0  # heart rate is still climbing during the warm-up: left out of the drift


def heart_rate_summary(trace: list[tuple[float, int, float]], intervals: list[IntervalSummary],
                       steady_power: float) -> dict | None:
    """Heart rate for the session and each interval, or None without a strap (or too few readings).

    Per interval: average during it, peak (up to HR_PEAK_AFTER_S after it), and the drop in the
    minute after the peak (left out if the next interval started first). For the session:
    average, maximum, and the drift of heart rate against power over the steady rowing
    (first half against second, after the warm-up; positive = more beats for the same watts later).
    """
    readings = [(t, hr, p) for t, hr, p in trace if hr > 0]
    if not trace or len(readings) < HR_MIN_COVERAGE * len(trace):
        return None
    windows = [(i.started_at, i.started_at + i.duration_s) for i in intervals]
    per_interval = []
    for k, (t0, t1) in enumerate(windows):
        during = [hr for t, hr, _ in readings if t0 <= t <= t1]
        around = [(hr, t) for t, hr, _ in readings if t0 <= t <= t1 + HR_PEAK_AFTER_S]
        entry: dict = {"hr_avg": round(sum(during) / len(during)) if during else None,
                       "hr_peak": max(around)[0] if around else None, "hr_recovery": None}
        if around:
            peak, t_peak = max(around)
            next_start = windows[k + 1][0] if k + 1 < len(windows) else float("inf")
            target = t_peak + HR_RECOVERY_S
            later = [(abs(t - target), hr) for t, hr, _ in readings if abs(t - target) <= 6]
            if later and target < next_start:
                entry["hr_recovery"] = peak - min(later)[1]
        per_interval.append(entry)

    # Steady rowing only: away from efforts and their recovery, not easy paddling.
    steady = [(t, hr, p) for t, hr, p in readings
              if p >= steady_power * EASY_RATIO
              and not any(t0 - 5 <= t <= t1 + 90 for t0, t1 in windows)]
    settled = [x for x in steady if x[0] >= trace[0][0] + DRIFT_SKIP_S]
    drift = None
    if len(settled) >= 2 * DRIFT_MIN_POINTS:
        half = len(settled) // 2

        def per_beat(points):
            return sum(p for _, _, p in points) / sum(hr for _, hr, _ in points)

        first, second = per_beat(settled[:half]), per_beat(settled[half:])
        drift = round((first - second) / first * 100, 1)
    hrs = [hr for _, hr, _ in readings]
    return {
        "avg": round(sum(hrs) / len(hrs)),
        "max": max(hrs),
        "steady_avg": round(sum(hr for _, hr, _ in steady) / len(steady)) if steady else None,
        "watts_per_beat": round(sum(p for *_, p in steady) / sum(hr for _, hr, _ in steady), 3) if steady else None,
        "drift_pct": drift,
        "intervals": per_interval,
    }


def heart_rate_findings(hr: dict | None, intervals: list[dict]) -> list[str]:
    if not hr:
        return []
    out = []
    peak_in = [i for i in intervals if i.get("hr_peak") == hr["max"]]
    where = f" (I{peak_in[0]['index']})" if peak_in else ""
    out.append(f"Heart rate averaged {hr['avg']} bpm and peaked at {hr['max']}{where}.")
    recoveries = [(i["hr_recovery"], i["index"]) for i in intervals if i.get("hr_recovery") is not None]
    if recoveries:
        mean = sum(r for r, _ in recoveries) / len(recoveries)
        best, k = max(recoveries)
        out.append(f"After the efforts it dropped {mean:.0f} bpm in the first minute on average "
                   f"(most: {best} after I{k}).")
    if hr.get("drift_pct") is not None:
        d = hr["drift_pct"]
        if d <= 5:
            out.append(f"Heart rate stayed in step with power through the steady rowing (drift {_pct(round(d))}).")
        else:
            out.append(f"Steady rowing took {d:.0f}% more heartbeats per watt in the second half than the first "
                       f"(under 5% is typical): fatigue, heat or not enough fuel.")
    return out
