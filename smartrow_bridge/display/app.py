"""What the screen shows and what the buttons do. No hardware here, so it can be tested.

- The status screen, until rowing starts; then the rowing screen, back to status a
  minute after the last stroke.
- B switches between the two by hand (until rowing starts or stops again).
- While rowing with the interval coach on (smartrow_bridge.coach): the interval
  screen during an effort, then its summary for a while after it; X brings the last
  summary back, or closes it.
- Holding Y for a second: you're done. The bridge saves the session as finished and
  uploads it straight away (not FINAL_AFTER_S later); the screen shows each upload as it
  goes. Rowing again after that is a new session. Offered (the Y tab) once a session has
  MIN_SESSION_STROKES strokes.
- Holding X for X_HOLD_S seconds (2): discard this session (a warm-up you don't want kept):
  the bridge deletes its files, so it is neither kept nor uploaded, and starts over. A
  quick tap of X still brings back or closes the interval summary (on release).
- Holding A for SRB_DISPLAY_HOLD_S seconds (2) switches the Pi off; letting go
  earlier cancels. A only counts once it has been seen released: a button that reads
  as pressed from the start (stuck, shorted, HAT seated a row off) must never be able
  to switch the Pi off on every boot. With a session still to upload, the Pi finishes
  it, uploads and then switches off by itself (A again: off now; B: cancel); a service
  that can't be reached gets it the next time the Pi is on.

The screen asks the bridge to finish a session through a request file beside the status
file (status.send_request); Frame.request carries it to the caller, which writes it.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

from . import screens

RED_HOLD = screens.RED

BACK_TO_STATUS_S = 60.0
RECALL_S = 20.0  # X shows the last interval summary this long
Y_HOLD_S = 1.0  # hold Y this long: done
X_HOLD_S = 2.0  # hold X this long: discard the session
X_TAP_S = 0.6  # X let go before this: a tap (the interval summary); longer shows the discard hold
DISCARD_SHOW_S = 6.0
MIN_SESSION_STROKES = 30  # as sessions.MIN_STROKES: fewer is not a session
ACK_WAIT_S = 10.0  # the bridge answers a request within a second or two; don't wait longer than this
FINISH_SHOW_S = 20.0  # the "Session finished" screen stays this long once its uploads are through
FINISH_MAX_S = 120.0
OFF_WAIT_MAX_S = 120.0  # A with uploads to go: switch off after this at the latest
OFF_CLOSING_S = 3.0  # show how the uploads ended, then switch off


@dataclass
class Frame:
    image: Image.Image
    led: str | None  # "red", "green" or None (off)
    switch_off: bool = False  # the hold finished: shut the Pi down now
    request: dict | None = None  # for the bridge: {"action": "finish", "token": ...} (status.request_path)


def _session_open(status: dict | None) -> bool:
    coach = (status or {}).get("coach") or {}
    return coach.get("session_strokes", 0) >= MIN_SESSION_STROKES


def _uploads(status: dict | None) -> dict:
    return (status or {}).get("uploads") or {}


def _outstanding(status: dict | None) -> bool:
    ups = _uploads(status)
    return bool(ups.get("sending")) or ups.get("waiting", 0) > 0


def _acked(status: dict | None, token: str | None) -> bool:
    """The bridge has handled the request and taken stock of the uploads since."""
    if token is None:
        return True
    finish = (status or {}).get("finish") or {}
    ups = (status or {}).get("uploads")
    return finish.get("token") == token and (ups is None or ups.get("ack") == token)

def led_for(status: dict | None, now: float, uptime: float, rowing_recently: bool = False) -> str | None:
    """Dim red: starting, looking for the pulley, or bridge not running. Dim green: ready.
    Off: while rowing, until the screen goes back to the status a minute after the last stroke."""
    if rowing_recently:
        return None
    if screens.bridge_state(status, now, uptime) == "ok" and status.get("stage") == "connected":
        return "green"
    return "red"


class Controller:
    def __init__(self, hold_s: float = 2.0) -> None:
        self.hold_s = hold_s
        self.view = "status"
        self.screen = "status"  # what was drawn last: status, rowing, interval or summary
        self._manual: str | None = None  # view chosen with B
        self._was_rowing = False
        self._last_rowing = -1e9
        self._a_since: float | None = None
        self.a_armed = False  # A has been seen released since start
        self._b_was_down = False
        self._x_was_down = False
        self._recall_until = -1e9  # X: show the last summary until then
        self._closed_index: int | None = None  # X: summary of this interval closed
        self.switching_off = False
        self._y_since: float | None = None
        self.y_armed = False  # Y has been seen released since start (as A)
        self._finish: dict | None = None  # Y: {"token", "at", "settled_at"} while its screen shows
        self._x_since: float | None = None
        self.x_armed = False  # X has been seen released since start (as A)
        self._discard: dict | None = None  # X held: {"token", "at"} while its screen shows
        self._off: dict | None = None  # A with uploads to go: {"token", "at", "closing_at", "a_up"}

    def _rowing_recently(self, now: float) -> bool:
        return now - self._last_rowing < BACK_TO_STATUS_S

    def _auto_view(self, rowing: bool, now: float) -> str:
        if rowing:
            self._last_rowing = now
        if rowing != self._was_rowing:
            self._manual = None  # rowing started or stopped: automatic choice again
            self._was_rowing = rowing
        if self._manual:
            return self._manual
        return "rowing" if self._rowing_recently(now) else "status"

    def _rowing_image(self, status: dict | None, now: float, uptime: float, x_pressed: bool) -> Image.Image:
        """The plain rowing screen, or the interval coach's live or summary screen."""
        coach = (status or {}).get("coach") or {}
        last = coach.get("last")
        if coach.get("live"):
            self._recall_until = -1e9
            self.screen = "interval"
            return screens.interval_screen(status, now, uptime)
        self.screen = "rowing"
        if not last:
            return screens.rowing_screen(status, now, uptime)
        auto = coach.get("show_summary") and self._closed_index != last["index"]
        showing = auto or now < self._recall_until
        if x_pressed:
            if showing:
                self._recall_until = -1e9
                self._closed_index = last["index"]
                showing = False
            else:
                self._recall_until = now + RECALL_S
                showing = True
        if showing:
            self.screen = "summary"
            return screens.summary_screen(status, now, uptime)
        return screens.rowing_screen(status, now, uptime)

    def _token(self, now: float) -> str:
        return f"screen-{now:.3f}"

    def _off_step(self, status: dict | None, now: float, buttons: dict[str, bool], led) -> Frame | None:
        """A was held with uploads to go: upload, then switch off (A again: now; B: cancel)."""
        off = self._off
        if not buttons.get("A", False):
            off["a_up"] = True
        elif off["a_up"]:
            self.switching_off = True
            return Frame(screens.switching_off_screen(), None, switch_off=True)
        b_down = buttons.get("B", False)
        if b_down and not self._b_was_down:
            self._b_was_down = True
            self._off = None
            return None
        self._b_was_down = b_down
        settled = _acked(status, off["token"]) or now - off["at"] > ACK_WAIT_S
        if off["closing_at"] is None and ((settled and not _outstanding(status)) or now - off["at"] > OFF_WAIT_MAX_S):
            off["closing_at"] = now
        if off["closing_at"] is not None and now - off["closing_at"] >= OFF_CLOSING_S:
            self.switching_off = True
            return Frame(screens.switching_off_screen(), None, switch_off=True)
        return Frame(screens.upload_off_screen(status, off["closing_at"] is not None), led)

    def _finish_step(self, status: dict | None, now: float, buttons: dict[str, bool], led) -> Frame | None:
        """After Y: the session finished and its uploads, until they're through (X or B closes)."""
        fin = self._finish
        x_down, b_down = buttons.get("X", False), buttons.get("B", False)
        closed = (x_down and not self._x_was_down) or (b_down and not self._b_was_down)
        self._x_was_down, self._b_was_down = x_down, b_down
        coach = (status or {}).get("coach") or {}
        if closed or now - fin["at"] > FINISH_MAX_S or (coach.get("live") and now - fin["at"] > 5):
            self._finish = None
            return None
        acked = _acked(status, fin["token"])
        session = ((status or {}).get("finish") or {}).get("session") if acked else None
        if acked and fin["settled_at"] is None:
            states = ((_uploads(status).get("latest") or {}).get("dests") or {}).values()
            if session is None or not any(st in ("waiting", "later", "rowing") for st in states):
                if not _uploads(status).get("sending"):
                    fin["settled_at"] = now
        if fin["settled_at"] is not None:
            linger = FINISH_SHOW_S if session and _uploads(status).get("configured") else 5.0
            if now - fin["settled_at"] > linger:
                self._finish = None
                return None
        return Frame(screens.finish_screen(status, session, acked), led)

    def step(self, status: dict | None, now: float, uptime: float, buttons: dict[str, bool],
             address: str = "") -> Frame:
        if self.switching_off:
            return Frame(screens.switching_off_screen(), None)

        fresh = screens.bridge_state(status, now, uptime) == "ok"
        rowing = fresh and bool(status.get("rowing"))
        self.view = self._auto_view(rowing, now)
        led = led_for(status, now, uptime, self._rowing_recently(now))

        if self._off is not None:
            frame = self._off_step(status, now, buttons, led)
            if frame is not None:
                return frame

        # Y: done (held Y_HOLD_S); only counts once seen released, like A.
        y_down = buttons.get("Y", False)
        if not y_down:
            self.y_armed = True
            self._y_since = None
        elif self.y_armed and fresh:
            self._y_since = now if self._y_since is None else self._y_since
            held = now - self._y_since
            if held >= Y_HOLD_S:
                self.y_armed, self._y_since = False, None
                token = self._token(now)
                self._finish = {"token": token, "at": now, "settled_at": None}
                self._x_was_down = buttons.get("X", False)
                self._b_was_down = buttons.get("B", False)
                return Frame(screens.finish_screen(status, None, False), led,
                             request={"action": "finish", "token": token})
            return Frame(screens.hold_screen(held / Y_HOLD_S, "Keep holding: done rowing"), led)

        if self._finish is not None:
            frame = self._finish_step(status, now, buttons, led)
            if frame is not None:
                return frame

        # X: a tap (on release) for the interval summary; held X_HOLD_S: discard the session.
        x_down, x_tap = buttons.get("X", False), False
        if not x_down:
            if self._x_since is not None and self.x_armed and now - self._x_since < X_TAP_S:
                x_tap = True
            self.x_armed = True
            self._x_since = None
        elif self.x_armed and fresh:
            self._x_since = now if self._x_since is None else self._x_since
            held = now - self._x_since
            if held >= X_HOLD_S:
                self.x_armed, self._x_since = False, None
                token = self._token(now)
                self._discard = {"token": token, "at": now}
                return Frame(screens.discard_screen(status, False), led, request={"action": "discard", "token": token})
            if held >= X_TAP_S:
                return Frame(screens.hold_screen((held - X_TAP_S) / (X_HOLD_S - X_TAP_S),
                                                 "Keep holding: discard session", RED_HOLD), led)
        self._x_was_down = x_down

        if self._discard is not None:
            if now - self._discard["at"] > DISCARD_SHOW_S or x_tap or (buttons.get("B") and not self._b_was_down):
                self._discard = None
                x_tap = False
            else:
                acked = ((status or {}).get("finish") or {}).get("token") == self._discard["token"]
                return Frame(screens.discard_screen(status, acked), led)

        b_down = buttons.get("B", False)
        if b_down and not self._b_was_down:
            self._manual = "status" if self.view == "rowing" else "rowing"
            self.view = self._manual
        self._b_was_down = b_down

        if not buttons.get("A", False):
            self.a_armed = True
        if buttons.get("A", False) and self.a_armed:
            if self._a_since is None:
                self._a_since = now
            held = now - self._a_since
            if held >= self.hold_s:
                if fresh and _uploads(status).get("configured") and (_session_open(status) or _outstanding(status)):
                    # Uploads to go: finish the session, upload, then switch off by itself.
                    token = self._token(now)
                    self._off = {"token": token, "at": now, "closing_at": None, "a_up": False}
                    self._a_since = None
                    self._b_was_down = buttons.get("B", False)
                    return Frame(screens.upload_off_screen(status, False), led,
                                 request={"action": "finish", "token": token})
                self.switching_off = True
                return Frame(screens.switching_off_screen(), None, switch_off=True)
            return Frame(screens.hold_screen(held / self.hold_s), led_for(status, now, uptime, self._rowing_recently(now)))
        self._a_since = None

        if self.view == "rowing":
            image = self._rowing_image(status, now, uptime, x_tap and fresh)
            if fresh and self.screen != "summary" and _session_open(status):
                image = screens.add_tab(image, "Y", "check", "Hold: done")
            coach = (status or {}).get("coach") or {}
            if fresh and self.screen == "rowing" and not coach.get("last") \
                    and coach.get("session_strokes", 0) > 0:
                image = screens.add_tab(image, "X", "close", "Hold: discard")
        else:
            self.screen = "status"
            image = screens.status_screen(status, now, uptime, address)
        return Frame(image, led_for(status, now, uptime, self._rowing_recently(now)))
