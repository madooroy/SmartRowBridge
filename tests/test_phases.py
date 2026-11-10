import math
import unittest

from smartrow_bridge import phases, rhythm

FRONT, BACK = 110.0, 48.0


def stroke_plan(n=12, period=3.4, load=0.3, legs=0.9, body=0.25, parked=1.2, early_load=False):
    """Event times for n strokes: catch, flag on, legs flat, flag off, knees rise (seconds)."""
    out, t = [], 10.0
    for _ in range(n):
        catch = t
        on = catch - 0.1 if early_load else catch + load
        flat = catch + legs
        off = flat + body
        knees = off + parked
        out.append({"catch": catch, "on": on, "flat": flat, "off": off, "knees": knees})
        t += period
    return out


def seat_readings(plan, rate=100):
    """The lidar: whole centimetres, 100 a second; the seat eases out of the front and into the back."""
    readings = []
    t, end = plan[0]["catch"] - 2.0, plan[-1]["knees"] + 0.5
    while t <= end:
        cm = FRONT
        for i, s in enumerate(plan):
            nxt = plan[i + 1]["catch"] if i + 1 < len(plan) else s["knees"] + 2.0
            if s["catch"] <= t < s["flat"]:
                x = (t - s["catch"]) / (s["flat"] - s["catch"])
                cm = FRONT - (FRONT - BACK) * (1 - math.cos(math.pi * x)) / 2
            elif s["flat"] <= t < s["knees"]:
                cm = BACK
            elif s["knees"] <= t < nxt:
                x = (t - s["knees"]) / (nxt - s["knees"])
                cm = BACK + (FRONT - BACK) * (1 - math.cos(math.pi * x)) / 2
        readings.append((round(t, 3), float(round(cm))))
        t += 1.0 / rate
    return readings


def log_rows(plan, readings):
    """Stroke-log rows as the coach builds them: events worked out at each stroke record (off + 0.4 s)."""
    rows, prev_off, prev_legs = [], None, None
    for s in plan:
        rec_t = s["off"] + 0.4
        seen = [r for r in readings if r[0] <= rec_t]
        ev = phases.stroke_events(seen, s["on"], s["off"], prev_off, prev_legs)
        rows.append((rec_t, 0, 110.0, 60 / 3.4, 0, 120, (0,) * 24, 0.0, s["off"] - s["on"], 0.0, s["on"], s["off"],
                     ev["catch"] if ev else 0.0, ev["legs"] if ev else 0.0, (ev["knees_before"] or 0.0) if ev else 0.0))
        prev_off, prev_legs = s["off"], ev["legs"] if ev else None
    return rows


class PhasesTest(unittest.TestCase):
    def test_events_and_parts_of_each_stroke(self):
        plan = stroke_plan()
        rows = log_rows(plan, seat_readings(plan))
        for s, r in zip(plan, rows):
            self.assertAlmostEqual(r[phases.I_CATCH], s["catch"], delta=0.04)
            self.assertAlmostEqual(r[phases.I_LEGS], s["flat"], delta=0.04)
        for s, r in zip(plan[:-1], rows[1:]):
            self.assertAlmostEqual(r[phases.I_KNEES], s["knees"], delta=0.04)
        d = phases.durations(rows[4], rows[5])
        self.assertAlmostEqual(d["drive"], 0.9 + 0.25, delta=0.07)
        self.assertAlmostEqual(d["load"], 0.3, delta=0.07)
        self.assertAlmostEqual(d["legs"], 0.9, delta=0.06)  # cosine easing: each end ~0.03 s
        self.assertAlmostEqual(d["body"], 0.25, delta=0.04)
        self.assertAlmostEqual(d["recovery"], 3.4 - 1.15, delta=0.07)
        self.assertAlmostEqual(d["parked"], 1.2, delta=0.04)
        self.assertAlmostEqual(d["parked"] + d["slide"], d["recovery"], places=6)
        self.assertIsNone(phases.durations(None, rows[0])["recovery"])  # the first stroke has no recovery before it

    def test_arms_first_shows_as_load_before_the_catch(self):
        plan = stroke_plan(early_load=True)
        rows = log_rows(plan, seat_readings(plan))
        self.assertLess(phases.durations(rows[3], rows[4])["load"], 0)

    def test_no_seat_travel_or_a_gap_gives_nothing(self):
        plan = stroke_plan(n=3)
        flat = [(t / 100, 80.0) for t in range(0, 2000)]  # arms-only drill: the seat doesn't move
        self.assertIsNone(phases.stroke_events(flat, plan[1]["on"], plan[1]["off"], plan[0]["off"], None))
        readings = [r for r in seat_readings(plan) if not plan[1]["catch"] + 0.2 < r[0] < plan[1]["catch"] + 0.5]
        self.assertIsNone(phases.stroke_events(readings, plan[1]["on"], plan[1]["off"], plan[0]["off"], None))

    def test_rhythm_is_measured_with_the_seat(self):
        plan = stroke_plan(n=40)
        rows = log_rows(plan, seat_readings(plan))
        self.assertIsNone(rhythm.session_rhythm(rows, []))  # all inside the first two minutes (the warm-up)
        plan = stroke_plan(n=90)
        rows = log_rows(plan, seat_readings(plan))
        rh = rhythm.session_rhythm(rows, [])
        easy = rh["easy"]
        self.assertEqual(easy["source"], "seat")
        self.assertAlmostEqual(easy["drive_s"], 1.15, delta=0.06)
        self.assertAlmostEqual(easy["ratio"], (3.4 - 1.15) / 1.15, delta=0.12)
        self.assertEqual(len(easy["thirds"]), 3)
        text = " ".join(rhythm.rhythm_findings(rh))
        self.assertIn("timed with the seat sensor", text)
        self.assertIn("Stroke parts (Easy): legs 0.9 s (the handle taking load 0.3 s into it), body and arms 0.2 s",
                      text)
        self.assertIn("then parked 1.", text)
        self.assertNotIn("Over the row", text)  # nothing moved
        bar = rhythm.live(rows, now=rows[-1][0] + 1)
        self.assertEqual(bar["source"], "seat")
        self.assertAlmostEqual(bar["phases"]["parked"], 1.2, delta=0.12)

    def test_compared_with_the_last_row(self):
        easy = {"spm": 17.6, "drive_s": 1.15, "recovery_s": 2.25, "ratio": 1.96, "source": "seat",
                "phases": {"load": 0.3, "legs": 0.9, "body": 0.25, "drive": 1.15, "parked": 1.2, "slide": 1.05,
                           "recovery": 2.25}}
        last = {**easy, "ratio": 1.7, "phases": {**easy["phases"], "parked": 0.9, "load": 0.45}}
        text = " ".join(rhythm.rhythm_findings({"easy": easy, "efforts": [], "last": last}))
        self.assertIn("Since the last row (Easy): ratio 1:1.7 → 1:2, catch to load 0.45 → 0.30 s, "
                      "parked after the finish 0.90 → 1.20 s.", text)
        same = " ".join(rhythm.rhythm_findings({"easy": easy, "efforts": [], "last": easy}))
        self.assertIn("Stroke parts as in the last row.", same)
        faster = " ".join(rhythm.rhythm_findings({"easy": easy, "efforts": [], "last": {**last, "spm": 20.0}}))
        self.assertIn("Since the last row: rate 20 \u2192 18 spm in Easy rowing, so the parts aren't compared.", faster)

    def test_a_row_without_the_seat_still_gets_the_pulley_estimate(self):
        rows = [(t * 3.0 + 200, 0, 110.0, 20, 0, 120, (0,) * 24, 0.0, 0.82, 2.18) for t in range(60)]
        rh = rhythm.session_rhythm(rows, [])
        self.assertEqual(rh["easy"]["source"], "pulley")
        self.assertNotIn("phases", rh["easy"])



class ReplayToolTest(unittest.TestCase):
    def test_tool_times_a_saved_session(self):
        import csv
        import importlib.util
        import io
        import json
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path
        spec = importlib.util.spec_from_file_location("phases_replay", Path(__file__).parents[1] / "tools" / "phases_replay.py")
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        plan = stroke_plan(n=40)
        with tempfile.TemporaryDirectory() as d:
            sp = Path(d, "2026-11-10T0600.strokes.json")
            cols = ["t", "distance_m", "power_w", "spm", "heart_rate", "length_cm", "force_n", "drive_s",
                    "flag_drive_s", "flag_recovery_s", "flag_on_t", "flag_off_t"]
            sp.write_text(json.dumps({"id": "2026-11-10T0600", "columns": cols, "rows": [
                [s["off"] + 0.4, 0, 110.0, 17.6, 0, 120, [], None, s["off"] - s["on"], None, s["on"], s["off"]]
                for s in plan]}))
            with open(Path(d, "2026-11-10T0600.seat.csv"), "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["t", "cm", "strength"])
                for t, cm in seat_readings(plan):
                    w.writerow([t, int(cm), 5000])
            out = io.StringIO()
            with redirect_stdout(out):
                tool.main([str(sp), "--csv", str(Path(d, "out.csv"))])
            self.assertIn("40 of 40 strokes timed", out.getvalue())
            self.assertNotIn("older file", out.getvalue())
            lines = Path(d, "out.csv").read_text().splitlines()
            self.assertEqual(len(lines), 41)


def real_seat(plan, seed=1, rate=100):
    """A seat closer to 9 Nov's file: it slows into the front (about 1 cm per 50 ms at the end), sits
    there 0.25 s, pushes off quickly; reaches the back, creeps back 2 cm in the body swing, reads
    46/45/47 at rest (1 cm flicker), rolls 2 cm forward before the knees rise."""
    import random
    rnd = random.Random(seed)
    out, t = [], plan[0]["catch"] - 2.0
    end = plan[-1]["knees"] + 0.5
    while t <= end:
        cm = FRONT
        for i, s in enumerate(plan):
            nxt = plan[i + 1]["catch"] if i + 1 < len(plan) else s["knees"] + 2.0
            sit = 0.25
            if s["catch"] <= t < s["flat"]:  # quick push from rest: steady acceleration, then easing in
                x = (t - s["catch"]) / (s["flat"] - s["catch"])
                cm = FRONT - (FRONT - BACK) * (x * x * 2 if x < 0.5 else 1 - 2 * (1 - x) ** 2)
            elif s["flat"] <= t < s["knees"]:
                creep = min(2.0, 8.0 * (t - s["flat"])) if t < s["off"] else 2.0  # back 2 cm in the swing
                roll = min(2.0, max(0.0, (t - (s["knees"] - 0.4)) * 5)) if t > s["knees"] - 0.4 else 0.0
                cm = BACK - creep + roll + rnd.choice((0, 0, 0, -1, 1))
            elif s["knees"] <= t < nxt - sit:  # the slide, slowing into the front
                x = (t - s["knees"]) / (nxt - sit - s["knees"])
                base = BACK + 0.0
                cm = base + (FRONT - base) * (1 - (1 - x) ** 2)
            elif nxt - sit <= t < nxt:
                cm = FRONT + rnd.choice((0, 0, -1))
        out.append((round(t, 3), float(round(cm))))
        t += 1.0 / rate
    return out


class HarderSeatTest(unittest.TestCase):
    """The cases from the 10 Nov review: slow arrival and a sit at the front, flicker and creep at rest,
    a stroke missing from the log, the seat dropping out."""

    def test_slow_arrival_sit_creep_and_flicker(self):
        plan = stroke_plan(n=14)
        rows = log_rows(plan, real_seat(plan))
        for s, r in list(zip(plan, rows))[2:]:
            self.assertAlmostEqual(r[phases.I_CATCH], s["catch"], delta=0.06)  # the seat leaving, not mid-sit
            self.assertAlmostEqual(r[phases.I_LEGS], s["flat"], delta=0.08)  # not moved by the 2 cm creep
        for s, r in list(zip(plan[:-1], rows[1:]))[2:]:
            self.assertAlmostEqual(r[phases.I_KNEES], s["knees"], delta=0.1)  # not moved by the 2 cm roll

    def test_first_stroke_after_a_wait_at_the_front(self):
        plan = stroke_plan(n=3)
        readings = [(t, 110.0 + (1 if int(t * 100) % 7 == 0 else 0)) for t in [x / 100 for x in range(500, 1000)]]
        readings += [r for r in real_seat(plan) if r[0] >= 10.0]
        ev = phases.stroke_events(readings, plan[0]["on"], plan[0]["off"], None, None)
        self.assertAlmostEqual(ev["catch"], plan[0]["catch"], delta=0.08)

    def test_a_missing_stroke_is_not_paired_across(self):
        plan = stroke_plan(n=8)
        rows = log_rows(plan, seat_readings(plan))
        # this stroke's own count of the time since the last finish (flag recovery)
        rows = [r[:9] + ((plan[i]["on"] - plan[i - 1]["off"]) if i else 0.0,) + r[10:] for i, r in enumerate(rows)]
        gap = rows[:4] + rows[5:]  # stroke 5 never reached the log
        self.assertIsNotNone(phases.durations(gap[2], gap[3])["recovery"])
        self.assertIsNone(phases.durations(gap[3], gap[4])["recovery"])  # not 4 s of "recovery"

    def test_seat_dropping_out_mid_row_keeps_the_screens_going(self):
        plan = stroke_plan(n=10)
        rows = log_rows(plan, seat_readings(plan))
        rows += [(r[0] + 40, *r[1:12], 0.0, 0.0, 0.0) for r in rows[-3:]]  # arms only / lidar gone
        bar = rhythm.live(rows, now=rows[-1][0] + 1)
        self.assertTrue(bar is None or bar["source"] in ("pulley", "seat"))
        self.assertEqual(phases.medians([None, {"drive": 1.0}])["drive"], 1.0)


class BridgeWithSeatTest(unittest.TestCase):
    def test_bridge_starts_with_the_coach_and_seat_sensor(self):
        import tempfile
        from smartrow_bridge import bridge, config
        with tempfile.TemporaryDirectory() as d:
            cfg = config.parse(["--coach", "--session-dir", d, "--status-file", f"{d}/status.json",
                                "--seat-sensor", "/dev/null-seat"])
            b = bridge.Bridge(cfg)
            self.assertIsNotNone(b.seat)
            self.assertEqual(b.coach.seat_window(0.0, 1.0), [])
            self.assertIsInstance(b._bridge_drives_pulley, bool)


if __name__ == "__main__":
    unittest.main()
