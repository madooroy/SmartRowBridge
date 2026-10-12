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

    def test_every_screen_draws_at_lcd_size(self):
        images = [
            screens.status_screen(None, NOW, 10.0),
            screens.status_screen(status("searching"), NOW, UPTIME, "host  10.0.0.2"),
            screens.status_screen(status(), NOW, UPTIME),
            screens.rowing_screen(status(rowing=True), NOW, UPTIME),
            screens.rowing_screen(None, NOW, UPTIME),
            screens.hold_screen(0.5), screens.switching_off_screen(), screens.final_screen("poweroff"),
        ]
        for image in images:
            self.assertEqual(image.size, (320, 240))


@unittest.skipIf(app is None, "Pillow not installed")
class ControllerTest(unittest.TestCase):
    def step(self, c, st, t, **buttons):
        return c.step(st, NOW + t, UPTIME, {**NO_BUTTONS, **buttons})

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
        self.assertFalse(self.step(c, status(), 0, A=True).switch_off)
        self.assertFalse(self.step(c, status(), 1.5, A=True).switch_off)
        frame = self.step(c, status(), 2.0, A=True)
        self.assertTrue(frame.switch_off)
        self.assertEqual(frame.led, (False, False, False))
        self.assertFalse(self.step(c, status(), 2.1).switch_off)  # only once
        self.assertTrue(c.switching_off)

    def test_letting_go_cancels(self):
        c = app.Controller(hold_s=2.0)
        self.step(c, status(), 0, A=True)
        self.step(c, status(), 1.5)  # released
        self.assertFalse(self.step(c, status(), 2.5, A=True).switch_off)  # timer restarted
        self.assertFalse(c.switching_off)

    def test_led(self):
        self.assertEqual(app.led_for(status(), NOW, UPTIME), (False, True, False))  # green
        self.assertEqual(app.led_for(status("searching"), NOW, UPTIME), (True, True, False))  # yellow
        self.assertEqual(app.led_for(None, NOW, UPTIME), (True, False, False))  # red


if __name__ == "__main__":
    unittest.main()
