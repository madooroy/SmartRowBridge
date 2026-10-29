"""Draws the screens for the 320 x 240 Display HAT Mini. Pure Pillow: no hardware needed.

Every function returns a PIL image; the hardware module only copies it to the LCD,
and `python -m smartrow_bridge.display --preview DIR` saves them as PNG files.

Button hints are small tabs in the corners next to the buttons (A top left,
B bottom left, X top right, Y bottom right): a power symbol on A, and on B the
screen it switches to.
"""

from __future__ import annotations

import math

from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FORCE_HALF = 12  # samples in the first half of the drive (smartrow_bridge.coach.back_share)

WIDTH, HEIGHT = 320, 240

BG = (0, 0, 0)
FG = (240, 240, 240)
DIM = (135, 135, 135)
FAINT = (70, 70, 70)
TILE = (26, 26, 26)
GREEN = (70, 205, 95)
AMBER = (245, 175, 45)
BLUE = (90, 165, 240)  # the force curve during steady rowing (no cue: steady is the reference)
RED = (235, 75, 65)
GAP = (215, 215, 215)  # force missing in the finish, against the steady shape

STALE_AFTER_S = 5.0  # status file older than this: the bridge is not running
BOOT_GRACE_S = 90.0  # before this much uptime, a missing bridge is still "starting"

_FONT_DIRS = (
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/dejavu",
)


@lru_cache(maxsize=None)
def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for directory in _FONT_DIRS:
        path = Path(directory) / name
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size)  # Pillow >= 10.1: a scalable built-in font


def _fit(draw: ImageDraw.ImageDraw, text: str, size: int, max_width: int, bold: bool = False):
    """The largest font up to `size` in which text fits max_width."""
    while size > 10:
        f = font(size, bold)
        if draw.textlength(text, font=f) <= max_width:
            return f
        size -= 2
    return font(size, bold)


def _canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    return image, ImageDraw.Draw(image)


def _icon_power(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, colour) -> None:
    """The standard power symbol: a circle open at the top, a line through the gap."""
    draw.arc((cx - r, cy - r, cx + r, cy + r), start=-50, end=230, fill=colour, width=2)
    draw.line((cx, cy - r - 1, cx, cy), fill=colour, width=2)


def _icon_rowing(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, colour) -> None:
    """Rowing figures: three bars of a bar chart."""
    for i, h in enumerate((0.5, 1.0, 0.75)):
        x = cx - r + 1 + i * (2 * r // 3 + 1)
        draw.rectangle((x, cy + r - int(2 * r * h), x + 3, cy + r), fill=colour)


def _icon_status(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, colour) -> None:
    """Status list: three dots with lines, like the status screen's rows."""
    for i in range(3):
        y = cy - r + 2 + i * (r - 1)
        draw.ellipse((cx - r, y - 2, cx - r + 4, y + 2), fill=colour)
        draw.line((cx - r + 7, y, cx + r, y), fill=colour, width=2)


def _icon_curve(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, colour) -> None:
    """Force curve: a single hump."""
    draw.arc((cx - r, cy - r + 2, cx + r, cy + r + 8), start=200, end=340, fill=colour, width=2)
    draw.line((cx - r, cy + r, cx + r, cy + r), fill=colour, width=1)


def _icon_check(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, colour) -> None:
    """Done: a tick."""
    draw.line((cx - r, cy, cx - r // 3, cy + r - 1, cx + r, cy - r + 1), fill=colour, width=3, joint="curve")


def _icon_close(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, colour) -> None:
    """Cancel: a cross."""
    d = r - 2
    draw.line((cx - d, cy - d, cx + d, cy + d), fill=colour, width=2)
    draw.line((cx - d, cy + d, cx + d, cy - d), fill=colour, width=2)


_ICONS = {"power": _icon_power, "rowing": _icon_rowing, "status": _icon_status, "curve": _icon_curve,
          "check": _icon_check, "close": _icon_close}


def add_tab(image: Image.Image, corner: str, icon: str, label: str) -> Image.Image:
    """Put a button tab on a finished screen (used for Y: Done, when there is a session to finish)."""
    _button(ImageDraw.Draw(image), corner, icon, label)
    return image


def _button(draw: ImageDraw.ImageDraw, corner: str, icon: str, label: str) -> None:
    """A small tab in the screen corner next to a button: icon + word."""
    f = font(13)
    tab_h, r = 22, 7
    width = 8 + 2 * r + 6 + int(draw.textlength(label, font=f)) + 8
    left = corner in ("A", "B")
    top = corner in ("A", "X")
    x0 = 0 if left else WIDTH - width
    y0 = 0 if top else HEIGHT - tab_h
    draw.rounded_rectangle((x0 - 8 if left else x0, y0 - 8 if top else y0,
                            x0 + width if left else WIDTH + 8, y0 + tab_h if top else HEIGHT + 8),
                           radius=8, fill=TILE)
    _ICONS[icon](draw, x0 + 8 + r, y0 + tab_h // 2, r, FG)
    draw.text((x0 + 8 + 2 * r + 6, y0 + tab_h // 2), label, font=f, fill=FG, anchor="lm")


def _hints(draw: ImageDraw.ImageDraw, view: str, x_label: str | None = None) -> None:
    """A: hold to switch off. B: go to the other screen. X (optional): the last interval."""
    _button(draw, "A", "power", "Hold: off")
    if view == "status":
        _button(draw, "B", "rowing", "Rowing")
    else:
        _button(draw, "B", "status", "Status")
    if x_label:
        _button(draw, "X", "curve", x_label)


def _centered(draw: ImageDraw.ImageDraw, lines: list[tuple[str, int, bool, tuple]], centre_y: int = HEIGHT // 2):
    """Lines of (text, size, bold, colour), centred as a block."""
    gap = 10
    fonts = [_fit(draw, text, size, WIDTH - 24, bold) for text, size, bold, _ in lines]
    heights = [f.size for f in fonts]
    y = centre_y - (sum(heights) + gap * (len(lines) - 1)) // 2
    for (text, _, _, colour), f, h in zip(lines, fonts, heights):
        draw.text((WIDTH // 2, y + h // 2), text, font=f, fill=colour, anchor="mm")
        y += h + gap


def bridge_state(status: dict | None, now: float, uptime: float) -> str:
    """'ok' when the status file is fresh, else 'starting' (early in boot) or 'down'."""
    if status and now - float(status.get("time", 0)) <= STALE_AFTER_S:
        return "ok"
    return "starting" if uptime < BOOT_GRACE_S else "down"


def format_pace(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}" if seconds > 0 else "–"


def format_time(seconds: int) -> str:
    hours, rest = divmod(max(0, int(seconds)), 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


# ---------------------------------------------------------------- screens

def status_screen(status: dict | None, now: float, uptime: float, address: str = "") -> Image.Image:
    image, draw = _canvas()
    state = bridge_state(status, now, uptime)
    _hints(draw, "status")
    if address:
        # One line (host and IP) or two (the web page's address by name and by IP), bottom right,
        # clear of the B tab: the first line brightest, so it reads at a glance.
        lines = address.split("\n")
        widest = max(lines, key=len)
        f = _fit(draw, widest, 15 if len(lines) > 1 else 13, WIDTH - 104)
        for i, line in enumerate(reversed(lines)):
            first = i == len(lines) - 1
            draw.text((WIDTH - 4, HEIGHT - 4 - i * (f.size + 3)), line, font=f,
                      fill=FG if first and len(lines) > 1 else DIM, anchor="rd")

    if state != "ok":
        if state == "starting":
            _centered(draw, [("Starting up…", 28, True, FG), ("The bridge is not ready yet", 17, False, DIM)])
        else:
            _centered(draw, [("Bridge not running", 26, True, RED),
                             ("It restarts by itself; if this stays,", 15, False, DIM),
                             ("run tools/health_check.sh", 15, False, DIM)])
        return image

    stage = status.get("stage")
    connected = stage in ("connected", "reconnecting")  # virtual devices are up
    if stage == "connected":
        pulley = (status.get("pulley") or "Pulley"), "Connected", GREEN
    elif stage == "reconnecting":
        pulley = (status.get("pulley") or "Pulley"), "Lost - pull the handle", AMBER
    elif stage == "searching":
        pulley = "Pulley", "Waiting - pull the handle", AMBER
    else:
        pulley = "Pulley", "Preparing Bluetooth…", DIM

    def app_row(name: str, attached: bool) -> tuple[str, str, tuple]:
        if not connected:
            return name, "After the pulley connects", FAINT
        return name, ("Connected" if attached else "Not connected"), (GREEN if attached else DIM)

    rows = [
        pulley,
        app_row("SmartRow app", bool(status.get("smartrow_app"))),
        app_row(f"{status.get('fitness_name') or 'PiRower'} (fitness app)", bool(status.get("fitness_app"))),
    ]
    hr = status.get("heart_rate")  # only when the bridge reads a strap (SRB_HEART_RATE)
    if hr is not None:
        bpm = (status.get("metrics") or {}).get("heart_rate_bpm")
        label = hr.get("name") or "Heart rate strap"  # its own name once found, like the pulley's
        if not connected:
            rows.append((label, "After the pulley connects", FAINT))
        elif hr.get("connected"):
            rows.append((label, f"Connected \u00b7 \u2665 {bpm} bpm" if bpm else "Connected \u00b7 \u2665", GREEN))
        else:
            rows.append((label, "Looking - put it on", AMBER))
    two_line_address = "\n" in address
    if len(rows) > 3:  # four rows: a little smaller, so they fit above the web page's address
        y, step, big, small, gap, dot = (22, 43, 18, 15, 21, 16) if two_line_address else (24, 48, 19, 16, 23, 16)
    else:
        y, step, big, small, gap, dot = (26, 55, 21, 18, 28, 18) if two_line_address else (30, 60, 21, 18, 28, 18)
    for label, text, colour in rows:
        top = y + (big - dot) // 2 + 2
        draw.ellipse((14, top, 14 + dot, top + dot), fill=colour if colour != FAINT else None,
                     outline=colour, width=2)
        draw.text((44, y), label, font=_fit(draw, label, big, WIDTH - 54, bold=True), fill=FG)
        draw.text((44, y + gap), text, font=_fit(draw, text, small, WIDTH - 54), fill=colour)
        y += step
    return image


def rowing_screen(status: dict | None, now: float, uptime: float) -> Image.Image:
    image, draw = _canvas()
    coach = (status or {}).get("coach") or {}
    _hints(draw, "rowing", "Last" if coach.get("last") else None)
    if bridge_state(status, now, uptime) != "ok":
        _centered(draw, [("No data", 28, True, DIM), ("B shows the status", 16, False, DIM)])
        return image

    m = status.get("metrics") or {}
    rowing = bool(status.get("rowing"))
    live = (lambda v: v) if rowing else (lambda v: "\u2013")
    footer = f"{format_time(m.get('elapsed_s', 0))}  \u00b7  {m.get('distance_m', 0)} m"
    if m.get("heart_rate_bpm"):
        footer += f"  \u00b7  \u2665 {m['heart_rate_bpm']}"
    if not rowing:
        footer += "  \u00b7  paused"

    if coach.get("stroke_curve") and coach.get("intensity") != "Recovery":
        # Easy rowing with the coach: three numbers, each stroke's force curve over your early Easy
        # strokes, and the cue (none while the coach is still learning them).
        tiles = [("WATTS", live(str(m.get("power_w", 0)))),
                 ("PACE", live(format_pace(int(m.get("pace_s_per_500m", 0))))),
                 ("SPM", live(str(round(m.get("stroke_rate_spm", 0)))))]
        top, tile_w, tile_h, gap = 26, 101, 50, 4
        for i, (label, value) in enumerate(tiles):
            x0 = 4 + i * (tile_w + gap)
            draw.rounded_rectangle((x0, top, x0 + tile_w, top + tile_h), radius=8, fill=TILE)
            draw.text((x0 + 8, top + 4), label, font=font(12), fill=DIM)
            draw.text((x0 + tile_w // 2, top + 32), value, font=_fit(draw, value, 28, tile_w - 12, bold=True),
                      fill=FG if value != "\u2013" else FAINT, anchor="mm")
        steady = coach.get("steady_curve") or []
        cue = coach.get("cue")
        text, colour = CUES.get(cue, ("", BLUE)) if cue else ("", BLUE)
        _plot_curves(draw, (4, 80, WIDTH - 4, 179), coach["stroke_curve"], steady, colour, "both")
        head = f"EASY  \u00b7  {format_time(m.get('elapsed_s', 0))}"
        if m.get("heart_rate_bpm"):
            head += f"  \u00b7  \u2665 {m['heart_rate_bpm']}"
        draw.text((12, 85), head, font=font(12, True), fill=DIM)
        banner = (4, 183, WIDTH - 4, 216)
        if cue:
            draw.rounded_rectangle(banner, radius=8, fill=colour)
            draw.text((WIDTH // 2, 199), text, font=_fit(draw, text, 26, WIDTH - 24, True), fill=BG, anchor="mm")
        else:
            draw.rounded_rectangle(banner, radius=8, fill=TILE)
            draw.text((WIDTH // 2, 199), "learning your Easy stroke", font=font(16), fill=DIM, anchor="mm")
        return image

    tiles = [
        ("WATTS", live(str(m.get("power_w", 0)))),
        ("STROKES / MIN", live(str(round(m.get("stroke_rate_spm", 0))))),
        ("PACE / 500 M", live(format_pace(int(m.get("pace_s_per_500m", 0))))),
        ("METRES", str(m.get("distance_m", 0))),
    ]
    top, tile_w, tile_h, gap = 26, 154, 82, 4
    for i, (label, value) in enumerate(tiles):
        x0 = 4 + (i % 2) * (tile_w + gap)
        y0 = top + (i // 2) * (tile_h + gap)
        draw.rounded_rectangle((x0, y0, x0 + tile_w, y0 + tile_h), radius=8, fill=TILE)
        draw.text((x0 + 10, y0 + 7), label, font=font(13), fill=DIM)
        colour = FG if value != "\u2013" else FAINT
        draw.text((x0 + tile_w // 2, y0 + 53), value, font=_fit(draw, value, 46, tile_w - 16, bold=True),
                  fill=colour, anchor="mm")

    footer = f"{format_time(m.get('elapsed_s', 0))}   \u00b7   {m.get('stroke_count', 0)} strokes"
    if m.get("heart_rate_bpm"):
        footer = f"{format_time(m.get('elapsed_s', 0))}  \u00b7  {m.get('stroke_count', 0)} strokes  \u00b7  \u2665 {m['heart_rate_bpm']}"
    if not rowing:
        footer += "   \u00b7   paused"
    recovery = coach.get("intensity") == "Recovery"  # no coaching while you recover: the numbers
    if recovery:
        footer = f"RECOVERY  \u00b7  {format_time(m.get('elapsed_s', 0))}" + (
            f"  \u00b7  \u2665 {m['heart_rate_bpm']}" if m.get("heart_rate_bpm") else "")
    draw.text((WIDTH // 2, top + 2 * tile_h + gap + 14), footer, font=font(16, recovery),
              fill=BLUE if recovery else FG if rowing else DIM, anchor="mm")
    return image


# ---------------------------------------------------------------- interval coach

CUES = {
    # Two words: what to do, and the part of the stroke; never a muscle (see coach.py).
    "finish": ("STRONGER FINISH", AMBER),
    "legs": ("EARLIER PUSH", AMBER),
    "length": ("LONGER STROKES", AMBER),
    "good": ("LOOKS GOOD", GREEN),
}
RATING_COLOURS = {"good": GREEN, "ok": AMBER, "short": RED}
GOOD_FINISH_PCT, OK_FINISH_PCT = -5, -12  # same bands as smartrow_bridge.coach (GOOD_FINISH, OK_FINISH)
METER_MIN_PCT, METER_MAX_PCT = -30, 15


def finish_colour(pct: int | None) -> tuple:
    if pct is None:
        return DIM
    return GREEN if pct >= GOOD_FINISH_PCT else AMBER if pct >= OK_FINISH_PCT else RED


def _shade(colour: tuple, amount: float = 0.35) -> tuple:
    return tuple(int(c * amount) for c in colour)


def _plot_curves(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], curve: list, steady: list,
                 colour: tuple, gap_half: str = "back") -> None:
    """The effort's force curve, filled, over the steady curve as a thin grey line.

    The steady curve is scaled to the same total force, so the picture compares shape
    (where in the drive the force comes), not size: a weak finish shows as the
    coloured curve dropping below the grey line in the back half.
    """
    if curve and steady and sum(steady):
        scale = sum(curve) / sum(steady)
        steady = [v * scale for v in steady]
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(box, radius=8, fill=TILE)
    x0, y0, x1, y1 = x0 + 8, y0 + 8, x1 - 8, y1 - 4
    top = max([*curve, *steady, 1])

    def points(values: list) -> list[tuple[float, float]]:
        values = [0, *values]  # the curve starts at the catch with no force
        step = (x1 - x0) / (len(values) - 1)
        return [(x0 + i * step, y1 - (y1 - y0) * v / top) for i, v in enumerate(values)]

    # Half-way along the steady drive (the samples are spaced by distance; the drive ends at
    # the last sample above 5 % of the peak, as in coach.drive_end), +1 for the leading zero.
    ref = steady or curve or [0]
    top_ref = max(ref) or 1
    end = max((i for i, v in enumerate(ref) if v > 0.05 * top_ref), default=len(ref) - 1)
    half = end / 2 + 1
    mid = x0 + (x1 - x0) * half / (len(curve or steady or [0]) + 1)
    if curve:
        pts = points(curve)
        draw.polygon([(x0, y1), *pts, (x1, y1)], fill=_shade(colour))
    if curve and steady:
        # Fill the force missing against the steady shape in the half that matters:
        # the back half normally, the front half after a late push.
        sp = points(steady)
        cut = math.ceil(half)
        span = (range(cut, len(pts) - 1) if gap_half == "back" else range(0, cut)
                if gap_half == "front" else range(0, len(pts) - 1))
        for i in span:
            a, b, c, d = pts[i], pts[i + 1], sp[i + 1], sp[i]
            if c[1] < b[1] or d[1] < a[1]:  # steady above the effort here (smaller y = higher)
                draw.polygon([a, b, (b[0], min(b[1], c[1])), (a[0], min(a[1], d[1]))], fill=GAP)
    if curve:
        draw.line(pts, fill=colour, width=3, joint="curve")
    if steady:
        draw.line(points(steady), fill=DIM, width=2, joint="curve")
        draw.text((x1, y0), "Easy shape", font=font(12), fill=DIM, anchor="rt")
    if curve:
        for y in range(y0 + 16, y1, 8):  # dashed line half-way along the steady drive
            draw.line((mid, y, mid, y + 3), fill=FAINT, width=1)


BAR_RANGE = 2.0  # the summary's shape block moves up to +-2 cue thresholds
STEADY_SHARE = 0.5  # the steady block's width, as a share of the meter
SHIFT_PER_UNIT = 0.25  # the force block moves this share of the steady width per cue threshold


def _block(draw: ImageDraw.ImageDraw, x0: float, y0: int, x1: float, y1: int, fill, outline=None) -> None:
    draw.rounded_rectangle((round(x0), y0, round(x1), y1), radius=3, fill=fill, outline=outline)


def _mini_shape(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], balance: float, colour: tuple) -> None:
    """A small version of the force and steady blocks, for the summary's SHAPE tile."""
    x0, y0, x1, y1 = box
    mid, w = (x0 + x1) / 2, (x1 - x0) * STEADY_SHARE
    shift = max(-BAR_RANGE, min(BAR_RANGE, balance)) * SHIFT_PER_UNIT * w
    ym = (y0 + y1) // 2
    _block(draw, mid - w / 2, ym + 1, mid + w / 2, y1, fill=FAINT)  # steady, below
    _block(draw, mid - w / 2 + shift, y0, mid + w / 2 + shift, ym - 1, fill=colour)  # this interval, above


def _mini_meter(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], pct: int,
                colour: tuple | None = None) -> None:
    """A thin version of the finish meter's bar, for the summary's FINISH tile."""
    x0, y0, x1, y1 = box

    def at(p: float) -> float:
        p = max(METER_MIN_PCT, min(METER_MAX_PCT, p))
        return x0 + (x1 - x0) * (p - METER_MIN_PCT) / (METER_MAX_PCT - METER_MIN_PCT)

    draw.rectangle(box, fill=FAINT)
    draw.rectangle((x0, y0, at(pct), y1), fill=colour or finish_colour(pct))
    draw.line((at(0), y0 - 2, at(0), y1 + 2), fill=FG, width=2)


def interval_screen(status: dict, now: float, uptime: float) -> Image.Image:
    """During an effort: time and watts, a finish meter, and one cue."""
    image, draw = _canvas()
    _hints(draw, "rowing")
    live = status["coach"]["live"]
    label = f"{live['intensity'].upper()}  \u00b7  {live['index']}" if live.get("intensity") else f"INTERVAL {live['index']}"
    draw.text((WIDTH - 6, 4), label, font=font(15, True), fill=DIM, anchor="rt")
    clock, watts = format_time(live["elapsed_s"]), f"{live['power_w']} W"
    size = min(_fit(draw, clock, 44, WIDTH // 2 - 14, True).size, _fit(draw, watts, 44, WIDTH // 2 - 14, True).size)
    draw.text((8, 54), clock, font=font(size, True), fill=FG, anchor="lm")  # the same size, side by side
    draw.text((WIDTH - 8, 54), watts, font=font(size, True), fill=FG, anchor="rm")
    # The force curve, as in the SmartRow app: each stroke over your steady shape (grey), the force
    # missing against steady filled in light grey. A shorter stroke ends before the steady curve does.
    stroke = live.get("stroke_curve") or live.get("curve") or []
    curve_colour = CUES.get(live.get("cue"), ("", FG))[1] if live.get("cue") else FG
    _plot_curves(draw, (4, 84, WIDTH - 4, 179), stroke, live.get("steady_curve") or [], curve_colour, "both")
    if live.get("balance") is not None and live.get("length_delta_cm") is not None:
        length = f"length {live['length_delta_cm']:+d} cm".replace("-", "\u2212")
        draw.text((12, 89), length, font=font(12), fill=DIM)

    text, colour = CUES.get(live.get("cue"), ("…", DIM))
    banner = (4, 183, WIDTH - 4, 216)
    if colour == DIM:
        draw.rounded_rectangle(banner, radius=8, fill=TILE)
        draw.text((WIDTH // 2, 199), text, font=font(24, True), fill=DIM, anchor="mm")
    else:
        draw.rounded_rectangle(banner, radius=8, fill=colour)
        draw.text((WIDTH // 2, 199), text, font=_fit(draw, text, 26, WIDTH - 24, True), fill=BG, anchor="mm")
    return image


def summary_screen(status: dict, now: float, uptime: float) -> Image.Image:
    """After an effort: its average curve against steady, and three numbers."""
    image, draw = _canvas()
    _hints(draw, "rowing", "Close")
    last = status["coach"]["last"]
    legs_late = bool(last.get("legs_late"))
    if last.get("balance") is not None:  # the same rule as the bars: green inside the cue markers
        off = abs(last["balance"]) >= 1 or last.get("length_balance", 0) <= -1
        colour = AMBER if off else GREEN
    else:
        colour = AMBER if legs_late else RATING_COLOURS.get(last.get("rating"), FG)
    title = f"{last['intensity'].upper()}  ·  INTERVAL {last['index']}" if last.get("intensity") else f"INTERVAL {last['index']}"
    draw.text((8, 28), title, font=font(15, True), fill=DIM, anchor="lt")
    headline = f"{format_time(last['duration_s'])}  ·  {last['power_w']} W"
    draw.text((WIDTH // 2, 62), headline, font=_fit(draw, headline, 34, WIDTH - 16, True), fill=FG, anchor="mm")
    _plot_curves(draw, (4, 84, WIDTH - 4, 160), last.get("curve") or [], last.get("steady_curve") or [], colour,
                 "both")

    delta = last.get("length_delta_cm", 0)
    # A late push: where the force peaked says more than the finish figure (which a late
    # push inflates).
    bal = last.get("balance")
    if bal is None:  # saved before the shape bar existed
        first = ("FINISH", f"{last['finish_pct']:+d}%", colour)
    else:
        word = "LATE" if bal >= 1 else "FADED" if bal <= -1 else "GOOD"
        first = ("SHAPE", word, GREEN if abs(bal) < 1 else AMBER)
    tiles = [
        first,
        ("LENGTH", f"{round(last['length_cm'])} cm", FG),
        ("PEAK", f"{last['peak_n']} N", FG),
    ]
    tile_w, gap, y0, y1 = 100, 6, 165, 215
    for i, (label, value, value_colour) in enumerate(tiles):
        x0 = 5 + i * (tile_w + gap)
        draw.rounded_rectangle((x0, y0, x0 + tile_w, y1), radius=8, fill=TILE)
        draw.text((x0 + 8, y0 + 5), label, font=font(12), fill=DIM)
        size, at_y = (20, y0 + 26) if label == "SHAPE" else (24, y0 + 30)
        draw.text((x0 + tile_w // 2, at_y), value, font=_fit(draw, value, size, tile_w - 10, True),
                  fill=value_colour, anchor="mm")
        if label == "SHAPE":
            _mini_shape(draw, (x0 + 10, y1 - 12, x0 + tile_w - 10, y1 - 4), bal, value_colour)
        elif label == "FINISH":
            _mini_meter(draw, (x0 + 8, y1 - 8, x0 + tile_w - 8, y1 - 4), last["finish_pct"],
                        AMBER if legs_late else None)
    if legs_late:
        draw.text((WIDTH - 6, HEIGHT - 4),
                  f"LATE PUSH \u00b7 peak {last.get('peak_at_pct', 0)}%, Easy {last.get('steady_peak_at_pct', 0)}%",
                  font=font(12, True), fill=AMBER, anchor="rd")
    elif bal is not None and bal <= -1:
        draw.text((WIDTH - 6, HEIGHT - 4), f"FINISH {last['finish_pct']:+d}% vs Easy".replace("-", "\u2212"),
                  font=font(12, True), fill=AMBER, anchor="rd")
    else:
        draw.text((WIDTH - 6, HEIGHT - 4), f"{delta:+.0f} cm vs Easy", font=font(12), fill=DIM, anchor="rd")
    return image


def hold_screen(progress: float, title: str = "Keep holding to switch off", colour: tuple | None = None) -> Image.Image:
    image, draw = _canvas()
    _centered(draw, [(title, 22, True, FG), ("Let go to cancel", 16, False, DIM)], centre_y=96)
    x0, y0, x1, y1 = 30, 150, WIDTH - 30, 174
    draw.rounded_rectangle((x0, y0, x1, y1), radius=12, outline=DIM, width=2)
    filled = x0 + int((x1 - x0) * max(0.0, min(1.0, progress)))
    if filled > x0 + 6:
        draw.rounded_rectangle((x0, y0, filled, y1), radius=12, fill=colour or AMBER)
    return image


UPLOAD_WORDS = {  # how each destination stands with the newest session (uploads.Uploader.snapshot)
    "sent": ("sent", GREEN), "failed": ("failed - see --status", RED), "retrying": ("will try again later", AMBER),
    "waiting": ("waiting", DIM), "later": ("waiting", DIM), "rowing": ("waiting", DIM),
}


def _upload_rows(draw: ImageDraw.ImageDraw, status: dict | None, top: int) -> int:
    """One line per destination: sent, sending..., waiting, will try again later, failed."""
    ups = (status or {}).get("uploads") or {}
    states = ((ups.get("latest") or {}).get("dests")) or {}
    y = top
    for label in ups.get("configured") or []:
        state = states.get(label)
        if state is None:
            continue  # not for this session (rowed before it was set up, or from a CSV)
        if ups.get("sending") == label and state != "sent":
            word, colour = "sending\u2026", FG
        else:
            word, colour = UPLOAD_WORDS.get(state, (state, DIM))
        draw.ellipse((16, y + 5, 28, y + 17), fill=colour if colour != DIM else None, outline=colour, width=2)
        name = label.replace(" activity", "").replace(" note", " note")  # "intervals.icu", "Strava"
        draw.text((38, y + 11), name, font=_fit(draw, name, 17, 150, True), fill=FG, anchor="lm")
        draw.text((WIDTH - 12, y + 11), word, font=_fit(draw, word, 15, 128), fill=colour, anchor="rm")
        y += 30
    return y


def finish_screen(status: dict | None, session: str | None, acked: bool) -> Image.Image:
    """After Y (Done): the session saved as finished, and its uploads as they go."""
    image, draw = _canvas()
    _button(draw, "X", "close", "Close")
    if not acked:
        _centered(draw, [("Finishing\u2026", 26, True, FG)], centre_y=110)
        return image
    if session is None:
        _centered(draw, [("No session to finish", 22, True, FG),
                         ("Row at least 30 strokes first", 15, False, DIM)], centre_y=110)
        return image
    draw.text((WIDTH // 2, 48), "Session finished", font=font(24, True), fill=GREEN, anchor="mm")
    y = _upload_rows(draw, status, 80)
    if y == 80:
        _centered(draw, [("Saved on the Pi", 18, True, FG), ("Summary on the web page", 15, False, DIM)],
                  centre_y=130)
    return image


UPLOAD_NAMES = {"intervals_activity": "intervals.icu", "intervals_note": "intervals.icu", "strava": "Strava"}


def discard_screen(status: dict | None, acked: bool) -> Image.Image:
    """After X was held: the session deleted (or there was none), and what that does not undo."""
    image, draw = _canvas()
    if not acked:
        _centered(draw, [("Discarding\u2026", 26, True, FG)], centre_y=110)
        return image
    result = (status or {}).get("finish") or {}
    lines = [("Session discarded", 24, True, AMBER),
             ("Not kept, not uploaded" if result.get("session") else "Nothing was saved yet", 16, False, FG)]
    sent = sorted({UPLOAD_NAMES.get(d, d) for d in result.get("uploaded") or []})
    if sent:
        lines.append((f"Already on {' and '.join(sent)}: delete it there", 15, False, AMBER))
    lines.append(("Start a new workout in the SmartRow app", 14, False, DIM))
    lines.append(("to reset the pulley's metres", 14, False, DIM))
    _centered(draw, lines, centre_y=120)
    return image


def upload_off_screen(status: dict | None, closing: bool) -> Image.Image:
    """A held with uploads still to go: upload first, then switch off by itself."""
    image, draw = _canvas()
    if closing:
        _centered(draw, [("Switching off", 24, True, FG)], centre_y=48)
    else:
        _button(draw, "A", "power", "Off now")
        _button(draw, "B", "close", "Cancel")
        draw.text((WIDTH // 2, 48), "Uploading, then off", font=font(22, True), fill=FG, anchor="mm")
    y = _upload_rows(draw, status, 80)
    ups = (status or {}).get("uploads") or {}
    if closing and ups.get("retrying"):
        draw.text((WIDTH // 2, y + 14), "The rest goes next time the Pi is on", font=_fit(draw, "x" * 36, 14, WIDTH - 24),
                  fill=DIM, anchor="mm")
    return image


def blank() -> Image.Image:
    return _canvas()[0]


def switching_off_screen() -> Image.Image:
    image, draw = _canvas()
    _centered(draw, [("Switching off…", 28, True, AMBER), ("Unplug when the screen", 18, False, FG),
                     ("goes dark", 18, False, FG)])
    return image


def final_screen(kind: str) -> Image.Image:
    """Drawn by the shutdown hook, the last thing that runs before the Pi stops."""
    image, draw = _canvas()
    if kind == "reboot":
        _centered(draw, [("Restarting…", 28, True, FG)])
    else:
        _centered(draw, [("Safe to unplug", 30, True, GREEN)])
    return image
