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
  steady curve. "finish" when too little of the force comes in the back half of the
  drive (a short, leg-only stroke), "legs" when the force comes late in the drive
  (a weak push off the catch, pulling with back and arms), "length" when strokes get
  clearly shorter, otherwise "good".
- Summary, after each effort: its average curve, peak force, stroke length and finish
  share against the steady reference. Also logged, so it lands in the session record.

Stroke power is work x rate / 60 for that stroke. The pulley's own instantaneous
power lags a stroke behind, which would blur where an effort starts and ends.

Calibrated on a 45-minute session with seven efforts at ~150 W over ~111 W steady:
the back-half share of the curve was 0.29 steady, 0.29-0.35 in the efforts that kept
their shape and 0.23-0.24 in the ones that went short and leg-heavy, with about 0.02
stroke-to-stroke scatter.

The "legs" cue measures where the curve's centre of force sits (in samples, 0-23)
against the steady curve, minus what a longer stroke explains by itself (LATE_PER_PCT
per % of extra length, longer strokes only): longer strokes push the centre later
without any loss of leg drive.
Over 15 real intervals the length-corrected shift never exceeded +0.34 samples; a
weak leg drive (peak force moving from about sample 8 to 11) shifts it by 1.5 or
more. The cue threshold sits between, untested on a real weak-leg stroke so far.

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
LATE_PER_PCT = 0.115  # centre of force moves this much later per % of extra stroke length
#                       (0.1 sample per cm at ~115 cm, from real sessions)
GOOD_FINISH, OK_FINISH = 0.95, 0.88  # summary colours (fixed, so screens agree across settings)
SUMMARY_SHOW_S = 45.0  # how long the summary is shown after an effort


@dataclass(frozen=True)
class CueThresholds:
    finish_cue: float  # back-half share vs steady: cue below ...
    finish_clear: float  # ... clear above
    legs_cue: float  # centre of force later than steady, samples (length-corrected)
    legs_clear: float
    length_cue_pct: float  # strokes shorter than steady by this %
    length_clear_pct: float


SENSITIVITY = {
    "low": CueThresholds(0.84, 0.88, 0.80, 0.60, 3.5, 2.5),
    "normal": CueThresholds(0.88, 0.92, 0.60, 0.45, 2.6, 1.7),  # calibrated
    "high": CueThresholds(0.91, 0.94, 0.45, 0.35, 2.0, 1.3),
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


def back_share(force: list[int] | list[float]) -> float:
    """Fraction of the stroke's force in the back half of the drive (samples 12-23)."""
    total = sum(force)
    return sum(force[FORCE_SAMPLES // 2:]) / total if total else 0.0


def centre_of_force(force: list[int] | list[float]) -> float:
    """Where in the drive the force is centred, in samples from the catch (0-23)."""
    total = sum(force)
    return sum(i * f for i, f in enumerate(force)) / total if total else 0.0


def drive_lateness(curve: list[float], steady_curve: list[float], length_delta_pct: float) -> float:
    """How much later than steady the force comes, beyond what the stroke length explains.

    Only extra length is credited: a longer stroke moves the centre later by itself, but
    a shorter one is never taken as a reason for the force to come late.
    """
    return centre_of_force(curve) - centre_of_force(steady_curve) - LATE_PER_PCT * max(0.0, length_delta_pct)


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

    @property
    def lateness(self) -> float:
        return drive_lateness(self.curve, self.steady_curve, self.length_delta_pct)

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
            "drive_late": round(self.lateness, 2),
            "legs_late": self.legs_late,
            "fade_pct": round((self.last_third_back_share / self.first_third_back_share - 1) * 100)
            if self.first_third_back_share else 0,
            "curve": [round(v) for v in self.curve],
            "steady_curve": [round(v) for v in self.steady_curve],
        }

    def log_line(self) -> str:
        m, s = divmod(round(self.duration_s), 60)
        return (f"Interval {self.index}: {m}:{s:02d}, {self.strokes} strokes, {self.power_w:.0f} W, "
                f"{self.stroke_rate_spm:.1f} spm, length {self.length_cm:.0f} cm ({self.length_delta_cm:+.0f}), "
                f"peak {self.peak_n:.0f} N, finish {self.finish_pct:+d}% vs steady ({self.rating})"
                + (f", leg drive late ({self.lateness:+.1f})" if self.legs_late else ""))


@dataclass
class _Effort:
    strokes: list[StrokeCurve] = field(default_factory=list)
    cue: str | None = None


class IntervalCoach:
    def __init__(self, settings: CoachSettings = DEFAULT_SETTINGS) -> None:
        self.settings = settings
        self.reset()

    def reset(self) -> None:
        self.steady: deque[StrokeCurve] = deque(maxlen=STEADY_WINDOW)
        self.recent: deque[StrokeCurve] = deque(maxlen=SMOOTH)
        self.effort: _Effort | None = None
        self.recovery_left = 0
        self.intervals: list[IntervalSummary] = []
        self.last_time: float | None = None
        self.last_count = 0

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
        )

    # ------------------------------------------------------------ live cue

    def _window(self) -> list[StrokeCurve]:
        return self.effort.strokes[-CUE_WINDOW:] if self.effort else []

    def _update_cue(self) -> None:
        e = self.effort
        if len(e.strokes) < CUE_MIN:
            e.cue = None
            return
        t = self.settings.cues
        window = self._window()
        curve = mean_curve(window)
        ratio = back_share(curve) / (self.steady_back_share() or 1)
        steady_len = self.steady_length() or 1
        short_by_pct = (steady_len - statistics.fmean(s.stroke_length_cm for s in window)) / steady_len * 100
        late = drive_lateness(curve, mean_curve(self.steady), -short_by_pct)
        if ratio < t.finish_cue or (e.cue == "finish" and ratio < t.finish_clear):
            e.cue = "finish"
        elif late > t.legs_cue or (e.cue == "legs" and late > t.legs_clear):
            e.cue = "legs"
        elif short_by_pct > t.length_cue_pct or (e.cue == "length" and short_by_pct > t.length_clear_pct):
            e.cue = "length"
        else:
            e.cue = "good"

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
                "curve": [round(v) for v in mean_curve(window)],
                "steady_curve": [round(v) for v in mean_curve(self.steady)],
            }
        return out
