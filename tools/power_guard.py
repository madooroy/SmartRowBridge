"""Keep a Windows laptop awake and fast while a long job (the pose tool) runs, and put things back after.

    with PowerGuard(battery_min=50) as guard:
        for frame in ...:
            guard.check()        # cheap; looks at the battery about every 30 s

While it runs:
  * Sleep is blocked (Windows' "busy, don't sleep" request, as a video player makes). Released when the
    job ends or is stopped. Closing the lid still sleeps: that is the lid setting, not this.
  * Power mode: Best performance when plugged in, or on battery at or above battery_min (%). On battery
    below that: Balanced, to save power. If the battery falls to battery_floor (%) during the run: Balanced
    and a warning to plug in. Once dropped to Balanced on battery, it stays there until plugged in.
  * At the end, the power mode you had is put back.

On anything but Windows, or if Windows refuses a step, it says so once and the job runs as normal.
"""
from __future__ import annotations

import sys
import time
import uuid

# Windows 11 power-mode slider settings ("overlay schemes").
BEST_PERFORMANCE = "ded574b5-45a0-4f42-8737-46345c09c238"
BALANCED = "00000000-0000-0000-0000-000000000000"
MODE_NAMES = {BEST_PERFORMANCE: "Best performance", BALANCED: "Balanced",
              "961cc777-2547-4f9d-8174-7d86181b8a7a": "Best power efficiency"}


class WindowsPower:
    """The four Windows calls the guard needs (ctypes; Windows only)."""

    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class SystemPowerStatus(ctypes.Structure):
            _fields_ = [("ACLineStatus", wintypes.BYTE), ("BatteryFlag", wintypes.BYTE),
                        ("BatteryLifePercent", wintypes.BYTE), ("SystemStatusFlag", wintypes.BYTE),
                        ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]

        class Guid(ctypes.Structure):
            _fields_ = [("bytes", ctypes.c_ubyte * 16)]

        self._ctypes, self._Status, self._Guid = ctypes, SystemPowerStatus, Guid
        self._kernel = ctypes.windll.kernel32
        self._kernel.SetThreadExecutionState.restype = ctypes.c_uint
        try:
            self._powrprof = ctypes.windll.powrprof
            self._powrprof.PowerGetEffectiveOverlayScheme  # Windows 10 1709 and later
            self._powrprof.PowerSetActiveOverlayScheme
        except (OSError, AttributeError):
            self._powrprof = None

    def status(self) -> tuple[bool | None, int | None]:
        """(plugged in?, battery %) with None for unknown; a PC without a battery reads as plugged in."""
        s = self._Status()
        if not self._kernel.GetSystemPowerStatus(self._ctypes.byref(s)):
            return None, None
        if (s.BatteryFlag & 0xFF) == 128:  # no system battery
            return True, None
        ac = {0: False, 1: True}.get(s.ACLineStatus & 0xFF)
        pct = s.BatteryLifePercent & 0xFF
        return ac, (None if pct == 255 else pct)

    def keep_awake(self, on: bool) -> bool:
        flags = self.ES_CONTINUOUS | (self.ES_SYSTEM_REQUIRED if on else 0)
        return bool(self._kernel.SetThreadExecutionState(flags))

    def get_mode(self) -> str | None:
        if not self._powrprof:
            return None
        g = self._Guid()
        if self._powrprof.PowerGetEffectiveOverlayScheme(self._ctypes.byref(g)) != 0:
            return None
        return str(uuid.UUID(bytes_le=bytes(g.bytes)))

    def set_mode(self, mode: str) -> bool:
        if not self._powrprof:
            return False
        g = self._Guid()
        g.bytes[:] = uuid.UUID(mode).bytes_le
        return self._powrprof.PowerSetActiveOverlayScheme(g) == 0


class PowerGuard:
    def __init__(self, battery_min: int = 50, battery_floor: int = 15, api=None, log=print,
                 every_s: float = 30.0, clock=time.monotonic):
        self.battery_min, self.battery_floor = battery_min, battery_floor
        self.api, self.log, self.every_s, self.clock = api, log, every_s, clock
        self.active = False          # Windows calls available and working
        self.mode_ok = False         # power mode can be read and set
        self.fast: bool | None = None
        self.low_warned = False
        self.originals: dict[bool, str] = {}  # power mode before we changed it, per power source
        self.source: bool | None = None
        self._last = None

    # ---- the rule ----
    def want_fast(self, ac: bool | None, pct: int | None) -> bool:
        """Best performance or not, given the power source and battery, and what we have now."""
        if ac or ac is None:                     # plugged in, or a PC without a battery
            return True
        if pct is None:
            return False
        if self.fast and self.source is False:   # already fast on battery: hold until the floor
            return pct > self.battery_floor
        return pct >= self.battery_min and pct > self.battery_floor

    # ---- start, check, stop ----
    def __enter__(self):
        if self.api is None:
            if sys.platform != "win32":
                self.log("Power: not Windows, so sleep and power mode are left as they are.")
                return self
            try:
                self.api = WindowsPower()
            except Exception as e:  # noqa: BLE001 - any failure here just means "can't help"
                self.log(f"Power: can't reach Windows' power settings ({e}); running as normal.")
                return self
        self.active = self.api.keep_awake(True)
        self.mode_ok = self.api.get_mode() is not None
        if not self.active:
            self.log("Power: Windows refused to block sleep; check the sleep setting.")
        self._apply(first=True)
        return self

    def check(self) -> None:
        if not self.api:
            return
        now = self.clock()
        if self._last is not None and now - self._last < self.every_s:
            return
        self._apply()

    def __exit__(self, *exc):
        if not self.api:
            return False
        if self.mode_ok and self.source in self.originals:
            original = self.originals[self.source]
            if self.api.get_mode() != original:
                self.api.set_mode(original)
            self.log(f"Power: done; power mode back to {MODE_NAMES.get(original, 'what it was')}, sleep allowed again.")
        elif self.active:
            self.log("Power: done; sleep allowed again.")
        if self.active:
            self.api.keep_awake(False)
        return False

    def _apply(self, first: bool = False) -> None:
        self._last = self.clock()
        ac, pct = self.api.status()
        source = bool(ac) if ac is not None else True
        changed_source = source != self.source
        fast = self.want_fast(ac, pct)
        where = "plugged in" if source else f"on battery ({pct if pct is not None else '?'}%)"
        if self.mode_ok and source not in self.originals:
            self.originals[source] = self.api.get_mode()
        if self.mode_ok:
            target = BEST_PERFORMANCE if fast else BALANCED
            if self.api.get_mode() != target:
                self.api.set_mode(target)
        if first or changed_source or fast != self.fast:
            mode = ("Best performance" if fast else "Balanced") if self.mode_ok else "power mode unchanged (Windows doesn't allow it)"
            awake = ", sleep blocked until done" if self.active else ""
            why = ""
            if not fast and not source:
                why = (f" (battery at or below {self.battery_floor}%: plug in)" if pct is not None and pct <= self.battery_floor
                       else f" (battery under {self.battery_min}%, saving power)")
            self.log(f"Power: {where}: {mode}{awake}{why}.")
        if not source and pct is not None and pct <= self.battery_floor and not self.low_warned:
            self.low_warned = True
            if not (first or changed_source or fast != self.fast):
                self.log(f"Power: battery {pct}%: plug in. Windows will hibernate at its critical level.")
        if source:
            self.low_warned = False
        self.fast, self.source = fast, source
