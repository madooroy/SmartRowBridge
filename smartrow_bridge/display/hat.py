"""Pimoroni Display HAT Mini: 320 x 240 LCD (ST7789 on SPI0 CE1), four buttons, one RGB LED.

Pins (BCM): LCD data/command 9, backlight 13; buttons A 5, B 6, X 16, Y 24 (pressed =
low); LED red 17, green 27, blue 22 (lit = low). These are the values in Pimoroni's
displayhatmini library; the LCD driver is Pimoroni's st7789 package, the pins go
through libgpiod.
"""

from __future__ import annotations

import gpiod
import gpiodevice
from gpiod.line import Bias, Direction, Value
from PIL import Image
from st7789 import ST7789

BUTTONS = {"A": 5, "B": 6, "X": 16, "Y": 24}
LED_PINS = {"r": 17, "g": 27, "b": 22}
SPI_PORT, SPI_CS, DC_PIN, BACKLIGHT_PIN = 0, 1, 9, 13
WIDTH, HEIGHT = 320, 240

_INPUT = gpiod.LineSettings(direction=Direction.INPUT, bias=Bias.PULL_UP)
_LED_OFF = gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.ACTIVE)


class DisplayHat:
    def __init__(self, buttons: bool = True, led: bool = True) -> None:
        self.lcd = ST7789(
            port=SPI_PORT, cs=SPI_CS, dc=DC_PIN, backlight=BACKLIGHT_PIN,
            width=WIDTH, height=HEIGHT, rotation=180, spi_speed_hz=60_000_000,
        )
        self._buttons = {name: gpiodevice.get_pin(pin, f"button-{name}", _INPUT)
                         for name, pin in BUTTONS.items()} if buttons else {}
        self._led = {colour: gpiodevice.get_pin(pin, f"led-{colour}", _LED_OFF)
                     for colour, pin in LED_PINS.items()} if led else {}
        self._last_frame: bytes | None = None

    def show(self, image: Image.Image) -> None:
        """Send the image to the LCD, unless it is the frame already shown."""
        frame = image.tobytes()
        if frame != self._last_frame:
            self.lcd.display(image)
            self._last_frame = frame

    def pressed(self, name: str) -> bool:
        pin = self._buttons.get(name)
        if pin is None:
            return False
        lines, offset = pin
        return lines.get_value(offset) == Value.INACTIVE

    def set_led(self, r: bool = False, g: bool = False, b: bool = False) -> None:
        for colour, on in (("r", r), ("g", g), ("b", b)):
            pin = self._led.get(colour)
            if pin is not None:
                lines, offset = pin
                lines.set_value(offset, Value.INACTIVE if on else Value.ACTIVE)

    def backlight(self, on: bool) -> None:
        self.lcd.set_backlight(on)
