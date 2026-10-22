"""What the screen shows and what the buttons do. No hardware here, so it can be tested.

- The status screen, until rowing starts; then the rowing screen, back to status a
  minute after the last stroke.
- B switches between the two by hand (until rowing starts or stops again).
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


@dataclass
class Frame:
    image: Image.Image
    led: tuple[bool, bool, bool]  # red, green, blue
    switch_off: bool = False  # the hold finished: shut the Pi down now


def led_for(status: dict | None, now: float, uptime: float) -> tuple[bool, bool, bool]:
    """Green: ready. Yellow: waiting for the pulley. Red: bridge not running."""
    state = screens.bridge_state(status, now, uptime)
    if state == "down":
        return True, False, False
    if state == "ok" and status.get("stage") == "connected":
        return False, True, False
    return True, True, False


class Controller:
    def __init__(self, hold_s: float = 2.0) -> None:
        self.hold_s = hold_s
        self.view = "status"
        self._manual: str | None = None  # view chosen with B
        self._was_rowing = False
        self._last_rowing = -1e9
        self._a_since: float | None = None
        self.a_armed = False  # A has been seen released since start
        self._b_was_down = False
        self.switching_off = False

    def _auto_view(self, rowing: bool, now: float) -> str:
        if rowing:
            self._last_rowing = now
        if rowing != self._was_rowing:
            self._manual = None  # rowing started or stopped: automatic choice again
            self._was_rowing = rowing
        if self._manual:
            return self._manual
        return "rowing" if now - self._last_rowing < BACK_TO_STATUS_S else "status"

    def step(self, status: dict | None, now: float, uptime: float, buttons: dict[str, bool],
             address: str = "") -> Frame:
        if self.switching_off:
            return Frame(screens.switching_off_screen(), (False, False, False))

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
                return Frame(screens.switching_off_screen(), (False, False, False), switch_off=True)
            return Frame(screens.hold_screen(held / self.hold_s), led_for(status, now, uptime))
        self._a_since = None

        if self.view == "rowing":
            image = screens.rowing_screen(status, now, uptime)
        else:
            image = screens.status_screen(status, now, uptime, address)
        return Frame(image, led_for(status, now, uptime))
