import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import power_guard as pg  # noqa: E402


class FakeLaptop:
    """Stands in for Windows: a power source, a battery, a power mode per source, and the sleep block."""

    def __init__(self, ac=True, pct=80, modes=None, mode_ok=True):
        self.ac, self.pct, self.mode_ok = ac, pct, mode_ok
        self.modes = modes or {True: pg.BALANCED, False: pg.BALANCED}  # the user's own setting per source
        self.awake = False

    def status(self):
        return self.ac, self.pct

    def keep_awake(self, on):
        self.awake = on
        return True

    def get_mode(self):
        return self.modes[self.ac is not False] if self.mode_ok else None

    def set_mode(self, mode):
        if not self.mode_ok:
            return False
        self.modes[self.ac is not False] = mode
        return True


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def guard(laptop, clock=None, **kw):
    logs = []
    g = pg.PowerGuard(api=laptop, log=logs.append, clock=clock or Clock(), **kw)
    return g, logs


class PowerGuardTest(unittest.TestCase):
    def test_plugged_in_best_performance_no_sleep_then_put_back(self):
        lap = FakeLaptop(ac=True)
        g, logs = guard(lap)
        with g:
            self.assertTrue(lap.awake)
            self.assertEqual(lap.modes[True], pg.BEST_PERFORMANCE)
        self.assertFalse(lap.awake)
        self.assertEqual(lap.modes[True], pg.BALANCED)  # what the user had
        self.assertIn("plugged in: Best performance, sleep blocked until done", logs[0])
        self.assertIn("back to Balanced", logs[-1])

    def test_on_battery_above_threshold_is_fast_below_is_balanced(self):
        lap = FakeLaptop(ac=False, pct=60)
        g, logs = guard(lap, battery_min=50)
        with g:
            self.assertEqual(lap.modes[False], pg.BEST_PERFORMANCE)
        lap2 = FakeLaptop(ac=False, pct=40)
        g2, logs2 = guard(lap2, battery_min=50)
        with g2:
            self.assertEqual(lap2.modes[False], pg.BALANCED)
            self.assertTrue(lap2.awake)  # still no sleep
        self.assertIn("under 50%", logs2[0])

    def test_fast_on_battery_holds_until_the_floor_then_warns(self):
        lap, clock = FakeLaptop(ac=False, pct=55), Clock()
        g, logs = guard(lap, clock, battery_min=50, battery_floor=15)
        with g:
            for pct in (45, 30, 16):  # under the start threshold but above the floor: stays fast
                lap.pct, clock.t = pct, clock.t + 31
                g.check()
                self.assertEqual(lap.modes[False], pg.BEST_PERFORMANCE)
            lap.pct, clock.t = 15, clock.t + 31
            g.check()
            self.assertEqual(lap.modes[False], pg.BALANCED)
            lap.pct, clock.t = 14, clock.t + 31
            g.check()
        self.assertTrue(any("at or below 15%: plug in" in m for m in logs))
        self.assertEqual(sum("plug in" in m for m in logs), 1)  # said once

    def test_checks_at_most_every_30_s(self):
        lap, clock = FakeLaptop(ac=True), Clock()
        g, _ = guard(lap, clock)
        with g:
            lap.ac, lap.pct = False, 10
            clock.t = 10
            g.check()
            self.assertEqual(lap.modes[False], pg.BALANCED)  # not looked yet: untouched
            clock.t = 31
            g.check()
            self.assertEqual(lap.modes[False], pg.BALANCED)  # looked: low battery, Balanced

    def test_unplugged_mid_run_then_plugged_back(self):
        lap = FakeLaptop(ac=True, modes={True: pg.BALANCED, False: "961cc777-2547-4f9d-8174-7d86181b8a7a"})
        clock = Clock()
        g, logs = guard(lap, clock, battery_min=50)
        with g:
            lap.ac, lap.pct, clock.t = False, 70, 31  # unplugged, plenty of battery: fast
            g.check()
            self.assertEqual(lap.modes[False], pg.BEST_PERFORMANCE)
            lap.ac, clock.t = True, 62  # plugged back in
            g.check()
            self.assertEqual(lap.modes[True], pg.BEST_PERFORMANCE)
        self.assertEqual(lap.modes[True], pg.BALANCED)  # plugged in at the end: that source's own setting back
        self.assertTrue(any("on battery (70%)" in m for m in logs))

    def test_power_mode_not_allowed_still_blocks_sleep(self):
        lap = FakeLaptop(ac=True, mode_ok=False)
        g, logs = guard(lap)
        with g:
            self.assertTrue(lap.awake)
        self.assertIn("power mode unchanged", logs[0])
        self.assertIn("sleep allowed again", logs[-1])

    def test_desktop_without_battery_counts_as_plugged_in(self):
        lap = FakeLaptop(ac=None, pct=None)
        g, _ = guard(lap)
        with g:
            self.assertEqual(lap.modes[True], pg.BEST_PERFORMANCE)

    def test_put_back_even_when_the_job_fails(self):
        lap = FakeLaptop(ac=True)
        g, _ = guard(lap)
        with self.assertRaises(KeyboardInterrupt):
            with g:
                raise KeyboardInterrupt
        self.assertFalse(lap.awake)
        self.assertEqual(lap.modes[True], pg.BALANCED)

    def test_not_windows_does_nothing(self):
        if sys.platform == "win32":
            self.skipTest("this machine is Windows")
        logs = []
        with pg.PowerGuard(log=logs.append) as g:
            g.check()
        self.assertIn("not Windows", logs[0])


if __name__ == "__main__":
    unittest.main()
