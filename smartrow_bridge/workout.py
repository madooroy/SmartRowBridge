# SPDX-License-Identifier: GPL-3.0-only
"""Planned workouts: follow a FIT workout file (e.g. exported from intervals.icu) while rowing.

Drop a workout file into the workouts folder (WORKOUT_DIR, /var/lib/smartrow-bridge/workouts):
the bridge picks it up within a few seconds and the workout starts with your next stroke. The
screen and the web page then show the step you're on, its target, your watts against it, the
time (or metres) left and what's next. When it is over the file moves to workouts/done/, so it
runs once; copy it in again to row it again. Delete it before rowing to cancel.

Steps last a time or a distance; a FIT repeat step ("repeat steps 2-3 five times") is unrolled.
Targets are power: watts as written in the file, or a % of FTP (FIT keeps watts as 1000 + W and
% of FTP as 0-1000), turned into watts with your FTP when the workout starts. A step without a
power target (open, or heart rate, pace...) is shown without one.

After the row each step gets its average power against the target (the first STEP_SETTLE_S of
a step left out: the change of pace) and how many of its strokes were within the tolerance.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import asdict, dataclass, field

log = logging.getLogger(__name__)

WORKOUT_DIR = "/var/lib/smartrow-bridge/workouts"
MAX_FILE_BYTES = 256 * 1024  # a workout file is a few hundred bytes; anything big is not one
STEP_SETTLE_S = 8.0  # the start of each step (the change of pace) isn't judged
TOLERANCE_PCT, TOLERANCE_MIN_W = 5.0, 5.0  # "on target": within 5 % of it, and never tighter than 5 W
DONE_SHOW_STROKES = 5  # "workout done" stays on the screens this many strokes

# FIT profile values (fit_tool returns the raw numbers)
DUR_TIME, DUR_DISTANCE, DUR_OPEN = 0, 1, 5
DUR_REPEAT_STEPS = 6  # repeat_until_steps_cmplt: duration_value = step to go back to, target_value = times
POWER_TARGETS = {4, 7, 8, 9, 10}  # power, power_3s, power_10s, power_30s, power_lap
INTENSITY = {0: "active", 1: "rest", 2: "warmup", 3: "cooldown", 4: "recovery", 5: "interval", 6: "other"}


@dataclass
class Step:
    kind: str  # "time" or "distance"
    length: float  # seconds or metres
    low_w: float | None = None  # target watts (low = high for a single value); None: no power target
    high_w: float | None = None
    low_pct: float | None = None  # ... or % of FTP, turned into watts when the workout starts
    high_pct: float | None = None
    intensity: str = "active"
    name: str = ""

    @property
    def target_w(self) -> float | None:
        if self.low_w is None or self.high_w is None:
            return None
        return (self.low_w + self.high_w) / 2


@dataclass
class Workout:
    name: str
    steps: list[Step] = field(default_factory=list)
    source: str = ""  # the file it came from

    @property
    def timed(self) -> bool:
        return all(s.kind == "time" for s in self.steps)

    @property
    def total_s(self) -> float | None:
        return sum(s.length for s in self.steps) if self.timed else None

    def uses_ftp(self) -> bool:
        return any(s.low_pct is not None for s in self.steps)

    def with_ftp(self, ftp: float | None) -> "Workout":
        """Targets in watts: % of FTP ones worked out with ftp (left without a target if there is none)."""
        steps = []
        for s in self.steps:
            s = Step(**asdict(s))
            if s.low_pct is not None:
                if ftp:
                    s.low_w, s.high_w = round(ftp * s.low_pct / 100), round(ftp * s.high_pct / 100)
                else:
                    log.warning("Workout %s: step targets are %% of FTP but there is no FTP; shown without them",
                                self.name)
            steps.append(s)
        return Workout(self.name, steps, self.source)


class WorkoutError(ValueError):
    pass


def _get(msg, name: str):
    """A field's value, scaled (duration_time in s, duration_distance in m), or None."""
    try:
        return getattr(msg, name)
    except Exception:  # noqa: BLE001 - a field this message type doesn't have
        return None


def _target(value) -> tuple[float | None, float | None]:
    """A FIT custom power target: (watts, None) above 1000 (1000 + W), (None, % of FTP) up to 1000."""
    if value is None or value <= 0:
        return None, None
    return (float(value - 1000), None) if value > 1000 else (None, float(value))


def read_fit(path: str, fallback_name: str = "Workout") -> Workout:
    """The workout in a FIT workout file. Raises WorkoutError if it isn't one we can follow."""
    from fit_tool.fit_file import FitFile  # (fit-tool: already needed for the activity files)
    from fit_tool.profile.messages.workout_message import WorkoutMessage
    from fit_tool.profile.messages.workout_step_message import WorkoutStepMessage

    try:
        fit = FitFile.from_file(path)
    except Exception as exc:  # noqa: BLE001 - any parse error means: not a workout file
        raise WorkoutError(f"not a FIT file ({exc})") from exc
    name, raw = None, []
    for record in fit.records:
        msg = record.message
        if isinstance(msg, WorkoutMessage):
            name = _get(msg, "workout_name")
        elif isinstance(msg, WorkoutStepMessage):
            raw.append(msg)
    if not raw:
        raise WorkoutError("no workout steps in it (an activity file, not a workout?)")
    raw.sort(key=lambda m: _get(m, "message_index") or 0)

    def plain(m) -> Step | None:
        kind = _get(m, "duration_type")
        if kind == DUR_TIME:
            length, kind_name = _get(m, "duration_time"), "time"
        elif kind == DUR_DISTANCE:
            length, kind_name = _get(m, "duration_distance"), "distance"
        else:
            return None
        if not length or length <= 0:
            return None
        low_w = high_w = low_pct = high_pct = None
        if _get(m, "target_type") in POWER_TARGETS:
            lo = _get(m, "custom_target_power_low") or _get(m, "custom_target_value_low")
            hi = _get(m, "custom_target_power_high") or _get(m, "custom_target_value_high") or lo
            (low_w, low_pct), (high_w, high_pct) = _target(lo), _target(hi)
            if (low_w is None) != (high_w is None):  # one in watts, one in %: not something we can show
                low_w = high_w = low_pct = high_pct = None
        return Step(kind_name, float(length), low_w, high_w, low_pct, high_pct,
                    INTENSITY.get(_get(m, "intensity"), "active"), _get(m, "workout_step_name") or "")

    # Unroll repeats: a repeat step sends us back to an earlier step a number of times.
    steps: list[Step] = []
    by_index = {(_get(m, "message_index") or i): i for i, m in enumerate(raw)}
    i = 0
    repeats_left: dict[int, int] = {}
    guard = 0
    while i < len(raw):
        guard += 1
        if guard > 5000:
            raise WorkoutError("its repeats never end")
        m = raw[i]
        if _get(m, "duration_type") == DUR_REPEAT_STEPS:
            back = by_index.get(int(_get(m, "duration_step") or _get(m, "duration_value") or 0))
            times = int(_get(m, "target_repeat_steps") or _get(m, "target_value") or 0)
            left = repeats_left.get(i, times - 1)  # the steps have run once already
            if back is not None and back < i and left > 0:
                repeats_left[i] = left - 1
                i = back
                continue
            repeats_left.pop(i, None)
            i += 1
            continue
        step = plain(m)
        if step is not None:
            steps.append(step)
        else:
            log.info("Workout step %d skipped (open-ended or not by time or distance)", i + 1)
        i += 1
    if not steps:
        raise WorkoutError("none of its steps last a time or a distance")
    return Workout(name or fallback_name, steps, path)


def tolerance(target: float) -> float:
    return max(target * TOLERANCE_PCT / 100, TOLERANCE_MIN_W)


class Player:
    """Follows a workout: started by the first stroke after it is loaded (start()); then advance()
    (every stroke, and once a second from the status) moves through the steps by time or distance."""

    def __init__(self, workout: Workout) -> None:
        self.original = workout  # as loaded (targets in % of FTP not yet in watts)
        self.workout = workout
        self.started_at: float | None = None  # monotonic
        self.start_distance = 0.0
        self.step = 0  # index of the current step
        self.step_started: list[tuple[float, float]] = []  # (monotonic time, distance) each step began
        self.ended_at: float | None = None  # monotonic: the last step done
        self.ended_distance = 0.0
        self.ftp: float | None = None

    @property
    def started(self) -> bool:
        return self.started_at is not None

    @property
    def done(self) -> bool:
        return self.ended_at is not None

    def start(self, t: float, distance: float, ftp: float | None) -> None:
        self.ftp = ftp
        self.workout = self.workout.with_ftp(ftp)
        self.started_at, self.start_distance = t, distance
        self.step_started = [(t, distance)]
        log.info("Workout %s started: %d steps%s", self.workout.name, len(self.workout.steps),
                 f", {round(self.workout.total_s / 60)} min" if self.workout.timed else "")

    def _step_end(self, i: int) -> tuple[str, float]:
        """("time", monotonic end) or ("distance", distance end) of step i, from where it began."""
        s = self.workout.steps[i]
        t0, d0 = self.step_started[i]
        return (s.kind, t0 + s.length) if s.kind == "time" else (s.kind, d0 + s.length)

    def advance(self, now: float, distance: float) -> None:
        if not self.started or self.done:
            return
        while True:
            kind, end = self._step_end(self.step)
            if kind == "time" and now >= end:
                at = (end, distance)  # a time step ends on the clock, rowing or not
            elif kind == "distance" and distance >= end:
                at = (now, distance)
            else:
                return
            if self.step + 1 >= len(self.workout.steps):
                self.ended_at, self.ended_distance = at
                log.info("Workout %s done", self.workout.name)
                return
            self.step += 1
            self.step_started.append(at)

    # ------------------------------------------------------------ live

    def state(self, now: float, distance: float, recent_power: float | None, strokes_since_end: int) -> dict | None:
        """For status.json (the screens): None once "done" has been shown long enough."""
        w = self.workout
        out: dict = {"name": w.name, "steps": len(w.steps), "total_s": w.total_s}
        if not self.started:
            first = w.steps[0]
            return {**out, "state": "ready", "step": 0, "target_w": _round(first.target_w),
                    "uses_ftp": w.uses_ftp()}
        if self.done:
            if strokes_since_end >= DONE_SHOW_STROKES:
                return None
            return {**out, "state": "done", "step": len(w.steps)}
        s = w.steps[self.step]
        kind, end = self._step_end(self.step)
        left = end - (now if kind == "time" else distance)
        nxt = w.steps[self.step + 1] if self.step + 1 < len(w.steps) else None
        target = s.target_w
        # Seconds to the end of the step, for the screens' countdown to the next one: on the clock for
        # a time step; for a distance step, the metres left at the speed the recent watts give
        # (watts = 2.8 x (m/s)^3, Concept2's formula, which SmartRow's pace matches), None while not rowing.
        if kind == "time":
            left_s = max(0.0, left)
        else:
            left_s = max(0.0, left) / (recent_power / 2.8) ** (1 / 3) if recent_power and recent_power > 0 else None
        out.update({
            "state": "running", "step": self.step + 1, "kind": kind, "length": s.length,
            "left": max(0, round(left)), "elapsed_s": round(now - self.started_at),
            "left_s": round(left_s, 2) if left_s is not None else None,
            "step_elapsed_s": round(now - self.step_started[self.step][0], 2),
            "target_w": _round(target), "low_w": _round(s.low_w), "high_w": _round(s.high_w),
            "intensity": s.intensity, "step_name": s.name,
            "power_w": _round(recent_power),
            "on_target": None if target is None or recent_power is None else
            ("on" if s.low_w - tolerance(s.low_w) <= recent_power <= s.high_w + tolerance(s.high_w)
             else "under" if recent_power < s.low_w else "over"),
            "next": {"target_w": _round(nxt.target_w), "kind": nxt.kind, "length": nxt.length} if nxt else None,
        })
        return out

    # ------------------------------------------------------------ after

    def result(self, stroke_log: list, first_time: float | None) -> dict | None:
        """Each step against its target, from the session's stroke log
        (time, distance, power, spm, heart rate, length, ...)."""
        if not self.started:
            return None
        t0 = first_time if first_time is not None else self.started_at
        bounds = list(self.step_started)
        steps = []
        for i, start in enumerate(bounds):
            end = bounds[i + 1] if i + 1 < len(bounds) else (self.ended_at, self.ended_distance) if self.done else None
            s = self.workout.steps[i]
            stop = end[0] if end else (stroke_log[-1][0] if stroke_log else start[0])
            rows = [r for r in stroke_log if start[0] <= r[0] < stop]
            judged = [r for r in rows if r[0] >= start[0] + STEP_SETTLE_S] or rows
            d = {"step": i + 1, "kind": s.kind, "length": s.length, "intensity": s.intensity,
                 "start_s": round(start[0] - t0, 1), "end_s": round(stop - t0, 1), "complete": end is not None,
                 "target_w": _round(s.target_w), "low_w": _round(s.low_w), "high_w": _round(s.high_w),
                 "strokes": len(rows)}
            if judged:
                power = [r[2] for r in judged]
                d.update({"power_w": round(statistics.fmean(power)),
                          "spm": round(statistics.fmean(r[3] for r in judged), 1),
                          "heart_rate_end": next((r[4] for r in reversed(rows) if r[4]), None)})
                lengths = [r[5] for r in judged if r[5]]
                if lengths:
                    d["length_cm"] = round(statistics.fmean(lengths))
                if s.target_w is not None:
                    lo, hi = s.low_w - tolerance(s.low_w), s.high_w + tolerance(s.high_w)
                    d["on_target_pct"] = round(sum(lo <= p <= hi for p in power) * 100 / len(power))
                    avg = d["power_w"]
                    d["verdict"] = "on" if lo <= avg <= hi else "under" if avg < lo else "over"
                    d["off_pct"] = round((avg / s.target_w - 1) * 100)
            steps.append(d)
        targeted = [d for d in steps if "verdict" in d]
        return {
            "name": self.workout.name,
            "steps": steps,
            "planned_steps": len(self.workout.steps),
            "complete": self.done,
            "ftp": round(self.ftp) if self.ftp and self.workout.uses_ftp() else None,
            "tolerance": {"pct": TOLERANCE_PCT, "min_w": TOLERANCE_MIN_W, "settle_s": STEP_SETTLE_S},
            "on_target": sum(d["verdict"] == "on" for d in targeted),
            "targeted": len(targeted),
        }


def _round(v):
    return None if v is None else round(v)


def findings(result: dict | None) -> list[str]:
    """One or two lines for the session's findings."""
    if not result or not result.get("steps"):
        return []
    steps = result["steps"]
    done = len([d for d in steps if d["complete"]])
    head = f"Workout {result['name']}: "
    head += f"all {result['planned_steps']} steps" if result["complete"] else \
        f"{done} of {result['planned_steps']} steps (stopped early)"
    targeted = [d for d in steps if "verdict" in d]
    if not targeted:
        return [head + "."]
    on = [d for d in targeted if d["verdict"] == "on"]
    out = [f"{head}; on target in {len(on)} of {len(targeted)} "
           f"(within {TOLERANCE_PCT:g}% after the first {STEP_SETTLE_S:g} s of each)."]
    off = [d for d in targeted if d["verdict"] != "on"]
    if off:
        parts = [f"step {d['step']} {d['power_w']} W for {d['target_w']} ({d['off_pct']:+d}%)".replace("-", "−")
                 for d in off[:4]]
        out.append(f"Off target: {', '.join(parts)}{' and more' if len(off) > 4 else ''}.")
    return out


class Folder:
    """The workouts folder: arms the coach with a workout file put there (the newest), cancels it if the
    file goes before the row starts, and moves it to done/ once it has started (so it runs once)."""

    CHECK_S = 3.0

    def __init__(self, directory: str) -> None:
        from pathlib import Path
        self.dir = Path(directory)
        self.file = None  # the file the armed workout came from
        self.player: Player | None = None
        self._next_check = 0.0
        self._bad: set = set()  # (name, mtime) of files that couldn't be read: not tried again
        self._missing_logged = False

    def tick(self, coach, now: float) -> None:
        if now < self._next_check:
            return
        self._next_check = now + self.CHECK_S
        if self.player is not None:
            if coach.workout is not self.player:  # dropped with its session
                self.player = self.file = None
            elif self.player.started:
                if self.file is not None:
                    self._archive()
                return
            elif not self.file.exists():
                log.info("Workout %s cancelled (its file was removed)", self.player.workout.name)
                coach.workout = None
                self.player = self.file = None
                return
            else:
                return
        if coach.workout is not None:
            return  # a workout from this session (done, or set some other way): one per session
        try:
            files = sorted((p for p in self.dir.glob("*.fit") if p.is_file()), key=lambda p: p.stat().st_mtime)
        except OSError:
            files = []
        if not self.dir.is_dir() and not self._missing_logged:
            log.info("Planned workouts: no folder %s (install.sh makes it)", self.dir)
            self._missing_logged = True
        for path in reversed(files):
            key = (path.name, path.stat().st_mtime)
            if key in self._bad:
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    raise WorkoutError("too big for a workout file")
                plan = read_fit(str(path), fallback_name=path.stem)
            except (WorkoutError, OSError) as exc:
                log.warning("Workout file %s can't be followed: %s", path.name, exc)
                self._bad.add(key)
                continue
            self.player, self.file = Player(plan), path
            coach.workout = self.player
            log.info("Workout %s ready (%s, %d steps%s): it starts with your next stroke", plan.name, path.name,
                     len(plan.steps), f", {round(plan.total_s / 60)} min" if plan.timed else "")
            return

    def _archive(self) -> None:
        import time as _time
        done = self.dir / "done"
        try:
            done.mkdir(exist_ok=True)
            self.file.rename(done / f"{_time.strftime('%Y-%m-%dT%H%M')}-{self.file.name}")
        except OSError as exc:
            log.warning("Could not move workout file %s to done/: %s; delete it so it isn't followed again",
                        self.file, exc)
        self.file = None
