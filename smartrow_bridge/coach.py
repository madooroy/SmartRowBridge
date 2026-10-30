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
  drive, LONGER STROKES ("length") when strokes get clearly shorter, BETTER TRANSITION
  ("transition") when the force stalls just after the peak on several of the last few strokes
  (see stall_at), otherwise LOOKS GOOD.
  (The internal names are kept so saved sessions still read.)
- Summary, after each effort: its average curve, peak force, stroke length, finish
  share and where the force peaks (and how consistently), and how even its strokes were
  (consistency), all against the Easy rowing just before it (the last LOCAL_EASY Easy
  strokes). Not against the fresh Easy stroke from the start of the row: your Easy stroke
  itself changes over a row (Oct 2026: its finish down 3-16 % from the first minutes to the
  last), and against the early stroke an effort late in the row read as a -15 % finish when
  it held the shape you were rowing at the time (-2 to -4 %). That change is reported on its
  own (easy_drift), and the fresh stroke still coaches the Easy rowing. Also logged, so it lands in the session record. Summaries describe (a late
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
- transition: a stall just after the peak, where the force stops falling for a sample or more and
  then drops again (stall_at). Spotted by Jaap van Ekris on a chart of one effort's strokes; none of
  the single-number measures moved, and averaged curves smooth it away, so it is counted stroke
  by stroke. Over five sessions it sat at the same place every day (about 70 cm of handle travel,
  85-90 % of peak force): on 3-6 % of Easy strokes, and up to 46 % of an effort's (Oct 29, I5).
  Coaching sources put dips like this at a handover (legs to back, or back to arms) and advise
  letting the phases overlap. The pulley reports force in 16 N steps, which is coarse here: the drops
  around a stall are two or three steps. Checked against each stroke's own smooth fit (a degree-4
  polynomial, rounded to 16 N): in efforts that finds 0-4 % stalls against 9-46 % in the real strokes,
  so those are real; in Easy rowing (less force, so coarser steps) 7 % against 6 % real, rounding
  only. So BETTER TRANSITION is an effort cue; Easy rowing gets none.
- consistency (also after Jaap van Ekris: smoothness and consistency are what matter): how
  much each stroke's curve (scaled to its own peak) differs from the one before (RMS over the
  drive), against the same in the Easy rowing before the effort, plus the spread of stroke
  length and any clearly short strokes. Over five sessions (21 efforts) efforts ran 0.72-1.20x
  their Easy rowing; the two flagged (1.68x and 1.90x, length spread 15 and 12 cm against 2-4)
  each had a couple of strokes cut 30-40 cm short and uneven power. A deliberately eased finish
  (Oct 29, I3) did not show in the finish measure against the Easy rowing before it (-3 %, as
  an ordinary Moderate effort that day): with 24 samples a stroke can change in ways the curve
  can't separate from normal effort-to-effort variation.

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
LEAD_GAP_S, LEAD_STROKES = 30.0, 10  # a few pulls, then a pause this long: the row starts after it

# The fresh Easy stroke: Easy strokes from FRESH_FROM_S to FRESH_TO_S into the row (after the
# warm-up, whose strokes are shorter, before fatigue; efforts, Recovery and the strokes just after
# an effort left out). Saved with the session; the next session (within FRESH_MAX_AGE_DAYS) judges
# its Easy rowing against it from the start. Stroke length is calibrated at FRESH_TO_S: today's
# becomes the reference unless it is more than LENGTH_SAME_CM shorter than the saved one; then the
# saved one stays (so the cue speaks up) and the summary says so.
FRESH_FROM_S, FRESH_TO_S = 120.0, 480.0
FRESH_MIN = 60  # strokes needed for a fresh Easy stroke
PROVISIONAL_MIN = 30  # with nothing saved: fresh strokes before a provisional reference
FRESH_MAX_AGE_DAYS = 30
LENGTH_SAME_CM = 3.0

# Your stroke from Challenging up is shaped differently from your Easy one, and that is not a fading
# finish: the drive comes harder off the catch and the peak earlier, so less of the force falls in
# the back half (Oct 2026 rows: Moderate about the same as Easy, Challenging about 10 % less, Max
# about 15 % less). An effort's finish is judged against this share of the Easy finish; learned from
# your saved rows once there are EXPECT_MIN efforts at an intensity (sessions.latest_fresh_easy).
EXPECTED_FINISH = {"Moderate": 1.0, "Challenging": 0.90, "Max": 0.85}
EXPECT_RANGE = (0.80, 1.05)
EXPECT_MIN = 3
SHAPE_MIN = 20  # Easy strokes after the warm-up needed before efforts are judged against them alone
LOCAL_EASY = 40  # efforts are judged against this many of the Easy strokes just before them
# Consistency (see the module notes): an effort is uneven when its strokes vary this much more than
# the Easy rowing before it, or its stroke length spreads this much more (and by at least so many cm).
UNEVEN_VARIATION = 1.4
UNEVEN_LENGTH_SD_RATIO, UNEVEN_LENGTH_SD_MIN = 2.0, 6.0
VARIATION_FLOOR = 0.02  # the least Easy-rowing variation an effort's is divided by
SHORT_STROKE_PCT = 15.0  # a stroke this much shorter than the Easy ones is counted as cut short
SETTLE_STROKES = 3  # an effort's first strokes (the step up) are left out of its consistency
# Transition (stall_at): a drop per sample this small against the steepest drops after the peak is
# a stall, if a drop at least STALL_STEEP of them comes before and after it.
STALL_FLAT, STALL_STEEP = 0.25, 0.5
SAMPLE_CM = 5.8  # handle travel between force samples (about; they are spaced by distance)
STALL_FLAG_PCT, STALL_FLAG_OVER = 25.0, 15.0  # summary: stalls on this % of strokes, and this many points over Easy
SPLIT_MIN_STROKES = 15  # an effort that steps up (or down) an intensity for this long is split in two

CUE_WINDOW = 8  # strokes in the live cue
CUE_MIN = 6  # strokes into an effort before the first cue
DRIVE_END_FRACTION = 0.05  # the drive ends at the last sample above this share of the stroke's peak
GOOD_FINISH, OK_FINISH = 0.95, 0.88  # summary colours (fixed, so screens agree across settings)
PEAK_SPREAD_RATIO, PEAK_SPREAD_MIN = 1.5, 3.0  # peak spread flagged above both: x steady, and steady + this (% of drive)
# Intensity labels, as Peloton's row classes name them. Peloton's targets sit at fixed shares of
# the Challenging mid target at every level (levels 1-10 within about 1 %): Easy 44-72 % (mid 55),
# Moderate 72-88 (mid 79), Challenging 88-116 (mid 100), Max 116-138 (mid 126); below Easy is
# Recovery. With the FTP as the Challenging mid (as the rower's Peloton targets table sets it), an
# interval is labelled by its average power with boundaries half-way between the mid targets, so a
# slightly light effort still gets the name it was aimed at. Without an FTP the same boundaries are
# taken relative to Easy rowing (the steady reference, Easy mid = 55 %).
INTENSITIES = ("Recovery", "Easy", "Moderate", "Challenging", "Max")
FTP_BOUNDS_PCT = (44.0, 67.0, 89.0, 113.0)
EASY_BOUNDS = tuple(round(b / 55.0, 2) for b in FTP_BOUNDS_PCT)  # 0.80, 1.22, 1.62, 2.05


def intensity_bands(ftp: float | None, easy_w: float | None) -> list[tuple[str, int | None, int | None]]:
    """(label, from W, to W) for each intensity: from the FTP (Challenging's middle) or Easy power."""
    if ftp:
        cuts = [round(ftp * b / 100) for b in FTP_BOUNDS_PCT]
    elif easy_w:
        cuts = [round(easy_w * b) for b in EASY_BOUNDS]
    else:
        return []
    return list(zip(INTENSITIES, [None, *cuts], [*cuts, None]))


def bands_text(ftp: float | None, easy_w: float | None) -> str:
    """'Recovery under 81 W, Easy 81-124 W, ..., Max over 209 W' (empty without either)."""
    parts = []
    for label, lo, hi in intensity_bands(ftp, easy_w):
        parts.append(f"{label} under {hi} W" if lo is None else f"{label} over {lo} W" if hi is None
                     else f"{label} {lo}\u2013{hi} W")
    return ", ".join(parts)
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
    transition_cue: int = 3  # strokes with a stall after the peak, of the last CUE_WINDOW: cue at ...
    transition_clear: int = 1  # ... clear at or below


SENSITIVITY = {
    "low": CueThresholds(0.84, 0.88, 4.0, 3.0, 3.5, 2.5, 4, 2),
    "normal": CueThresholds(0.88, 0.92, 3.0, 2.3, 2.6, 1.7, 3, 1),  # calibrated
    "high": CueThresholds(0.91, 0.94, 2.3, 1.8, 2.0, 1.3, 2, 0),
}


@dataclass(frozen=True)
class CoachSettings:
    start_pct: float = 20.0  # an effort is at least this % above steady ...
    start_min_w: float = 15.0  # ... and at least this many watts
    end_pct: float = 10.0  # it ends when power falls back below steady + this %
    min_effort_strokes: int = 8  # shorter is a burst, not an interval
    max_effort_s: float = 300.0  # longer is a new steady pace, not an interval (without an FTP)
    sensitivity: str = "normal"  # how readily the cues speak up: low, normal or high
    # With your FTP known (IntervalCoach.ftp): an effort is Moderate and above (this % of it), so
    # a warm-up ramping into Easy rowing can't pass for one, and an effort lasts as long as you
    # hold it: no time limit (max_effort_s applies only without an FTP).
    ftp_floor_pct: float = FTP_BOUNDS_PCT[1]

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


def stall_span(force: list[int] | list[float]) -> tuple[int, int] | None:
    """The stall just after the peak, as (first sample, sample where the fall resumes), or None.

    After the peak the force should fall ever faster to the end of the drive. A stall is a stretch
    where it falls by under STALL_FLAT of the steepest drops (or not at all), with a drop of at least
    STALL_STEEP of them before and after it: the curve flattens, then falls again. Mostly it is a
    shoulder (the fall slows for two or three samples) rather than a flat stop: requiring a flat
    stop two force steps below the drops around it leaves 0-10 % of effort strokes (Oct 29, I5: 8 %
    against 46 %), while a smooth fit of each stroke rounded to 16 N shows the shoulder 0-9 %.
    """
    top = max(force) if force else 0
    if top <= 0:
        return None
    at = max(i for i, f in enumerate(force) if f == top)  # the last sample of a flat top
    drops = [force[i] - force[i + 1] for i in range(at, drive_end(force))]
    if len(drops) < 5:
        return None
    steep = statistics.fmean(sorted(drops)[-3:])
    i = 1
    while i < len(drops) - 1:
        if drops[i] <= STALL_FLAT * steep and max(drops[:i]) >= STALL_STEEP * steep:
            j = i
            while j + 1 < len(drops) and drops[j + 1] <= STALL_FLAT * steep:
                j += 1
            if j + 1 < len(drops) and drops[j + 1] >= STALL_STEEP * steep:
                return at + i, at + j + 1
            i = j + 1
        else:
            i += 1
    return None


def stall_at(force: list[int] | list[float]) -> int | None:
    """The first sample of the stall just after the peak (stall_span), or None for a smooth fall."""
    span = stall_span(force)
    return span[0] if span else None


def stall_stats(strokes) -> tuple[float, float]:
    """% of strokes with a stall after the peak, and where (median, cm of handle travel; 0 if none)."""
    strokes = [s for s in strokes if s.force_n]
    at = [stall_at(s.force_n) for s in strokes]
    hits = [a for a in at if a is not None]
    if not strokes:
        return 0.0, 0.0
    return len(hits) / len(strokes) * 100, (statistics.median(hits) + 1) * SAMPLE_CM if hits else 0.0


def curve_variation(strokes) -> float:
    """Stroke-to-stroke variation: the RMS difference between each stroke's curve and the one before
    (both scaled to their own peak, over the longer drive of the two), averaged. 0 = identical strokes."""
    diffs = []
    for a, b in zip(strokes, list(strokes)[1:]):
        ta, tb = max(a.force_n, default=0), max(b.force_n, default=0)
        if ta <= 0 or tb <= 0:
            continue
        n = max(drive_end(a.force_n), drive_end(b.force_n)) + 1
        diffs.append((sum((a.force_n[i] / ta - b.force_n[i] / tb) ** 2 for i in range(n)) / n) ** 0.5)
    return statistics.fmean(diffs) if diffs else 0.0


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
    ftp: float | None = None
    intensity: str = ""  # Moderate, Challenging or Max (INTENSITIES)
    steady_power_w: float = 0.0
    started_at: float = 0.0  # time of its first stroke (same clock as the strokes)
    expected_finish: float = 1.0  # the usual finish at this intensity, as a share of the Easy one
    peak_at_pct: float = 0.0  # where the force peaks, % of the drive (mean over the strokes)
    peak_spread_pct: float = 0.0  # ... and how much that varies from stroke to stroke (std dev)
    steady_peak_at_pct: float = 0.0
    steady_peak_spread_pct: float = 0.0
    variation: float = 0.0  # stroke-to-stroke variation of the curve (curve_variation)
    steady_variation: float = 0.0  # ... in the Easy rowing it is judged against
    length_sd_cm: float = 0.0  # spread of stroke length
    steady_length_sd_cm: float = 0.0
    short_strokes: int = 0  # strokes cut clearly short (SHORT_STROKE_PCT)
    shortest_cm: float = 0.0
    stall_pct: float = 0.0  # strokes with a stall just after the peak (stall_at), %
    stall_cm: float = 0.0  # ... where, cm of handle travel
    steady_stall_pct: float = 0.0  # ... in the Easy rowing before

    @property
    def transition(self) -> bool:
        """The force stalled after the peak on clearly more strokes than in Easy rowing."""
        return self.stall_pct >= max(STALL_FLAG_PCT, self.steady_stall_pct + STALL_FLAG_OVER)

    @property
    def variation_ratio(self) -> float:
        # (floored: real Easy rowing varies 0.05-0.08, so a near-zero reference can't blow the ratio up)
        return self.variation / max(self.steady_variation, VARIATION_FLOOR)

    @property
    def uneven(self) -> bool:
        """The strokes were clearly less even than in the Easy rowing before the effort."""
        return self.variation_ratio >= UNEVEN_VARIATION or self.length_sd_cm >= max(
            self.steady_length_sd_cm * UNEVEN_LENGTH_SD_RATIO, UNEVEN_LENGTH_SD_MIN)

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
        """The finish against your usual stroke at this intensity (1.0 = as usual)."""
        expected = self.steady_back_share * (self.expected_finish or 1.0)
        return self.back_share / expected if expected else 1.0

    @property
    def easy_finish_ratio(self) -> float:
        """The finish against your Easy stroke, whatever the intensity (what is learned from)."""
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
            "expected_finish": round(self.expected_finish, 3),
            "easy_finish_ratio": round(self.easy_finish_ratio, 3),
            "rating": self.rating,
            "steady_power_w": round(self.steady_power_w),
            "drive_late": round(self.lateness, 2),
            "legs_late": self.legs_late,
            "ftp_pct": round(self.power_w / self.ftp * 100) if self.ftp else None,
            "intensity": self.intensity,
            "balance": round(balance(self.finish_ratio, self.lateness, self.cues), 2),
            "length_balance": round(length_balance(self.length_delta_pct, self.cues), 2),
            "peak_at_pct": round(self.peak_at_pct),
            "peak_spread_pct": round(self.peak_spread_pct, 1),
            "steady_peak_at_pct": round(self.steady_peak_at_pct),
            "steady_peak_spread_pct": round(self.steady_peak_spread_pct, 1),
            "peak_wanders": self.peak_wanders,
            "variation_ratio": round(self.variation_ratio, 2),
            "length_sd_cm": round(self.length_sd_cm, 1),
            "steady_length_sd_cm": round(self.steady_length_sd_cm, 1),
            "short_strokes": self.short_strokes,
            "shortest_cm": round(self.shortest_cm),
            "uneven": self.uneven,
            "stall_pct": round(self.stall_pct),
            "stall_cm": round(self.stall_cm),
            "steady_stall_pct": round(self.steady_stall_pct),
            "transition": self.transition,
            "reference": "local",  # judged against the Easy rowing just before (sessions.learned_finish)
            "fade_pct": round((self.last_third_back_share / self.first_third_back_share - 1) * 100)
            if self.first_third_back_share else 0,
            "curve": [round(v) for v in self.curve],
            "steady_curve": [round(v) for v in self.steady_curve],
        }

    def log_line(self) -> str:
        m, s = divmod(round(self.duration_s), 60)
        label = f" {self.intensity}" if self.intensity else ""
        return (f"Interval {self.index}: {m}:{s:02d}, {self.strokes} strokes, {self.power_w:.0f} W{label}, "
                f"{self.stroke_rate_spm:.1f} spm, length {self.length_cm:.0f} cm ({self.length_delta_cm:+.0f}), "
                f"peak {self.peak_n:.0f} N at {self.peak_at_pct:.0f}% \u00b1{self.peak_spread_pct:.1f} "
                f"(Easy {self.steady_peak_at_pct:.0f}% \u00b1{self.steady_peak_spread_pct:.1f}), "
                f"finish {self.finish_pct:+d}% vs usual {self.intensity or 'Easy'} ({self.rating}), "
                f"over the effort {round((self.last_third_back_share / (self.first_third_back_share or 1) - 1) * 100):+d}%, "
                f"strokes varied x{self.variation_ratio:.2f} Easy, length \u00b1{self.length_sd_cm:.1f} cm "
                f"(Easy \u00b1{self.steady_length_sd_cm:.1f})"
                + (f", {self.short_strokes} cut short (shortest {self.shortest_cm:.0f} cm)" if self.short_strokes else "")
                + (", UNEVEN" if self.uneven else "")
                + f", stall after the peak on {self.stall_pct:.0f}% (Easy {self.steady_stall_pct:.0f}%)"
                + (" at about {:.0f} cm".format(self.stall_cm) if self.stall_cm else "")
                + (", TRANSITION" if self.transition else "")
                + (f", late push ({self.lateness:+.1f}% of the drive)" if self.legs_late else ""))


@dataclass
class _Effort:
    strokes: list[StrokeCurve] = field(default_factory=list)
    cue: str | None = None


class IntervalCoach:
    def __init__(self, settings: CoachSettings = DEFAULT_SETTINGS, ftp: float | None = None,
                 reference=None) -> None:
        """reference: a callable returning the last session's fresh Easy stroke (sessions.
        latest_fresh_easy), called whenever a new session starts; None: always learn it today."""
        self.settings = settings
        self.ftp = ftp  # rowing FTP, W (SRB_COACH_FTP, or from intervals.icu); None: efforts by steady alone
        self.reference = reference
        self.expect = dict(EXPECTED_FINISH)
        self.session = 0
        self.reset()

    @property
    def effort_floor_w(self) -> float:
        """With an FTP: the least power an effort must reach (ftp_floor_pct of it); else 0."""
        return self.ftp * self.settings.ftp_floor_pct / 100 if self.ftp else 0.0

    def intensity(self, watts: float) -> str:
        """Recovery, Easy, Moderate, Challenging or Max (INTENSITIES), by % of FTP or against Easy."""
        if self.ftp:
            x, bounds = watts / self.ftp * 100, FTP_BOUNDS_PCT
        else:
            base = self.steady_power()
            if not base:
                return "Easy"
            x, bounds = watts / base, EASY_BOUNDS
        return INTENSITIES[sum(x >= b for b in bounds)]

    def _start_at(self, base: float) -> float:
        """Smoothed power at which an effort starts."""
        if self.ftp:  # Moderate and above, and clearly above Easy
            return max(self.effort_floor_w, base + self.settings.start_min_w)
        return max(base * self.settings.start_ratio, base + self.settings.start_min_w)

    def _end_at(self) -> float:
        """Smoothed power below which an effort is over."""
        end = self.steady_power() * self.settings.end_ratio
        return max(end, self.effort_floor_w * 0.95) if self.ftp else end

    @property
    def max_effort_s(self) -> float:
        """Without an FTP, longer is taken for a new steady pace; with one, efforts have no limit."""
        return float("inf") if self.ftp else self.settings.max_effort_s

    def reset(self) -> None:
        self.steady: deque[StrokeCurve] = deque(maxlen=STEADY_WINDOW)
        # The same, without the warm-up (its shorter strokes look like a stronger finish): what
        # efforts are judged against once there are SHAPE_MIN of them.
        self.shape: deque[StrokeCurve] = deque(maxlen=STEADY_WINDOW)
        self.recent: deque[StrokeCurve] = deque(maxlen=SMOOTH)
        self.effort: _Effort | None = None
        self.recovery_left = 0
        # Easy rowing: judged against the fresh Easy stroke (baseline): the last session's when there
        # is one, else today's (provisional from PROVISIONAL_MIN strokes, set at FRESH_TO_S).
        self.saved_ref: dict | None = None
        if self.reference is not None:
            try:
                self.saved_ref = self.reference()
            except Exception:  # noqa: BLE001 - no saved stroke: learn today's
                log.exception("Could not read the last session's Easy stroke")
        self.baseline: dict | None = dict(self.saved_ref) if self.saved_ref else None
        self.expect = {**EXPECTED_FINISH, **((self.saved_ref or {}).get("expect") or {})}
        self.fresh: list[StrokeCurve] = []
        self.fresh_done = False
        self.fresh_result: dict | None = None
        self.length_note: dict | None = None
        self.easy_window: deque[StrokeCurve] = deque(maxlen=CUE_WINDOW)
        self.easy_cue: str | None = None
        self.easy_log: list[tuple[float, float]] = []  # (back-half share, length cm) per Easy stroke
        self.intervals: list[IntervalSummary] = []
        self.last_time: float | None = None
        self.last_count = 0
        self.session += 1  # a new number every time the coach starts over
        self.first_time: float | None = None
        self.strokes_total = 0
        self.power_total = 0.0
        self.length_total = 0.0
        self.trace: list[tuple[float, int, float]] = []  # (time, heart rate, power) per stroke
        # Every stroke, for the session's activity file and later analysis:
        # (time, distance m, power W, strokes/min, heart rate, stroke length cm, force curve N)
        self.stroke_log: list[tuple[float, int, float, float, int, int, tuple[int, ...]]] = []

    # ------------------------------------------------------------ reference

    @property
    def ready(self) -> bool:
        return len(self.steady) >= STEADY_MIN

    def steady_power(self) -> float:
        recent = list(self.steady)[-POWER_RECENT:]
        return statistics.median(stroke_power(s) for s in recent) if recent else 0.0

    def steady_length(self) -> float:
        ref = self.shape_strokes()
        return statistics.fmean(s.stroke_length_cm for s in ref) if ref else 0.0

    def steady_back_share(self) -> float:
        return back_share(mean_curve(self.shape_strokes()))

    def local_easy(self) -> list[StrokeCurve]:
        """The Easy strokes just before now (after the warm-up, efforts and their recovery left out)."""
        return list(self.shape)[-LOCAL_EASY:]

    def effort_ref(self) -> dict:
        """What efforts are judged against: the Easy rowing just before them (LOCAL_EASY strokes after
        the warm-up), so a change in your Easy stroke over the row isn't blamed on the efforts. Before
        there are SHAPE_MIN of those: your fresh Easy stroke (last session's), else the Easy strokes so far."""
        if len(self.shape) >= SHAPE_MIN:
            ref = self.local_easy()
        elif self.baseline:
            return self.baseline
        else:
            ref = list(self.steady)
        curve = mean_curve(ref)
        return {"curve": curve, "back_share": back_share(curve),
                "length_cm": statistics.fmean(s.stroke_length_cm for s in ref) if ref else 0.0}

    def expected(self, intensity: str) -> float:
        return self.expect.get(intensity, 1.0)

    def shape_strokes(self) -> list[StrokeCurve]:
        """The Easy strokes efforts are judged against: after the warm-up when there are enough."""
        return list(self.shape) if len(self.shape) >= SHAPE_MIN else list(self.steady)

    # ------------------------------------------------------------ input

    def add(self, s: StrokeCurve) -> IntervalSummary | None:
        """Take one stroke. Returns the summary if this stroke ended an effort."""
        finished = None
        if self.last_time is not None:
            gap = s.time - self.last_time
            if gap > NEW_SESSION_S or s.stroke_count < self.last_count or \
                    (gap > LEAD_GAP_S and self.strokes_total < LEAD_STROKES):
                self.reset()  # (a stray pull or two while setting up is not the start of the row)
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
                                s.stroke_length_cm, tuple(s.force_n)))
        self.recent.append(s)
        smoothed = statistics.median(stroke_power(r) for r in self.recent)
        if not self.fresh_done and s.time - self.first_time >= FRESH_TO_S:
            self._settle_fresh()

        if self.effort is None:
            base = self.steady_power()
            start_at = self._start_at(base)
            if self.ready and len(self.recent) >= 3 and smoothed >= start_at:
                # Back-date to the first hard stroke among the recent ones.
                recent = list(self.recent)
                first = next(i for i, r in enumerate(recent) if stroke_power(r) >= start_at)
                self.effort = _Effort(strokes=recent[first:])
                self._drop_from_steady(self.effort.strokes)
                self.easy_window.clear()
                self.easy_cue = None
                log.debug("Effort started (%.0f W vs %.0f W steady)", smoothed, base)
            else:
                self._learn(s, base)
        else:
            self.effort.strokes.append(s)
            if s.time - self.effort.strokes[0].time > self.max_effort_s:
                self._rebaseline()
            elif smoothed < self._end_at():
                finished = self._finish(s.time) or finished
            else:
                self._update_cue()
        return finished

    def _learn(self, s: StrokeCurve, base: float) -> None:
        if self.recovery_left > 0:
            self.recovery_left -= 1
            return
        if self.ready and (stroke_power(s) < base * EASY_RATIO or self.intensity(stroke_power(s)) == "Recovery"):
            return
        self.steady.append(s)
        elapsed = s.time - self.first_time
        if elapsed >= FRESH_FROM_S:
            self.shape.append(s)
        if FRESH_FROM_S <= elapsed < FRESH_TO_S and (not self.ready or self.intensity(stroke_power(s)) == "Easy"):
            self.fresh.append(s)
            if self.baseline is None and len(self.fresh) >= PROVISIONAL_MIN:
                self.baseline = fresh_stats(self.fresh)
                log.debug("Provisional Easy reference from %d fresh strokes", len(self.fresh))
        self.easy_window.append(s)
        self.easy_log.append((back_share(s.force_n), s.stroke_length_cm))
        self._update_easy_cue()

    def _fresh_outcome(self) -> tuple[dict | None, dict | None, dict | None]:
        """From the fresh strokes so far: (today's fresh stroke to save, the reference to use, the
        length note), by the rules above. (None, None, None) with too few strokes."""
        if len(self.fresh) < FRESH_MIN:
            return None, None, None
        today = fresh_stats(self.fresh)
        saved = self.saved_ref
        if not saved:
            return today, today, None
        diff = today["length_cm"] - saved["length_cm"]
        if diff > LENGTH_SAME_CM:  # clearly longer: the new length
            return today, {**saved, "length_cm": today["length_cm"]}, {"change_cm": round(diff), "kept": False}
        if diff >= -LENGTH_SAME_CM:  # within the margin: today's length (no cue for a cm or two)
            return today, {**saved, "length_cm": today["length_cm"]}, None
        # clearly shorter: the saved length stays (and is carried to the next session), so the cue speaks up
        return {**today, "measured_length_cm": today["length_cm"], "length_cm": saved["length_cm"]}, \
            {**saved}, {"change_cm": round(diff), "kept": True}

    def _settle_fresh(self) -> None:
        self.fresh_done = True
        result, ref, note = self._fresh_outcome()
        self.fresh_result, self.length_note = result, note
        if ref is not None:
            self.baseline = ref
            log.info("Fresh Easy stroke: finish %.3f, length %.0f cm (%d strokes)%s", result["back_share"],
                     result.get("measured_length_cm", result["length_cm"]), result["strokes"],
                     f"; length {note['change_cm']:+d} cm against last time" if note else "")

    def _rebaseline(self) -> None:
        """Rowing has settled at a higher pace: make it the steady reference, no interval."""
        log.info("Steady pace moved up to about %.0f W; no interval recorded",
                 statistics.median(stroke_power(s) for s in self.effort.strokes))
        self.steady.extend(self.effort.strokes)
        self.shape.extend(self.effort.strokes)
        self.effort = None

    def _drop_from_steady(self, strokes: list[StrokeCurve]) -> None:
        ids = {id(s) for s in strokes}
        for ref in (self.steady, self.shape):
            kept = [s for s in ref if id(s) not in ids]
            ref.clear()
            ref.extend(kept)

    def _finish(self, now: float) -> IntervalSummary | None:
        effort, self.effort = self.effort, None
        if effort is None:
            return None
        end_at = self._end_at()
        strokes = list(effort.strokes)
        while strokes and stroke_power(strokes[-1]) < end_at:  # the easy strokes that ended it
            strokes.pop()
        self.recovery_left = RECOVERY_STROKES
        if len(strokes) < self.settings.min_effort_strokes:
            return None
        summary = None
        parts = self._intensity_parts(strokes)
        for k, part in enumerate(parts):
            end = now if k == len(parts) - 1 else parts[k + 1][0].time
            summary = self._summarise(part, end)
            self.intervals.append(summary)
            log.info(summary.log_line())
        return summary

    def _intensity_parts(self, strokes: list[StrokeCurve]) -> list[list[StrokeCurve]]:
        """An effort split where it steps to another intensity (Moderate, then Challenging) and stays
        there for SPLIT_MIN_STROKES; shorter changes stay part of the run around them."""
        smoothed = [statistics.median(stroke_power(x) for x in strokes[max(0, i - SMOOTH // 2):i + SMOOTH // 2 + 1])
                    for i in range(len(strokes))]
        labels = [self.intensity(w) for w in smoothed]
        labels = [k if k in ("Moderate", "Challenging", "Max") else "Moderate" for k in labels]
        runs: list[list] = []  # [label, first index, last index]
        for i, k in enumerate(labels):
            if runs and runs[-1][0] == k:
                runs[-1][2] = i
            else:
                runs.append([k, i, i])
        changed = True
        while changed and len(runs) > 1:  # fold the shortest too-short run into a neighbour
            changed = False
            short = [j for j, r in enumerate(runs) if r[2] - r[1] + 1 < SPLIT_MIN_STROKES]
            if short:
                j = min(short, key=lambda j: runs[j][2] - runs[j][1])
                n = j - 1 if j > 0 else j + 1
                lo, hi = min(runs[j][1], runs[n][1]), max(runs[j][2], runs[n][2])
                runs[n][1:] = [lo, hi]
                del runs[j]
                merged = []
                for r in runs:
                    if merged and merged[-1][0] == r[0]:
                        merged[-1][2] = r[2]
                    else:
                        merged.append(r)
                runs, changed = merged, True
        return [strokes[r[1]:r[2] + 1] for r in runs]

    def _summarise(self, strokes: list[StrokeCurve], now: float) -> IntervalSummary:
        n = len(strokes)
        period = 60.0 / statistics.fmean(s.stroke_rate_spm for s in strokes)
        third = max(1, n // 3)
        peak_at, peak_spread = peak_stats(strokes)
        local = self.local_easy() if len(self.shape) >= SHAPE_MIN else self.shape_strokes()
        steady_at, steady_spread = peak_stats(local)
        ref = self.effort_ref()
        settled = strokes[SETTLE_STROKES:] if n > 2 * SETTLE_STROKES + 4 else strokes
        lengths = [s.stroke_length_cm for s in strokes]
        local_lengths = [s.stroke_length_cm for s in local]
        stall_pct, stall_cm = stall_stats(strokes)
        cut = [x for x in lengths if ref["length_cm"] and x < ref["length_cm"] * (1 - SHORT_STROKE_PCT / 100)]
        intensity = self.intensity(statistics.fmean(stroke_power(s) for s in strokes))
        return IntervalSummary(
            index=len(self.intervals) + 1,
            duration_s=strokes[-1].time - strokes[0].time + period,
            strokes=n,
            power_w=statistics.fmean(stroke_power(s) for s in strokes),
            stroke_rate_spm=statistics.fmean(s.stroke_rate_spm for s in strokes),
            length_cm=statistics.fmean(s.stroke_length_cm for s in strokes),
            peak_n=statistics.fmean(max(s.force_n) for s in strokes),
            back_share=back_share(mean_curve(strokes)),
            steady_back_share=ref["back_share"],
            steady_length_cm=ref["length_cm"],
            curve=mean_curve(strokes),
            steady_curve=list(ref["curve"]),
            first_third_back_share=back_share(mean_curve(strokes[:third])),
            last_third_back_share=back_share(mean_curve(strokes[-third:])),
            ended_at=now,
            legs_threshold=self.settings.cues.legs_cue,
            cues=self.settings.cues,
            ftp=self.ftp,
            intensity=intensity,
            expected_finish=self.expected(intensity),
            steady_power_w=self.steady_power(),
            started_at=strokes[0].time,
            peak_at_pct=peak_at,
            peak_spread_pct=peak_spread,
            steady_peak_at_pct=steady_at,
            steady_peak_spread_pct=steady_spread,
            variation=curve_variation(settled),
            steady_variation=curve_variation(local),
            length_sd_cm=statistics.pstdev(x.stroke_length_cm for x in settled) if len(settled) > 1 else 0.0,
            steady_length_sd_cm=statistics.pstdev(local_lengths) if len(local_lengths) > 1 else 0.0,
            short_strokes=len(cut),
            shortest_cm=min(lengths),
            stall_pct=stall_pct,
            stall_cm=stall_cm,
            steady_stall_pct=stall_stats(local)[0],
        )

    # ------------------------------------------------------------ live cue

    def _window(self) -> list[StrokeCurve]:
        return self.effort.strokes[-CUE_WINDOW:] if self.effort else []

    def _window_measures(self) -> tuple[float, float, float]:
        """The last few strokes against steady: finish ratio, lateness (% of drive), % shorter."""
        window = self._window()
        curve = mean_curve(window)
        ref = self.effort_ref()
        expect = self.expected(self.intensity(statistics.fmean(stroke_power(s) for s in window)))
        ratio = back_share(curve) / ((ref["back_share"] * expect) or 1)
        ref_len = ref["length_cm"] or 1
        short_by_pct = (ref_len - statistics.fmean(s.stroke_length_cm for s in window)) / ref_len * 100
        return ratio, drive_lateness(curve, ref["curve"]), short_by_pct

    def _judge(self, previous: str | None, ratio: float, late: float, short_by_pct: float, stalls: int = 0) -> str:
        """The cue for a window of strokes against a reference (with a little hysteresis)."""
        t = self.settings.cues
        if ratio < t.finish_cue or (previous == "finish" and ratio < t.finish_clear):
            return "finish"
        if late > t.legs_cue or (previous == "legs" and late > t.legs_clear):
            return "legs"
        if short_by_pct > t.length_cue_pct or (previous == "length" and short_by_pct > t.length_clear_pct):
            return "length"
        if stalls >= t.transition_cue or (previous == "transition" and stalls > t.transition_clear):
            return "transition"
        return "good"

    def _update_cue(self) -> None:
        e = self.effort
        if len(e.strokes) < CUE_MIN:
            e.cue = None
            return
        e.cue = self._judge(e.cue, *self._window_measures(), stalls=_stalls(self._window()))

    def _easy_measures(self) -> tuple[float, float, float]:
        """The last few Easy strokes against the early Easy ones: finish ratio, lateness, % shorter."""
        b = self.baseline
        curve = mean_curve(self.easy_window)
        ratio = back_share(curve) / (b["back_share"] or 1)
        short_by_pct = (b["length_cm"] - statistics.fmean(s.stroke_length_cm for s in self.easy_window)) \
            / (b["length_cm"] or 1) * 100
        return ratio, drive_lateness(curve, b["curve"]), short_by_pct

    def warming_up(self) -> bool:
        """The first FRESH_FROM_S of a row: warm-up strokes are shorter, so no Easy cues yet."""
        return self.first_time is None or (self.last_time or 0) - self.first_time < FRESH_FROM_S

    def _update_easy_cue(self) -> None:
        if self.baseline is None or len(self.easy_window) < CUE_MIN or self.warming_up():
            self.easy_cue = None
            return
        self.easy_cue = self._judge(self.easy_cue, *self._easy_measures())  # no BETTER TRANSITION (see stall_at)

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
            "ftp": round(self.ftp) if self.ftp else None,
            "settings": {"start_pct": self.settings.start_pct,
                         "max_effort_min": None if self.ftp else self.max_effort_s / 60,
                         "ftp_floor_pct": self.settings.ftp_floor_pct if self.ftp else None,
                         "sensitivity": self.settings.sensitivity},
            "intervals": intervals,
            "heart_rate": hr,
            "easy_drift": self.easy_drift(),
            "fresh_easy": self._fresh_record(),
            "findings": findings(intervals) + easy_findings(self.easy_drift()) + length_findings(self._length_note())
            + heart_rate_findings(hr, intervals),
        }

    def _fresh_record(self) -> dict | None:
        """Today's fresh Easy stroke, for the session record (the next session's reference)."""
        result = self.fresh_result if self.fresh_done else self._fresh_outcome()[0]
        if not result:
            return None
        return {"curve": [round(v, 1) for v in result["curve"]], "back_share": round(result["back_share"], 4),
                "length_cm": round(result["length_cm"], 1), "strokes": result["strokes"],
                **({"measured_length_cm": round(result["measured_length_cm"], 1)}
                   if "measured_length_cm" in result else {})}

    def _length_note(self) -> dict | None:
        return self.length_note if self.fresh_done else self._fresh_outcome()[2]

    def easy_drift(self) -> dict | None:
        """Easy rowing through the session: the finish and stroke length of the last EASY_DRIFT_N
        Easy strokes against the first (both medians, as single strokes scatter)."""
        n = EASY_DRIFT_N
        if len(self.easy_log) < 3 * n:
            return None
        # the first: the fresh Easy strokes (after the warm-up) when there are enough
        first = [(back_share(s.force_n), s.stroke_length_cm) for s in self.fresh] \
            if len(self.fresh) >= n else self.easy_log[:n]
        last = self.easy_log[-n:]
        f0, f1 = statistics.median(x[0] for x in first), statistics.median(x[0] for x in last)
        l0, l1 = statistics.median(x[1] for x in first), statistics.median(x[1] for x in last)
        return {"finish_pct": round((f1 / f0 - 1) * 100) if f0 else 0, "length_delta_cm": round(l1 - l0)}

    # ------------------------------------------------------------ status

    def state(self, now: float) -> dict:
        """What the screen needs, for status.json."""
        if self.effort is not None and self.last_time is not None and now - self.last_time > GAP_S:
            self._finish(self.last_time)
        last = self.intervals[-1] if self.intervals else None
        out: dict = {
            "mode": "warmup" if not self.ready else "interval" if self.effort else "steady",
            "session_strokes": self.strokes_total,  # strokes in this session (the screen offers Y: Done)
            "ftp": round(self.ftp) if self.ftp else None,
            "steady_power_w": round(self.steady_power()),
            "intervals": len(self.intervals),
            "last": last.as_dict() if last else None,
            "history": [{k: v for k, v in i.as_dict().items() if k not in ("curve", "steady_curve")}
                        for i in self.intervals[-20:]],
            "show_summary": bool(last and self.effort is None and now - last.ended_at < SUMMARY_SHOW_S),
            # Between efforts: Recovery or Easy (from the last few strokes), the latest stroke and the
            # reference it is drawn against (the early Easy strokes once there are enough, else the
            # recent ones), and the Easy cue (None while still learning).
            "intensity": self.intensity(statistics.median(stroke_power(r) for r in self.recent))
            if self.recent and self.effort is None else None,
            "cue": self.easy_cue if self.effort is None else None,
            "warming_up": self.warming_up(),
            "easy_reference": ("last session" if self.saved_ref else "today") if self.baseline else None,
            "stroke_curve": list(self.stroke_log[-1][6]) if self.stroke_log else [],
            "steady_curve": [round(v) for v in (self.baseline["curve"] if self.baseline else mean_curve(self.steady))]
            if self.steady else [],
            "length_delta_cm": round(statistics.fmean(s.stroke_length_cm for s in self.easy_window)
                                     - self.baseline["length_cm"]) if self.baseline and self.easy_window else None,
        }
        if out["intensity"] not in (None, "Recovery", "Easy"):
            out["intensity"] = "Easy"  # above Easy but not (yet) an effort
        if out["intensity"] == "Recovery":
            out["cue"] = None  # Recovery: the numbers only
        if self.effort is not None:
            strokes = self.effort.strokes
            window = self._window()
            first = strokes[0]
            judged = len(strokes) >= CUE_MIN
            ratio, late, short_by_pct = self._window_measures() if judged else (1.0, 0.0, 0.0)
            out["live"] = {
                "index": len(self.intervals) + 1,
                "intensity": self.intensity(statistics.fmean(stroke_power(s) for s in strokes)),
                "elapsed_s": round(max(0.0, now - first.time)),
                "strokes": len(strokes),
                "power_w": round(statistics.fmean(stroke_power(s) for s in strokes[-3:])),
                "length_cm": round(statistics.fmean(s.stroke_length_cm for s in window)),
                "cue": self.effort.cue,
                "finish_pct": round((ratio - 1) * 100) if judged else None,
                "length_delta_cm": round(statistics.fmean(s.stroke_length_cm for s in window)
                                         - self.effort_ref()["length_cm"]),
                # The screens' two bars (see balance()): None until there is enough to judge.
                "balance": round(balance(ratio, late, self.settings.cues), 2) if judged else None,
                "length_balance": round(length_balance(-short_by_pct, self.settings.cues), 2) if judged else None,
                "curve": [round(v) for v in mean_curve(window)],
                "stroke_curve": list(strokes[-1].force_n),  # the latest stroke, drawn live over steady
                "stall": stall_marker(window, self.effort.cue),  # where to mark it (BETTER TRANSITION)
                "steady_curve": [round(v) for v in self.effort_ref()["curve"]],
            }
        return out


def stall_marker(strokes, cue: str | None) -> dict | None:
    """Where to mark the stall on the latest stroke's curve while BETTER TRANSITION shows: the
    samples where that stroke stalls ("now": true), or else where the recent ones did (false)."""
    if cue != "transition" or not strokes:
        return None
    latest = stall_span(strokes[-1].force_n) if strokes[-1].force_n else None
    if latest is not None:
        return {"sample": latest[0], "until": latest[1], "now": True}
    hits = [a for a in (stall_span(s.force_n) for s in strokes if s.force_n) if a is not None]
    if not hits:
        return None
    return {"sample": round(statistics.median(a for a, _ in hits)),
            "until": round(statistics.median(b for _, b in hits)), "now": False}


def _stalls(strokes) -> int:
    """How many of these strokes stall after the peak (stall_at)."""
    return sum(stall_at(s.force_n) is not None for s in strokes if s.force_n)


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
        return ["No efforts: Easy rowing only, or efforts too short or too close to the Easy pace to count."]
    out: list[str] = []
    shorter = [i for i in intervals if i.get("length_delta_cm", 0) <= SHORTER_CM]
    good = [i for i in intervals if i["rating"] == "good" and not i.get("legs_late") and i not in shorter]
    faded = [i for i in intervals if i["rating"] == "short"]  # "short": the finish faded (an old name)
    ok = [i for i in intervals if i["rating"] == "ok"]
    n = len(intervals)

    kinds = [k for k in INTENSITIES if any(i.get("intensity") == k for i in intervals)]
    if kinds:
        parts = []
        for k in kinds:
            these = [i for i in intervals if i.get("intensity") == k]
            watts = [round(i["power_w"]) for i in these if i.get("power_w")]
            span = "" if not watts else f" ({min(watts)} W)" if min(watts) == max(watts) \
                else f" ({min(watts)}\u2013{max(watts)} W)"
            parts.append(f"{len(these)} {k}{span}")
        out.append("Efforts: " + ", ".join(parts) + ".")

    if len(good) == n:
        out.append(f"All {n} efforts kept their shape and length: finish within 5% of your usual at that intensity.")
    elif good:
        out.append(f"{len(good)} of {n} efforts kept their shape and length ({_names(good)}).")
    if faded:
        lo, hi = min(i["finish_pct"] for i in faded), max(i["finish_pct"] for i in faded)
        span = _pct(lo) if lo == hi else f"{_pct(hi)} to {_pct(lo)}"
        out.append(f"The finish faded in {_names(faded)}: {span} against your usual at that intensity.")
    if ok:
        pcts = ", ".join(_pct(i["finish_pct"]) for i in ok)
        out.append(f"The finish eased a little in {_names(ok)} ({pcts}).")
    if shorter:
        cm = sum(i["length_delta_cm"] for i in shorter) / len(shorter)
        out.append(f"Strokes were shorter than Easy in {_names(shorter)} (about {abs(cm):.0f} cm).")

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
        out.append(f"The push came later in the drive than in Easy rowing in {_names(legs)}. The curve can't say "
                   f"why (an early arm pull and a slow push off the catch look alike); a video of an effort can.")

    uneven = [i for i in intervals if i.get("uneven")]
    for i in uneven:
        detail = [f"strokes varied {i['variation_ratio']:.1f}\u00d7 as much as in the Easy rowing before it"]
        if i.get("length_sd_cm", 0) >= max(2 * i.get("steady_length_sd_cm", 0), UNEVEN_LENGTH_SD_MIN):
            detail.append(f"length \u00b1{i['length_sd_cm']:.0f} cm against \u00b1{i['steady_length_sd_cm']:.0f}")
        if i.get("short_strokes"):
            n_short = i["short_strokes"]
            detail.append(f"{n_short} stroke{'s' if n_short > 1 else ''} cut short (shortest {i['shortest_cm']} cm)")
        out.append(f"I{i['index']} was uneven: " + ", ".join(detail) + ".")

    stalled = [i for i in intervals if i.get("transition")]
    if stalled:
        pcts = ", ".join(f"{i['stall_pct']}%" for i in stalled)
        cm = round(statistics.median(i["stall_cm"] for i in stalled))
        easy = max(i.get("steady_stall_pct", 0) for i in stalled)
        out.append(f"The force stalled just after the peak (about {cm} cm into the drive) in {_names(stalled)} "
                   f"({pcts} of strokes, against {easy}% or less in Easy rowing): a break in the handover after the "
                   f"peak. The curve can't say which one; keeping the arms relaxed until the swing is nearly done "
                   f"tells legs-to-back from back-to-arms, and a video shows it.")

    wander = [i for i in intervals if i.get("peak_wanders")]
    if wander:
        spreads = ", ".join(f"\u00b1{i['peak_spread_pct']:.0f}%" for i in wander)
        steady_spread = wander[0].get("steady_peak_spread_pct", 0)
        out.append(f"The force peak landed less consistently from stroke to stroke in {_names(wander)} "
                   f"({spreads} of the drive, against \u00b1{steady_spread:.0f}% in Easy rowing).")


    steady = [i["steady_power_w"] for i in intervals if i.get("steady_power_w")]
    if len(steady) >= 2 and min(steady):
        change = (steady[-1] - steady[0]) / steady[0] * 100
        if abs(change) <= 5:
            out.append(f"Easy pace held at about {round(sum(steady) / len(steady))} W between efforts.")
        else:
            out.append(f"Easy pace went from {steady[0]} W to {steady[-1]} W over the session.")
    return out


# ---------------------------------------------------------------- heart rate (optional)

HR_PEAK_AFTER_S = 30.0  # heart rate peaks a little after an effort ends
HR_RECOVERY_S = 60.0  # recovery: the drop this long after the peak
HR_MIN_COVERAGE = 0.5  # share of strokes with a reading before heart rate is reported
DRIFT_MIN_POINTS = 40  # steady strokes needed in each half for the drift
DRIFT_SKIP_S = 600.0  # heart rate is still climbing during the warm-up: left out of the drift


EASY_DRIFT_N = 60  # strokes at each end of the session's Easy rowing, for easy_drift()


def fresh_stats(strokes: list[StrokeCurve]) -> dict:
    """The fresh Easy stroke from a list of strokes: average curve, its back-half share, length."""
    curve = mean_curve(strokes)
    return {"curve": curve, "back_share": back_share(curve),
            "length_cm": statistics.fmean(s.stroke_length_cm for s in strokes), "strokes": len(strokes)}


def length_findings(note: dict | None) -> list[str]:
    """The stroke length calibration, when today's fresh strokes differed clearly from last time's."""
    if not note:
        return []
    cm = abs(note["change_cm"])
    if note["kept"]:
        return [f"Fresh Easy strokes were {cm} cm shorter than last time; the coach kept last time's length."]
    return [f"Fresh Easy strokes were {cm} cm longer than last time: the new length reference."]


def easy_findings(drift: dict | None) -> list[str]:
    """One sentence on how Easy rowing changed from the first minutes to the last, if it did."""
    if not drift:
        return []
    parts = []
    if abs(drift["finish_pct"]) >= 5:
        parts.append(f"finish {_pct(drift['finish_pct'])}")
    if abs(drift["length_delta_cm"]) >= 2:
        parts.append(f"strokes {abs(drift['length_delta_cm'])} cm {'longer' if drift['length_delta_cm'] > 0 else 'shorter'}")
    if not parts:
        return ["Easy rowing kept its shape from the first minutes to the last."]
    return [f"Easy rowing, last minutes against the first: {' and '.join(parts)} "
            "(efforts are judged against the Easy rowing just before them, so this isn't counted against them)."]


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
            out.append(f"Heart rate stayed in step with power through the Easy rowing (drift {_pct(round(d))}).")
        else:
            out.append(f"Steady rowing took {d:.0f}% more heartbeats per watt in the second half than the first "
                       f"(under 5% is typical): fatigue, heat or not enough fuel.")
    return out
