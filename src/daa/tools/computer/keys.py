"""Key combinations and typed text: canonicalisation, and two deny-lists.

A key combination is the one UI action whose readback is structurally weak.
"the Delete Account button in Account Settings in Safari" says what the thing
IS; "command Return in Mail" says only what the keystroke is, because what it
MEANS is defined by the app. That asymmetry is why `ui_key` is not grantable --
a grant cannot answer in advance for a sentence that does not describe an
effect -- and why the deny-list here is a refusal rather than a consequence.

The canonicaliser splits on `+`, `-`, `,` and whitespace. Splitting on `+`
alone is a real bug with a real exploit: a caller that writes
"control-option-delete" walks straight through a gate that only understands
"control+option+delete".
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

MODIFIERS = {
    "cmd": "command", "command": "command", "⌘": "command", "meta": "command",
    "super": "command", "win": "command", "apple": "command",
    "opt": "option", "option": "option", "alt": "option", "⌥": "option",
    "ctrl": "control", "control": "control", "⌃": "control",
    "shift": "shift", "⇧": "shift",
    "fn": "fn", "function": "fn",
}

# US virtual key codes. Used ONLY for key combinations, where the code is the
# right thing: a shortcut is defined by the physical position. Literal text
# never goes through here -- see ax.type_text and the note there about layouts.
KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "equals": 24, "9": 25,
    "7": 26, "minus": 27, "8": 28, "0": 29, "rightbracket": 30, "o": 31, "u": 32,
    "leftbracket": 33, "i": 34, "p": 35, "return": 36, "enter": 36, "l": 37, "j": 38,
    "quote": 39, "k": 40, "semicolon": 41, "backslash": 42, "comma": 43, "slash": 44,
    "n": 45, "m": 46, "period": 47, "tab": 48, "space": 49, "grave": 50,
    "delete": 51, "backspace": 51, "escape": 53, "esc": 53,
    "forwarddelete": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
    "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}

# Spoken names for keys, so the readback is a sentence rather than a keymap.
SPOKEN = {
    "return": "Return", "enter": "Return", "escape": "Escape", "esc": "Escape",
    "delete": "Delete", "backspace": "Delete", "tab": "Tab", "space": "Space",
    "up": "the up arrow", "down": "the down arrow", "left": "the left arrow",
    "right": "the right arrow", "forwarddelete": "Forward Delete",
}
SPOKEN_MODIFIERS = {
    "command": "command", "option": "option", "control": "control",
    "shift": "shift", "fn": "fn",
}
_MODIFIER_ORDER = ("control", "option", "shift", "command", "fn")

# Combinations that are never worth the risk of a mishearing. Each one is a
# whole-machine action with no target: there is no element to name, so there is
# no honest readback, so there is no confirmation worth collecting.
DENIED_COMBOS: dict[frozenset[str], str] = {
    frozenset({"command", "shift", "delete"}): "that empties the Trash",
    frozenset({"command", "option", "delete"}): "that empties the Trash without asking",
    frozenset({"command", "control", "q"}): "that locks the screen",
    frozenset({"command", "shift", "q"}): "that logs you out",
    frozenset({"command", "option", "shift", "q"}): "that logs you out immediately",
    frozenset({"command", "option", "escape"}): "that opens Force Quit",
    frozenset({"command", "control", "power"}): "that restarts the machine",
}

# Text that is a command, not a message. Checked BEFORE any approval is asked
# for, because the approval for "type this into Terminal" is not the approval
# the sentence would have described.
DENIED_TEXT = (
    ("curl", "|", "bash"),
    ("curl", "|", "sh"),
    ("wget", "|", "bash"),
    ("wget", "|", "sh"),
    ("sudo", "rm", "-rf"),
    ("rm", "-rf", "/"),
    (":(){", ":|:&", "};:"),
)

_SPLIT = re.compile(r"[\s+\-,]+")
# The glyph spellings arrive with no separator at all -- "\u2318\u21e7delete" is one
# token until these are peeled off, and a deny-list that only sees one token
# does not fire.
_GLYPHS = re.compile(r"([\u2318\u21e7\u2325\u2303])")


@dataclass(frozen=True, slots=True)
class KeyCombo:
    modifiers: tuple[str, ...] = ()
    key: str = ""
    keycode: int = 0
    error: str = ""
    denied_reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and not self.denied_reason

    def spoken(self) -> str:
        parts = [SPOKEN_MODIFIERS[m] for m in self.modifiers if m in SPOKEN_MODIFIERS]
        key = SPOKEN.get(self.key, self.key.upper() if len(self.key) == 1 else self.key)
        parts.append(key)
        return " ".join(parts)


def parse_combo(text: str) -> KeyCombo:
    """"command s", "cmd-shift-S", "⌘+S" -> one canonical combination."""
    raw = str(text or "").strip()
    if not raw:
        return KeyCombo(error="you did not say which keys")
    tokens = [t for t in _SPLIT.split(_GLYPHS.sub(r" \1 ", raw.lower())) if t]
    if not tokens:
        return KeyCombo(error="you did not say which keys")
    modifiers: list[str] = []
    keys: list[str] = []
    for token in tokens:
        canonical = MODIFIERS.get(token)
        if canonical:
            if canonical not in modifiers:
                modifiers.append(canonical)
        else:
            keys.append(token)
    if not keys:
        return KeyCombo(error="that is only modifier keys, with nothing to press")
    if len(keys) > 1:
        return KeyCombo(error="I can only press one key at a time, with modifiers")
    key = keys[0]
    ordered = tuple(m for m in _MODIFIER_ORDER if m in modifiers)

    denial = DENIED_COMBOS.get(frozenset({*ordered, key}))
    if denial:
        return KeyCombo(modifiers=ordered, key=key, denied_reason=denial)

    keycode = KEYCODES.get(key)
    if keycode is None:
        return KeyCombo(modifiers=ordered, key=key, error=f"I do not know the key {key!r}")
    return KeyCombo(modifiers=ordered, key=key, keycode=int(keycode))


def denied_text_reason(text: str) -> str:
    """Is this text a command rather than something someone would write?"""
    flat = " ".join(str(text or "").split()).lower()
    if not flat:
        return ""
    squeezed = flat.replace(" ", "")
    for needles in DENIED_TEXT:
        if all(n.replace(" ", "") in squeezed for n in needles):
            return "that text is a shell command, not something I will type for you"
    return ""


def describe_text(text: str, limit: int = 120) -> str:
    """The literal text, for `consequences`. Truncated, never paraphrased.

    `run_shortcut` set the precedent: the body of the thing being sent is part
    of what the user is agreeing to, so it goes in the sentence.
    """
    flat = " ".join(str(text or "").split())
    if not flat:
        return ""
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def normalise_steps(steps: Sequence[object]) -> list[dict[str, str]]:
    """Coerce whatever the model produced into a list of step dicts."""
    out: list[dict[str, str]] = []
    for raw in steps or ():
        if not isinstance(raw, dict):
            out.append({"action": "", "error": "a step was not an object"})
            continue
        out.append(
            {
                "action": str(raw.get("action") or "").strip().lower(),
                "target": str(raw.get("target") or "").strip(),
                "text": str(raw.get("text") or ""),
                "keys": str(raw.get("keys") or "").strip(),
            }
        )
    return out


__all__ = [
    "DENIED_COMBOS",
    "DENIED_TEXT",
    "KEYCODES",
    "MODIFIERS",
    "KeyCombo",
    "denied_text_reason",
    "describe_text",
    "normalise_steps",
    "parse_combo",
]
