"""Draws the screens for the 320 x 240 Display HAT Mini. Pure Pillow: no hardware needed.

Every function returns a PIL image; the hardware module only copies it to the LCD,
and `python -m smartrow_bridge.display --preview DIR` saves them as PNG files.

Button hints sit next to the buttons: A top left, B bottom left, X top right,
Y bottom right.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 320, 240

BG = (0, 0, 0)
FG = (240, 240, 240)
DIM = (135, 135, 135)
FAINT = (70, 70, 70)
TILE = (26, 26, 26)
GREEN = (70, 205, 95)
AMBER = (245, 175, 45)
RED = (235, 75, 65)

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


def _hints(draw: ImageDraw.ImageDraw, a: str = "", b: str = "", x: str = "", y: str = "") -> None:
    f = font(13)
    if a:
        draw.text((4, 4), "◀ " + a, font=f, fill=DIM)
    if b:
        draw.text((4, HEIGHT - 4), "◀ " + b, font=f, fill=DIM, anchor="ld")
    if x:
        draw.text((WIDTH - 4, 4), x + " ▶", font=f, fill=DIM, anchor="ra")
    if y:
        draw.text((WIDTH - 4, HEIGHT - 4), y + " ▶", font=f, fill=DIM, anchor="rd")


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
    _hints(draw, a="hold: switch off", b="rowing")
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
    _hints(draw, a="hold: switch off", b="status")
    if bridge_state(status, now, uptime) != "ok":
        _centered(draw, [("No data", 28, True, DIM), ("◀ B shows the status", 16, False, DIM)])
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
    top, tile_w, tile_h, gap = 24, 154, 84, 4
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
    _centered(draw, [("Switching off…", 28, True, AMBER), ("Do not unplug yet", 18, False, FG)])
    return image


def final_screen(kind: str) -> Image.Image:
    """Drawn by the shutdown hook, the last thing that runs before the Pi stops."""
    image, draw = _canvas()
    if kind == "reboot":
        _centered(draw, [("Restarting…", 28, True, FG)])
    else:
        _centered(draw, [("Safe to unplug", 30, True, GREEN),
                         ("once the green light on the Pi", 16, False, DIM),
                         ("has stopped flashing", 16, False, DIM)])
    return image
