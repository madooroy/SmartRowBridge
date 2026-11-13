"""Optional screen (Pimoroni Display HAT Mini) showing the bridge status; see __main__.py."""

from __future__ import annotations

import os

# Turned the other way up (SRB_DISPLAY_FLIP=1), each button sits where its opposite was:
# the top-left button is then the one printed Y, and so on. The screen's button tabs stay
# by position (A top left, B bottom left, X top right, Y bottom right), so each role
# follows its corner, not the letter printed on the board.
FLIPPED_BUTTONS = {"A": "Y", "B": "X", "X": "B", "Y": "A"}


SETTINGS_FILE = "/etc/default/smartrow-bridge"


def flipped(settings_file: str = SETTINGS_FILE) -> bool:
    """SRB_DISPLAY_FLIP: 1 = the HAT is mounted the other way up (picture and buttons turned 180 degrees).

    The screen service has it in its environment; the last picture at power-off (the shutdown
    hook, which runs with no environment) reads it from the settings file instead."""
    value = os.environ.get("SRB_DISPLAY_FLIP")
    if value is None:
        try:
            with open(settings_file, encoding="utf-8") as f:
                for line in f:
                    name, sep, text = line.strip().partition("=")
                    if sep and name.strip() == "SRB_DISPLAY_FLIP":
                        value = text.strip().strip("'\"")
        except OSError:
            value = ""
    return (value or "").strip().lower() in ("1", "true", "yes")


def button_pin_name(corner: str, flip: bool) -> str:
    """The printed name of the button in this corner (A top left, B bottom left, X top right, Y bottom right)."""
    return FLIPPED_BUTTONS[corner] if flip else corner
