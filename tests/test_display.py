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

    def test_status_screen_heart_rate_row(self):
        from smartrow_bridge.display import screens
        base = {"version": 1, "time": 1000.0, "stage": "connected", "pulley": "SmartRow-34", "smartrow_app": True,
                "fitness_app": True, "fitness_name": "PiRower", "metrics": {"heart_rate_bpm": 96}}
        for hr in (None, {"name": None, "connected": False}, {"name": "COROS HRM 8A2F", "connected": True}):
            image = screens.status_screen({**base, "heart_rate": hr}, 1000.0, 600.0, "a\nb")
            self.assertEqual(image.size, (screens.WIDTH, screens.HEIGHT))
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

    def test_easy_screen_rhythm_bar(self):
        st = coached()
        st["coach"].update(stroke_curve=[0, 200, 400, 300, 100, 0], steady_curve=[0, 210, 390, 290, 90, 0],
                           cue="good")
        plain = screens.rowing_screen(st, NOW, UPTIME)
        st["coach"]["rhythm"] = {"drive_s": 1.3, "recovery_s": 1.7, "ratio": 1.31, "aim": 2.0}
        with_bar = screens.rowing_screen(st, NOW, UPTIME)
        self.assertEqual(with_bar.size, (320, 240))
        self.assertNotEqual(plain.tobytes(), with_bar.tobytes())
        # the drive's blue runs from the left edge to 1.3 / 3.0 of the bar, past the 1:2 tick at 1/3
        px = with_bar.getpixel((4 + int(312 * 0.40), 172))
        self.assertEqual(px, screens.BLUE)
        self.assertNotEqual(with_bar.getpixel((4 + int(312 * 0.50), 172)), screens.BLUE)


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
        self.assertEqual(at(1, X=True), "rowing")  # a tap counts when let go
        self.assertEqual(at(1.2), "summary")
        self.assertEqual(at(3 + app.RECALL_S), "rowing")  # recall times out
        self.assertEqual(at(30, show_summary=True), "summary")
        at(31, show_summary=True, X=True)
        self.assertEqual(at(31.2, show_summary=True), "rowing")  # closed early
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
        self.step(c, status(), -1)  # B seen released first
        self.step(c, status(), 0, B=True)
        self.step(c, status(), 0.1, B=True)  # a tap switches on release
        self.assertEqual(c.view, "status")
        self.step(c, status(), 0.2)
        self.assertEqual(c.view, "rowing")
        self.step(c, status(), 0.3, B=True)
        self.step(c, status(), 0.4)
        self.assertEqual(c.view, "status")
        self.step(c, status(rowing=True), 1)  # rowing starts: automatic again
        self.assertEqual(c.view, "rowing")

    def test_hold_b_for_drills(self):
        c = app.Controller()
        self.step(c, status(), -1)
        self.step(c, status(), 0, B=True)
        self.assertIsNone(self.step(c, status(), 1.0, B=True).request)  # the hold screen
        frame = self.step(c, status(), 2.0, B=True)
        self.assertEqual((frame.request["action"], frame.request["on"]), ("drill", True))
        self.assertIsNone(self.step(c, status(), 2.5, B=True).request)  # only once
        self.step(c, status(), 3.0)  # let go: no screen switch
        on = lambda t: {**status(age=-t), "drill": True}  # noqa: E731
        self.step(c, on(3.1), 3.1)  # any button or a few seconds closes the drills screen
        self.step(c, on(3.2), 3.2, B=True)
        self.assertFalse(self.step(c, on(5.2), 5.2, B=True).request["on"])  # held again: off

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

def session_status(t, strokes=120, uploads=None, finish=None, live=None):
    """A fresh status at NOW + t, with a session of `strokes` strokes and the uploads' state."""
    st = coached(live=live, age=-t)
    st["coach"]["session_strokes"] = strokes
    st["uploads"] = uploads
    st["finish"] = finish
    return st


def ups(states, waiting=0, sending=None, ack=None, retrying=0):
    return {"configured": list(states), "sending": sending, "waiting": waiting, "retrying": retrying,
            "error": None, "latest": {"id": "2026-10-28T0702", "dests": states}, "ack": ack}


@unittest.skipIf(screens is None, "Pillow not installed")
class DoneAndOffTest(unittest.TestCase):
    def step(self, c, st, t, **buttons):
        return c.step(st, NOW + t, UPTIME, {**NO_BUTTONS, **buttons})

    def test_y_tab_only_with_a_session(self):
        c = app.Controller()
        self.step(c, session_status(0, strokes=10), 0)
        self.assertEqual(c.screen, "rowing")
        # (the tab itself is drawn; here: the screen draws without error either way)
        self.step(c, session_status(1, strokes=120), 1)

    def test_hold_y_finishes_and_shows_the_uploads(self):
        c = app.Controller()
        self.step(c, session_status(0), 0)  # Y seen released
        self.assertIsNone(self.step(c, session_status(1), 1, Y=True).request)  # holding
        frame = self.step(c, session_status(2), 2.0, Y=True)
        self.assertEqual(frame.request["action"], "finish")
        token = frame.request["token"]
        self.assertIsNone(self.step(c, session_status(2.5), 2.5, Y=True).request)  # once per hold
        # The bridge answers: finished, Strava sending, intervals.icu sent.
        answered = {"token": token, "session": "2026-10-28T0702"}
        st = session_status(4, strokes=0, finish=answered,
                            uploads=ups({"intervals.icu activity": "sent", "Strava": "waiting"}, waiting=1,
                                        sending="Strava", ack=token))
        self.step(c, st, 4)
        self.assertIsNotNone(c._finish)
        done = session_status(6, strokes=0, finish=answered,
                              uploads=ups({"intervals.icu activity": "sent", "Strava": "sent"}, ack=token))
        self.step(c, done, 6)
        self.assertIsNotNone(c._finish)  # stays a while to be read
        self.step(c, done, 6 + app.FINISH_SHOW_S + 1)
        self.assertIsNone(c._finish)

    def test_finishing_does_not_wait_on_an_upload_already_in_flight(self):
        c = app.Controller()
        self.step(c, session_status(0), 0)
        self.step(c, session_status(1), 1, Y=True)
        frame = self.step(c, session_status(2), 2.0, Y=True)
        token = frame.request["token"]
        # The bridge has finished the session, but the uploader is mid-send and hasn't echoed the token.
        st = session_status(4, strokes=0, finish={"token": token, "session": "2026-10-28T0702"},
                            uploads=ups({"Strava": "waiting"}, waiting=1, sending="Strava", ack=None))
        self.step(c, st, 4)
        self.assertIsNotNone(c._finish)
        st["time"] = NOW + 3 + app.ACK_WAIT_S
        self.step(c, st, 3 + app.ACK_WAIT_S)
        self.assertIsNotNone(c._finish)  # still showing: the uploads, not "Finishing…" (drawn without error)

    def test_x_closes_the_finished_screen(self):
        c = app.Controller()
        self.step(c, session_status(0), 0)
        self.step(c, session_status(1), 1.0, Y=True)
        frame = self.step(c, session_status(2), 2.0, Y=True)
        self.assertIsNotNone(frame.request)
        self.step(c, session_status(3), 3)
        self.step(c, session_status(4), 4, X=True)
        self.assertIsNone(c._finish)
        self.assertNotEqual(c.screen, "summary")  # the X press closed this, not recalled a summary

    def test_a_with_uploads_to_go_uploads_then_switches_off(self):
        c = app.Controller(hold_s=2.0)
        waiting = ups({"Strava": "later"}, waiting=1)
        self.step(c, session_status(-1, uploads=waiting), -1)
        self.step(c, session_status(0, uploads=waiting), 0, A=True)
        frame = self.step(c, session_status(2, uploads=waiting), 2.0, A=True)
        self.assertFalse(frame.switch_off)
        self.assertEqual(frame.request["action"], "finish")
        token = frame.request["token"]
        self.assertFalse(self.step(c, session_status(3, uploads=waiting), 3, A=True).switch_off)  # still held
        sending = ups({"Strava": "waiting"}, waiting=1, sending="Strava", ack=token)
        fin = {"token": token, "session": "2026-10-28T0702"}
        self.assertFalse(self.step(c, session_status(5, strokes=0, uploads=sending, finish=fin), 5).switch_off)
        sent = ups({"Strava": "sent"}, ack=token)
        self.assertFalse(self.step(c, session_status(8, strokes=0, uploads=sent, finish=fin), 8).switch_off)
        self.assertTrue(self.step(c, session_status(8 + app.OFF_CLOSING_S, strokes=0, uploads=sent, finish=fin),
                                  8 + app.OFF_CLOSING_S).switch_off)

    def test_a_again_switches_off_at_once_and_b_cancels(self):
        waiting = ups({"Strava": "waiting"}, waiting=1)
        c = app.Controller(hold_s=2.0)
        self.step(c, session_status(-1, uploads=waiting), -1)
        self.step(c, session_status(0, uploads=waiting), 0, A=True)
        self.step(c, session_status(2, uploads=waiting), 2, A=True)
        self.step(c, session_status(3, uploads=waiting), 3)  # let go
        self.assertTrue(self.step(c, session_status(4, uploads=waiting), 4, A=True).switch_off)

        c = app.Controller(hold_s=2.0)
        self.step(c, session_status(-1, uploads=waiting), -1)
        self.step(c, session_status(0, uploads=waiting), 0, A=True)
        self.step(c, session_status(2, uploads=waiting), 2, A=True)
        self.step(c, session_status(3, uploads=waiting), 3, B=True)
        self.assertIsNone(c._off)
        self.assertFalse(c.switching_off)

    def test_a_offline_switches_off_after_saying_so(self):
        c = app.Controller(hold_s=2.0)
        waiting = ups({"Strava": "waiting"}, waiting=1)
        self.step(c, session_status(-1, uploads=waiting), -1)
        self.step(c, session_status(0, uploads=waiting), 0, A=True)
        token = self.step(c, session_status(2, uploads=waiting), 2, A=True).request["token"]
        offline = ups({"Strava": "retrying"}, retrying=1, ack=token)
        fin = {"token": token, "session": None}
        self.assertFalse(self.step(c, session_status(4, uploads=offline, finish=fin), 4).switch_off)
        self.assertTrue(self.step(c, session_status(8, uploads=offline, finish=fin), 8).switch_off)

    def test_a_without_uploads_set_up_switches_off_as_before(self):
        c = app.Controller(hold_s=2.0)
        self.step(c, session_status(-1, uploads=ups({})), -1)
        self.step(c, session_status(0, uploads=ups({})), 0, A=True)
        self.assertTrue(self.step(c, session_status(2, uploads=ups({})), 2, A=True).switch_off)

    def test_hold_x_discards(self):
        c = app.Controller()
        self.step(c, session_status(0), 0)  # X seen released
        self.step(c, session_status(1), 1, X=True)
        self.assertIsNone(self.step(c, session_status(1.4), 1.4, X=True).request)  # tap length: nothing yet
        frame = self.step(c, session_status(2), 2.0, X=True)  # past a tap: the hold shows
        self.assertIsNone(frame.request)
        frame = self.step(c, session_status(3), 3.0, X=True)
        self.assertEqual(frame.request["action"], "discard")
        token = frame.request["token"]
        self.assertIsNone(self.step(c, session_status(3.5), 3.5, X=True).request)  # once per hold
        done = session_status(4, strokes=0, finish={"token": token, "session": "s", "action": "discard",
                                                    "uploaded": ["strava"]})
        self.step(c, done, 4)
        self.assertIsNotNone(c._discard)
        self.step(c, done, 4 + app.DISCARD_SHOW_S + 1)
        self.assertIsNone(c._discard)
        self.assertNotEqual(c.screen, "summary")  # the long press was not a tap

    def test_x_tap_still_recalls_with_a_session(self):
        c = app.Controller()
        st = lambda t: {**session_status(t), "coach": {**session_status(t)["coach"], "last": LAST}}  # noqa: E731
        self.step(c, st(0), 0)
        self.step(c, st(1), 1, X=True)
        self.step(c, st(1.3), 1.3)
        self.assertEqual(c.screen, "summary")

    def test_new_screens_draw(self):
        st = session_status(0, uploads=ups({"intervals.icu activity": "sent", "Strava": "retrying"}, retrying=1),
                            finish={"token": "t", "session": "x"})
        for image in (screens.finish_screen(st, "x", True), screens.finish_screen(st, None, True),
                      screens.finish_screen(st, None, False), screens.upload_off_screen(st, False),
                      screens.upload_off_screen(st, True), screens.hold_screen(0.5, "Keep holding: done rowing"),
                      screens.discard_screen(st, False),
                      screens.discard_screen({"finish": {"session": "x", "uploaded": ["strava"]}}, True),
                      screens.discard_screen({"finish": {"session": None}}, True)):
            self.assertEqual(image.size, (screens.WIDTH, screens.HEIGHT))


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(screens is None, "Pillow not installed")
class SleepTest(unittest.TestCase):
    def step(self, c, st, t, **buttons):
        return c.step(st, NOW + t, UPTIME + t, {**NO_BUTTONS, **buttons})

    def fresh(self, t, **kw):
        """Nothing connected (the pulley asleep, no apps, no strap) unless asked."""
        st = status(age=-t, stage=kw.pop("stage", "searching"), **kw)
        st["smartrow_app"] = False
        return st

    def test_goes_dark_when_nothing_happens_and_rowing_wakes_it(self):
        c = app.Controller(sleep_s=300)
        self.assertTrue(self.step(c, self.fresh(0), 0).screen_on)
        self.assertTrue(self.step(c, self.fresh(299), 299).screen_on)
        f = self.step(c, self.fresh(300), 300)
        self.assertFalse(f.screen_on)
        self.assertEqual(f.led, "red")  # the LED stays on (red: no pulley): asleep, not off
        self.assertFalse(self.step(c, self.fresh(900), 900).screen_on)
        self.assertTrue(self.step(c, self.fresh(901, rowing=True), 901).screen_on)
        self.assertEqual(c.view, "rowing")

    def test_rowing_keeps_it_on(self):
        c = app.Controller(sleep_s=300)
        for t in range(0, 1200, 30):
            self.assertTrue(self.step(c, self.fresh(t, rowing=True), t).screen_on)

    def test_the_waking_press_does_nothing_else(self):
        c = app.Controller(sleep_s=300)
        self.step(c, self.fresh(0), 0)
        self.assertFalse(self.step(c, self.fresh(400), 400).screen_on)
        for t in (401, 402, 403, 404):  # A held long enough to switch off, but it woke the screen
            f = self.step(c, self.fresh(t), t, A=True)
            self.assertTrue(f.screen_on)
            self.assertFalse(f.switch_off)
        self.assertFalse(c.switching_off)
        self.step(c, self.fresh(405), 405)  # let go: buttons work again
        self.step(c, self.fresh(406), 406, B=True)
        self.step(c, self.fresh(406.2), 406.2)
        self.assertEqual(c.view, "rowing")

    def test_uploads_still_going_keep_it_on(self):
        c = app.Controller(sleep_s=300)
        st = lambda t: {**self.fresh(t), "uploads": {"sending": "strava", "waiting": 1}}
        for t in range(0, 900, 60):
            self.assertTrue(self.step(c, st(t), t).screen_on)

    def test_a_clock_jump_after_boot_does_not_darken_it(self):
        # The Pi has no clock battery: the time jumps forward by hours once it syncs.
        c = app.Controller(sleep_s=300)
        idle = status(stage="searching")
        idle["smartrow_app"] = False
        self.assertTrue(c.step(idle, 1000.0, 20.0, NO_BUTTONS).screen_on)
        st = dict(idle)
        st["time"] = 101030.0
        self.assertTrue(c.step(st, 101030.0, 50.0, NO_BUTTONS).screen_on)
        st["time"] = 101030.0 + 300
        self.assertFalse(c.step(st, 101030.0 + 300, 350.0, NO_BUTTONS).screen_on)  # uptime, not the clock

    def test_never_while_anything_is_connected(self):
        def connected(t, what):
            st = self.fresh(t)
            if what == "pulley":
                st["stage"] = "connected"
            elif what == "strap":
                st["heart_rate"] = {"name": "COROS", "connected": True}
            else:
                st[what] = True
            return st
        for what in ("pulley", "smartrow_app", "fitness_app", "strap"):
            c = app.Controller(sleep_s=3600)
            for t in range(0, 4 * 3600, 600):
                self.assertTrue(self.step(c, connected(t, what), t).screen_on, (what, t))
            # disconnected: the hour starts then
            self.assertTrue(self.step(c, self.fresh(4 * 3600), 4 * 3600).screen_on)
            self.assertFalse(self.step(c, self.fresh(5 * 3600), 5 * 3600).screen_on, what)
            self.assertTrue(self.step(c, connected(5 * 3600 + 1, what), 5 * 3600 + 1).screen_on)  # and wakes it

    def test_an_hour_by_default(self):
        self.assertEqual(app.SLEEP_S, 3600)

    def test_zero_means_never(self):
        c = app.Controller(sleep_s=0)
        for t in range(0, 7200, 600):
            self.assertTrue(self.step(c, self.fresh(t), t).screen_on)
