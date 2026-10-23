"""Органы управления для веб-интерфейса (страница Controls): мини-клавиатура (раскладка — settings.keypad,
действия — fnav/core/keypad.py), геймпад, кнопка на GPS-модуле."""

from __future__ import annotations

import glob

from fnav.core.games import PAD_GLOB
from fnav.core.keypad import ACTIONS, PLACE_LABELS
from fnav.models import KeypadSettings

GAMEPAD = [
    {"control": "Hold PS (~1 s)", "title": "Games menu", "hint": "Only when the vehicle is stopped"},
    {"control": "D-pad / left stick", "title": "Choose a game", "hint": ""},
    {"control": "✕ (Cross)", "title": "Play", "hint": "Connects the Bluetooth speaker first"},
    {"control": "○ (Circle) / PS", "title": "Close the menu", "hint": ""},
    {"control": "Share + Options (hold 1 s)", "title": "Exit the game to the map", "hint": "In any game"},
    {"control": "PS in a game", "title": "RetroArch menu", "hint": "Game Boy and Doom: save state, Close Content"},
]

BUTTON = [
    {"control": "Short press", "title": "Video output on / off", "hint": "Beep and LED confirm"},
    {"control": "Long press", "title": "Protect recording", "hint": "The current recording is never deleted"},
]


def controls_info(keypad_connected: bool, bindings: KeypadSettings) -> dict:
    return {
        "keypad": {"connected": keypad_connected,
                   "places": [{"place": p, "label": lbl, "action": getattr(bindings, p)}
                              for p, lbl in PLACE_LABELS.items()],
                   "actions": ACTIONS, "defaults": KeypadSettings().model_dump()},
        "gamepad": {"connected": bool(glob.glob(PAD_GLOB)), "controls": GAMEPAD},
        "button": {"controls": BUTTON},
        "notes": ["The macro pad and the gamepad share the USB port: plug in one or the other.",
                  "Games also stop by themselves when the vehicle starts moving."],
    }
