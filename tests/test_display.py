import unittest

try:
    from smartrow_bridge.display import app, screens
except ImportError:  # Pillow is only installed with the optional screen
    app = screens = None

NOW, UPTIME = 1_000_000.0, 600.0
NO_BUTTONS = {"A": False, "B": False, "X": False, "Y": False}


def status(stage="connected", rowing=False, age=0.0):
    return {"version": 1, "time": NOW - age, "stage": stage, "pulley": "SmartRow-34", "smartrow_app": True,
            "fitness_app": False, "clone_name": "SmartRow", "fitness_name": "PiRower", "rowing": rowing,
            "metrics": {"distance_m": 500, "power_w": 150 if rowing else 0, "stroke_rate_spm": 24.0 if rowing else 0,
                        "stroke_count": 50, "pace_s_per_500m": 120 if rowing else 0, "elapsed_s": 130}}


CURVE = [100, 200, 300, 400, 500, 520, 500, 450, 400, 350, 300, 250, 200, 160, 120, 90, 60, 40, 20, 10, 0, 0, 0, 0]
LAST = {"index": 2, "duration_s": 62, "strokes": 22, "power_w": 150, "stroke_rate_spm": 21.5, "length_cm": 118.7,
        "length_delta_cm": 4.6, "peak_n": 623, "finish_pct": 0, "rating": "good", "fade_pct": -3, "curve": CURVE,
        "steady_curve": CURVE}
LIVE = {"index": 3, "elapsed_s": 72, "strokes": 25, "power_w": 154, "length_cm": 112, "cue": "finish",
        "finish_pct": -15, "length_delta_cm": -3, "curve": CURVE, "steady_curve": CURVE}


def coached(live=None, last=None, show_summary=False, age=0.0):
    st = status(rowing=True, age=age)
    st["coach"] = {"mode": "interval" if live else "steady", "steady_power_w": 111, "intervals": 2 if last else 0,
                   "live": live, "last": last, "show_summary": show_summary}
    return st


@unittest.skipIf(screens is None, "Pillow not installed")
class ScreensTest(unittest.TestCase):
    def test_bridge_state(self):
        self.assertEqual(screens.bridge_state(status(), NOW, UPTIME), "ok")
        self.assertEqual(screens.bridge_state(status(age=10), NOW, UPTIME), "down")
        self.assertEqual(screens.bridge_state(None, NOW, 20.0), "starting")
        self.assertEqual(screens.bridge_state(None, NOW, UPTIME), "down")

    def test_formats(self):
        self.assertEqual(screens.format_pace(118), "1:58")
        self.assertEqual(screens.format_pace(0), "–")
        self.assertEqual(screens.format_time(455), "7:35")
        self.assertEqual(screens.format_time(3723), "1:02:03")

    def test_address_lines(self):
        from smartrow_bridge.display.__main__ import address_lines
        self.assertEqual(address_lines("raspberrypi", "192.168.1.23", "8080"),
                         "http://raspberrypi.local:8080\nhttp://192.168.1.23:8080")
        self.assertEqual(address_lines("raspberrypi", None, "8080"), "http://raspberrypi.local:8080")
        self.assertEqual(address_lines("raspberrypi", "192.168.1.23", None), "raspberrypi  192.168.1.23")
        self.assertEqual(address_lines("raspberrypi", None, None), "raspberrypi")
        two = screens.status_screen(status(), NOW, UPTIME, address_lines("raspberrypi", "192.168.1.23", "8080"))
        self.assertEqual(two.size, (320, 240))

    def test_finish_colours_match_the_coach(self):
        from smartrow_bridge import coach
        self.assertEqual(screens.GOOD_FINISH_PCT, round((coach.GOOD_FINISH - 1) * 100))
        self.assertEqual(screens.OK_FINISH_PCT, round((coach.OK_FINISH - 1) * 100))
        self.assertEqual(screens.finish_colour(2), screens.GREEN)
        self.assertEqual(screens.finish_colour(-8), screens.AMBER)
        self.assertEqual(screens.finish_colour(-15), screens.RED)
        self.assertEqual(screens.finish_colour(None), screens.DIM)

    def test_every_screen_draws_at_lcd_size(self):
        images = [
            screens.status_screen(None, NOW, 10.0),
            screens.status_screen(status("searching"), NOW, UPTIME, "host  10.0.0.2"),
            screens.status_screen(status(), NOW, UPTIME),
            screens.rowing_screen(status(rowing=True), NOW, UPTIME),
            screens.rowing_screen(None, NOW, UPTIME),
            screens.hold_screen(0.5), screens.switching_off_screen(), screens.final_screen("poweroff"),
            screens.interval_screen(coached(live=LIVE), NOW, UPTIME),
            screens.interval_screen(coached(live={**LIVE, "cue": None, "finish_pct": None, "curve": [],
                                                  "steady_curve": []}), NOW, UPTIME),
            screens.interval_screen(coached(live={**LIVE, "finish_pct": -60}), NOW, UPTIME),  # off the scale
            screens.summary_screen(coached(last=LAST, show_summary=True), NOW, UPTIME),
            screens.summary_screen(coached(last={**LAST, "rating": "short", "finish_pct": -15}), NOW, UPTIME),
            screens.summary_screen(coached(last={**LAST, "legs_late": True, "finish_pct": 14}), NOW, UPTIME),
            screens.interval_screen(coached(live={**LIVE, "cue": "legs", "finish_pct": 14}), NOW, UPTIME),
            screens.rowing_screen(coached(last=LAST), NOW, UPTIME),
        ]
        for image in images:
            self.assertEqual(image.size, (320, 240))


@unittest.skipIf(app is None, "Pillow not installed")
class ControllerTest(unittest.TestCase):
    def step(self, c, st, t, **buttons):
        return c.step(st, NOW + t, UPTIME, {**NO_BUTTONS, **buttons})

    def test_interval_coach_screens(self):
        c = app.Controller()
        self.step(c, coached(), 0)
        self.assertEqual(c.screen, "rowing")
        self.step(c, coached(live=LIVE), 1)
        self.assertEqual(c.screen, "interval")
        self.step(c, coached(last=LAST, show_summary=True), 2)
        self.assertEqual(c.screen, "summary")
        self.step(c, coached(last=LAST, show_summary=False), 3)
        self.assertEqual(c.screen, "rowing")

    def test_x_recalls_and_closes_the_summary(self):
        c = app.Controller()

        def at(t, show_summary=False, **buttons):
            self.step(c, coached(last=LAST, show_summary=show_summary, age=-t), t, **buttons)  # fresh status
            return c.screen

        self.assertEqual(at(0), "rowing")
        self.assertEqual(at(1, X=True), "summary")
        self.assertEqual(at(2, X=True), "summary")  # still held: no change
        self.assertEqual(at(3 + app.RECALL_S), "rowing")  # recall times out
        self.assertEqual(at(30, show_summary=True), "summary")
        self.assertEqual(at(31, show_summary=True, X=True), "rowing")  # closed early
        self.assertEqual(at(32, show_summary=True), "rowing")

    def test_rowing_switches_view_and_back_after_a_minute(self):
        c = app.Controller()
        self.step(c, status(), 0)
        self.assertEqual(c.view, "status")
        self.step(c, status(rowing=True), 1)
        self.assertEqual(c.view, "rowing")
        self.step(c, status(rowing=False), 30)
        self.assertEqual(c.view, "rowing")
        self.step(c, status(rowing=False), 62)
        self.assertEqual(c.view, "status")

    def test_b_toggles_until_rowing_changes(self):
        c = app.Controller()
        self.step(c, status(), 0, B=True)
        self.assertEqual(c.view, "rowing")
        self.step(c, status(), 0.1, B=True)  # still held: no second toggle
        self.assertEqual(c.view, "rowing")
        self.step(c, status(), 0.2)
        self.step(c, status(), 0.3, B=True)
        self.assertEqual(c.view, "status")
        self.step(c, status(rowing=True), 1)  # rowing starts: automatic again
        self.assertEqual(c.view, "rowing")

    def test_hold_a_to_switch_off(self):
        c = app.Controller(hold_s=2.0)
        self.step(c, status(), -1)  # A seen released first
        self.assertFalse(self.step(c, status(), 0, A=True).switch_off)
        self.assertFalse(self.step(c, status(), 1.5, A=True).switch_off)
        frame = self.step(c, status(), 2.0, A=True)
        self.assertTrue(frame.switch_off)
        self.assertIsNone(frame.led)
        self.assertFalse(self.step(c, status(), 2.1).switch_off)  # only once
        self.assertTrue(c.switching_off)

    def test_a_pressed_from_start_never_switches_off(self):
        c = app.Controller(hold_s=2.0)
        for t in (0, 1, 2, 3, 10):
            self.assertFalse(self.step(c, status(), t, A=True).switch_off)
        self.assertFalse(c.switching_off)
        self.step(c, status(), 11)  # released once: from now on a hold counts
        self.step(c, status(), 12, A=True)
        self.assertTrue(self.step(c, status(), 14, A=True).switch_off)

    def test_letting_go_cancels(self):
        c = app.Controller(hold_s=2.0)
        self.step(c, status(), -1)
        self.step(c, status(), 0, A=True)
        self.step(c, status(), 1.5)  # released
        self.assertFalse(self.step(c, status(), 2.5, A=True).switch_off)  # timer restarted
        self.assertFalse(c.switching_off)

    def test_led(self):
        self.assertEqual(app.led_for(status(), NOW, UPTIME), "green")
        self.assertEqual(app.led_for(status("searching"), NOW, UPTIME), "red")
        self.assertEqual(app.led_for(None, NOW, UPTIME), "red")
        self.assertIsNone(app.led_for(status(), NOW, UPTIME, rowing_recently=True))

    def test_led_off_while_rowing_and_green_a_minute_after(self):
        c = app.Controller()
        self.assertEqual(self.step(c, status(), 0).led, "green")
        self.assertIsNone(self.step(c, status(rowing=True), 1).led)
        self.assertIsNone(self.step(c, status(rowing=False, age=-30), 30).led)
        self.assertEqual(self.step(c, status(rowing=False, age=-62), 62).led, "green")  # status file still fresh

if __name__ == "__main__":
    unittest.main()
