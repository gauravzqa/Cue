"""The readback. This module is the whole point of the capability.

`run_applescript` already established the rule this file applies to a new
tool: the sentence the user answers is derived from WHAT THE THING IS, never
from what the model called it. There, the model's `purpose` is untrusted
because a script that empties an inbox can introduce itself as "check what time
my next meeting is". Here the identical attack is one step shorter -- the model
asks to press "the OK button" and the tree says `AXButton` titled
"Delete Account" inside the window "Account Settings" in Safari.

So:

    the spoken target is composed from AX attributes ONLY

and the model's phrasing travels in `args` as `requested_target`, where the
audit row and the confirmation card can show it side by side with the truth.
If those two ever merge, the tier system becomes decorative: every floor, every
hint and every consequence is spent buying one sentence, and the sentence would
be one the model wrote.

Nothing in here reads `AXValue` -- see the note in `ax.Element.label`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from daa.contracts import RiskTier
from daa.tools.computer.ax import (
    SYSTEM_BUNDLE_IDS,
    SYSTEM_BUNDLE_PREFIXES,
    TERMINAL_BUNDLE_IDS,
    Element,
)

# Words whose presence in the TREE'S OWN title changes what a yes means. The
# direct analogue of applescript.DANGEROUS, and matched against the element's
# real title -- never against the model's phrasing, which is the whole point.
#
# English-only and title-based, which is a known hole: a button labelled
# "Continue" that closes an account reads back as safe. That is why the WINDOW
# title is part of the target string rather than decoration -- "press Continue
# in Delete Account in Safari" carries the context the button label lost.
DESTRUCTIVE: Mapping[str, str] = {
    "delete": "delete",
    "remove": "remove",
    "erase": "erase",
    "discard": "discard",
    "trash": "trash",
    "destroy": "destroy",
    "wipe": "wipe",
    "format": "format",
    "reset": "reset",
    "revoke": "revoke",
    "deactivate": "deactivate",
    "unsubscribe": "unsubscribe",
    "send": "send",
    "pay": "pay",
    "buy": "buy",
    "purchase": "purchase",
    "subscribe": "subscribe",
    "confirm": "confirm",
    "publish": "publish",
    "post": "post",
    "submit": "submit",
    "transfer": "transfer",
    "allow": "allow",
    "grant": "grant",
    "approve": "approve",
    "overwrite": "overwrite",
    "replace": "replace",
}
# Multi-word phrases, checked against the normalised title as substrings.
DESTRUCTIVE_PHRASES = (
    "don t save",
    "do not save",
    "sign out",
    "log out",
    "empty trash",
    "delete account",
    "move to trash",
    "turn off",
)

_WORD = re.compile(r"[a-z0-9]+")


def _normalise(text: str) -> str:
    return " ".join(_WORD.findall(str(text or "").lower()))


def destructive_words(title: str) -> tuple[str, ...]:
    """Which destructive words the TREE says. Order-stable, deduplicated."""
    flat = _normalise(title)
    if not flat:
        return ()
    words = flat.split()
    hits: list[str] = []
    for word in words:
        reason = DESTRUCTIVE.get(word)
        if reason and reason not in hits:
            hits.append(reason)
    for phrase in DESTRUCTIVE_PHRASES:
        if phrase in flat and phrase not in hits:
            hits.append(phrase)
    return tuple(hits)


def is_system_window(element: Element) -> bool:
    """Does this element belong to macOS itself rather than to a user app?

    A press inside one of these is a press on a security or permission
    decision. Naming it is not enough; it has to go on a screen.
    """
    bundle = (element.bundle_id or "").strip()
    if not bundle:
        return False
    return bundle in SYSTEM_BUNDLE_IDS or bundle.startswith(SYSTEM_BUNDLE_PREFIXES)


def is_terminal(element: Element) -> bool:
    return (element.bundle_id or "") in TERMINAL_BUNDLE_IDS


# ---------------------------------------------------------------------------
# Composing the spoken target
# ---------------------------------------------------------------------------

_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


def role_noun(element: Element) -> str:
    """What kind of thing this is, in words.

    `AXRoleDescription` is the app's own localised answer and is preferred.
    The camel-case fallback ("AXMenuItem" -> "menu item") exists so that an app
    which omits the description still produces a noun rather than a raw
    constant read aloud letter by letter.
    """
    described = " ".join(str(element.role_description or "").split()).strip().lower()
    if described:
        return described
    role = str(element.subrole or element.role or "")
    role = role.removeprefix("AX")
    if not role:
        return ""
    return _CAMEL.sub(" ", role).lower().strip()


def compose_target(element: Element) -> str | None:
    """The spoken target, from AX attributes only, or None if unnameable.

    None is not a weaker description -- it is a refusal. An action that cannot
    be named from the tree at resolve time is not performed.
    """
    label = element.label
    if not label:
        return None
    noun = role_noun(element)
    # "the Save button", but "the Save menu item" -> not "the Save menu item
    # menu item" when an app helpfully puts the role in the title already.
    if noun and _normalise(label).endswith(_normalise(noun)):
        head = f"the {label}"
    elif noun:
        head = f"the {label} {noun}"
    else:
        head = f"the {label}"
    parts = [head]
    if element.window_title:
        parts.append(f"in {element.window_title}")
    if element.app:
        parts.append(f"in {element.app}")
    return " ".join(parts)


def searchable(element: Element) -> str:
    """What a spoken `target` is matched against. The label, and only the label.

    Matching against the composed sentence would let the app name and the
    window title drag in every control in the app for the query "safari".
    """
    return element.label


# ---------------------------------------------------------------------------
# Consequences and the floor hint
# ---------------------------------------------------------------------------

UNDO_CONSEQUENCE = "I can't undo this -- whether it can be taken back is up to the app"


def assess(
    element: Element,
    *,
    app_query: str = "",
    verb: str = "press",
) -> tuple[RiskTier | None, dict[str, str]]:
    """Everything resolve() can COMPUTE that changes what a yes means.

    Returns `(floor_hint, consequences)`.

    The hint is set by deterministic code that read the real tree, so policy
    can take `max(spec.floor, floor_hint, derived)` without asking a judgment
    model whether a button labelled "Delete Account" is dangerous. It only ever
    raises. A resolver that fails to recognise something degrades to the tool's
    ordinary floor, which is why the dangerous MODES have their own tools and
    their own static floors and this handles the residue.
    """
    consequences: dict[str, str] = {}
    hint: RiskTier | None = None

    words = destructive_words(element.title or element.label)
    if words:
        # Says the label AND says we classified it. The target sentence
        # already names the control, so repeating the label alone would be
        # noise in a spoken readback; what this adds is the judgment, and for
        # a multi-step plan it is what marks WHICH step is the dangerous one.
        consequences["wording"] = f"{element.label} is a destructive label"
        hint = RiskTier.CONFIRM_VISUAL

    if is_system_window(element):
        consequences["system"] = "this is a macOS permission dialog, not an app window"
        hint = RiskTier.CONFIRM_VISUAL
    elif element.in_alert and element.is_default_button:
        consequences["modal"] = "this is the default button of an alert"
        hint = RiskTier.CONFIRM_VISUAL
    elif element.in_alert:
        consequences["modal"] = "this is inside an alert"

    if (
        app_query
        and _normalise(app_query)
        and element.app
        and _normalise(app_query) not in _normalise(element.app)
    ):
        consequences["app"] = f"this is in {element.app}, not in {app_query}"

    if verb:
        consequences["focus"] = f"this will bring {element.app} to the front" if _raises(
            element
        ) else f"this happens inside {element.app} without bringing it to the front"

    consequences["undo"] = UNDO_CONSEQUENCE
    return hint, consequences


def _raises(element: Element) -> bool:
    """Will acting on this element pull the app in front of what you are doing?

    AXPress presses an element in place. Anything that has to go through a
    synthetic event has to have the app frontmost first, and raising a window
    is a visible side effect nobody consented to -- so it is spoken, separately
    from the press itself.
    """
    from daa.tools.computer.ax import PRESSABLE_ACTIONS

    return not any(a in PRESSABLE_ACTIONS for a in element.actions)


__all__ = [
    "DESTRUCTIVE",
    "DESTRUCTIVE_PHRASES",
    "UNDO_CONSEQUENCE",
    "assess",
    "compose_target",
    "destructive_words",
    "is_system_window",
    "is_terminal",
    "role_noun",
    "searchable",
]
