"""Draws the screens for the 320 x 240 Display HAT Mini. Pure Pillow: no hardware needed.

Every function returns a PIL image; the hardware module only copies it to the LCD,
and `python -m smartrow_bridge.display --preview DIR` saves them as PNG files.

Button hints are small tabs in the corners next to the buttons (A top left,
B bottom left, X top right, Y bottom right): a power symbol on A, and on B the
screen it switches to.
"""

from __future__ import annotations

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


_ICONS = {"power": _icon_power, "rowing": _icon_rowing, "status": _icon_status, "curve": _icon_curve}


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
        draw.text((WIDTH - 4, HEIGHT - 4), address, font=_fit(draw, address, 13, 170), fill=DIM, anchor="rd")

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
    y = 30
    for label, text, colour in rows:
        draw.ellipse((14, y + 8, 32, y + 26), fill=colour if colour != FAINT else None,
                     outline=colour, width=2)
        draw.text((44, y), label, font=_fit(draw, label, 21, WIDTH - 54, bold=True), fill=FG)
        draw.text((44, y + 28), text, font=_fit(draw, text, 18, WIDTH - 54), fill=colour)
        y += 60
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
    live = (lambda v: v) if rowing else (lambda v: "–")
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
        colour = FG if value != "–" else FAINT
        draw.text((x0 + tile_w // 2, y0 + 53), value, font=_fit(draw, value, 46, tile_w - 16, bold=True),
                  fill=colour, anchor="mm")

    footer = f"{format_time(m.get('elapsed_s', 0))}   ·   {m.get('stroke_count', 0)} strokes"
    if not rowing:
        footer += "   ·   paused"
    draw.text((WIDTH // 2, top + 2 * tile_h + gap + 14), footer, font=font(16), fill=FG if rowing else DIM,
              anchor="mm")
    return image


# ---------------------------------------------------------------- interval coach

CUES = {
    "finish": ("FINISH THE STROKE", AMBER),
    "length": ("LONGER STROKES", AMBER),
    "good": ("GOOD", GREEN),
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
                 colour: tuple) -> None:
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

    mid = x0 + (x1 - x0) * (FORCE_HALF + 1) / (len(curve or steady or [0]) + 1)
    if curve:
        pts = points(curve)
        draw.polygon([(x0, y1), *pts, (x1, y1)], fill=_shade(colour))
    if curve and steady:
        # Back half of the drive: fill the force missing against the steady shape.
        sp = points(steady)
        for i in range(FORCE_HALF + 1, len(pts) - 1):
            a, b, c, d = pts[i], pts[i + 1], sp[i + 1], sp[i]
            if c[1] < b[1] or d[1] < a[1]:  # steady above the effort here (smaller y = higher)
                draw.polygon([a, b, (b[0], min(b[1], c[1])), (a[0], min(a[1], d[1]))], fill=GAP)
    if curve:
        draw.line(pts, fill=colour, width=3, joint="curve")
    if steady:
        draw.line(points(steady), fill=DIM, width=2, joint="curve")
        draw.text((x1, y0), "steady shape", font=font(12), fill=DIM, anchor="rt")
    if curve:
        for y in range(y0 + 16, y1, 8):  # dashed line where the finish half starts
            draw.line((mid, y, mid, y + 3), fill=FAINT, width=1)
        draw.text((mid + 4, y1 - 2), "finish", font=font(11), fill=DIM, anchor="lb")


def _finish_meter(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], pct: int | None,
                  length_delta: int | None) -> None:
    """Back-half force against steady: a big number and a bar with a tick at steady."""
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(box, radius=8, fill=TILE)
    colour = finish_colour(pct)
    draw.text((x0 + 10, y0 + 8), "FINISH vs STEADY", font=font(13), fill=DIM)
    value = "…" if pct is None else f"{pct:+d}%"
    draw.text((x1 - 10, y0 + 22), value, font=font(34, True), fill=colour, anchor="rm")

    bx0, bx1, by0, by1 = x0 + 12, x1 - 12, y0 + 46, y0 + 66

    def at(p: float) -> float:
        p = max(METER_MIN_PCT, min(METER_MAX_PCT, p))
        return bx0 + (bx1 - bx0) * (p - METER_MIN_PCT) / (METER_MAX_PCT - METER_MIN_PCT)

    for lo, hi, zone in ((METER_MIN_PCT, OK_FINISH_PCT, RED), (OK_FINISH_PCT, GOOD_FINISH_PCT, AMBER),
                         (GOOD_FINISH_PCT, METER_MAX_PCT, GREEN)):
        draw.rectangle((at(lo), by0, at(hi), by1), fill=_shade(zone, 0.3))
    if pct is not None:
        draw.rectangle((bx0, by0 + 4, at(pct), by1 - 4), fill=colour)
        draw.polygon([(at(pct) - 7, by0 - 6), (at(pct) + 7, by0 - 6), (at(pct), by0 + 2)], fill=FG)
    draw.line((at(0), by0 - 3, at(0), by1 + 3), fill=FG, width=2)
    draw.text((at(0), by1 + 3), "steady", font=font(11), fill=DIM, anchor="mt")
    if length_delta is not None:
        draw.text((bx0, by1 + 3), f"length {length_delta:+d} cm", font=font(11), fill=DIM, anchor="lt")


def _mini_meter(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], pct: int) -> None:
    """A thin version of the finish meter's bar, for the summary's FINISH tile."""
    x0, y0, x1, y1 = box

    def at(p: float) -> float:
        p = max(METER_MIN_PCT, min(METER_MAX_PCT, p))
        return x0 + (x1 - x0) * (p - METER_MIN_PCT) / (METER_MAX_PCT - METER_MIN_PCT)

    draw.rectangle(box, fill=FAINT)
    draw.rectangle((x0, y0, at(pct), y1), fill=finish_colour(pct))
    draw.line((at(0), y0 - 2, at(0), y1 + 2), fill=FG, width=2)


def interval_screen(status: dict, now: float, uptime: float) -> Image.Image:
    """During an effort: time and watts, a finish meter, and one cue."""
    image, draw = _canvas()
    _hints(draw, "rowing")
    live = status["coach"]["live"]
    draw.text((WIDTH - 6, 4), f"INTERVAL {live['index']}", font=font(15, True), fill=DIM, anchor="rt")
    draw.text((8, 58), format_time(live["elapsed_s"]), font=font(50, True), fill=FG, anchor="lm")
    draw.text((WIDTH - 8, 58), f"{live['power_w']} W", font=font(40, True), fill=FG, anchor="rm")
    _finish_meter(draw, (4, 90, WIDTH - 4, 176), live.get("finish_pct"), live.get("length_delta_cm"))

    text, colour = CUES.get(live.get("cue"), ("…", DIM))
    banner = (4, 181, WIDTH - 4, 215)
    if colour == DIM:
        draw.rounded_rectangle(banner, radius=8, fill=TILE)
        draw.text((WIDTH // 2, 198), text, font=font(24, True), fill=DIM, anchor="mm")
    else:
        draw.rounded_rectangle(banner, radius=8, fill=colour)
        draw.text((WIDTH // 2, 198), text, font=_fit(draw, text, 26, WIDTH - 24, True), fill=BG, anchor="mm")
    return image


def summary_screen(status: dict, now: float, uptime: float) -> Image.Image:
    """After an effort: its average curve against steady, and three numbers."""
    image, draw = _canvas()
    _hints(draw, "rowing", "Close")
    last = status["coach"]["last"]
    colour = RATING_COLOURS.get(last.get("rating"), FG)
    draw.text((8, 28), f"INTERVAL {last['index']}", font=font(15, True), fill=DIM, anchor="lt")
    headline = f"{format_time(last['duration_s'])}  ·  {last['power_w']} W"
    draw.text((WIDTH // 2, 62), headline, font=_fit(draw, headline, 34, WIDTH - 16, True), fill=FG, anchor="mm")
    _plot_curves(draw, (4, 84, WIDTH - 4, 160), last.get("curve") or [], last.get("steady_curve") or [], colour)

    delta = last.get("length_delta_cm", 0)
    tiles = [
        ("FINISH", f"{last['finish_pct']:+d}%", colour),
        ("LENGTH", f"{round(last['length_cm'])} cm", FG),
        ("PEAK", f"{last['peak_n']} N", FG),
    ]
    tile_w, gap, y0, y1 = 100, 6, 165, 215
    for i, (label, value, value_colour) in enumerate(tiles):
        x0 = 5 + i * (tile_w + gap)
        draw.rounded_rectangle((x0, y0, x0 + tile_w, y1), radius=8, fill=TILE)
        draw.text((x0 + 8, y0 + 5), label, font=font(12), fill=DIM)
        draw.text((x0 + tile_w // 2, y0 + 30), value, font=_fit(draw, value, 24, tile_w - 10, True),
                  fill=value_colour, anchor="mm")
        if label == "FINISH":
            _mini_meter(draw, (x0 + 8, y1 - 8, x0 + tile_w - 8, y1 - 4), last["finish_pct"])
    note = f"{delta:+.0f} cm vs steady"
    draw.text((WIDTH - 6, HEIGHT - 4), note, font=font(12), fill=DIM, anchor="rd")
    return image


def hold_screen(progress: float) -> Image.Image:
    image, draw = _canvas()
    _centered(draw, [("Keep holding to switch off", 22, True, FG), ("Let go to cancel", 16, False, DIM)],
              centre_y=96)
    x0, y0, x1, y1 = 30, 150, WIDTH - 30, 174
    draw.rounded_rectangle((x0, y0, x1, y1), radius=12, outline=DIM, width=2)
    filled = x0 + int((x1 - x0) * max(0.0, min(1.0, progress)))
    if filled > x0 + 6:
        draw.rounded_rectangle((x0, y0, filled, y1), radius=12, fill=AMBER)
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
