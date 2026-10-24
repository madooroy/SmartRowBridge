"""What the screen shows and what the buttons do. No hardware here, so it can be tested.

- The status screen, until rowing starts; then the rowing screen, back to status a
  minute after the last stroke.
- B switches between the two by hand (until rowing starts or stops again).
- While rowing with the interval coach on (smartrow_bridge.coach): the interval
  screen during an effort, then its summary for a while after it; X brings the last
  summary back, or closes it.
- Holding A for SRB_DISPLAY_HOLD_S seconds (2) switches the Pi off; letting go
  earlier cancels. A only counts once it has been seen released: a button that reads
  as pressed from the start (stuck, shorted, HAT seated a row off) must never be able
  to switch the Pi off on every boot.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

from . import screens

BACK_TO_STATUS_S = 60.0
RECALL_S = 20.0  # X shows the last interval summary this long


@dataclass
class Frame:
    image: Image.Image
    led: str | None  # "red", "green" or None (off)
    switch_off: bool = False  # the hold finished: shut the Pi down now


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

    def _rowing_image(self, status: dict | None, now: float, uptime: float, x_down: bool) -> Image.Image:
        """The plain rowing screen, or the interval coach's live or summary screen."""
        coach = (status or {}).get("coach") or {}
        last = coach.get("last")
        x_pressed = x_down and not self._x_was_down
        self._x_was_down = x_down
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

    def step(self, status: dict | None, now: float, uptime: float, buttons: dict[str, bool],
             address: str = "") -> Frame:
        if self.switching_off:
            return Frame(screens.switching_off_screen(), None)

        fresh = screens.bridge_state(status, now, uptime) == "ok"
        rowing = fresh and bool(status.get("rowing"))
        self.view = self._auto_view(rowing, now)

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
                self.switching_off = True
                return Frame(screens.switching_off_screen(), None, switch_off=True)
            return Frame(screens.hold_screen(held / self.hold_s), led_for(status, now, uptime, self._rowing_recently(now)))
        self._a_since = None

        if self.view == "rowing":
            image = self._rowing_image(status, now, uptime, buttons.get("X", False) if fresh else False)
        else:
            self.screen = "status"
            image = screens.status_screen(status, now, uptime, address)
        return Frame(image, led_for(status, now, uptime, self._rowing_recently(now)))
