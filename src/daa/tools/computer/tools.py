"""The five `ui_*` tools. Named elements in, ToolResult out; no coordinates.

`ui_click(target="the Save button", app="TextEdit")` -- the unit of action is a
named element, and a coordinate is never an argument. The inversion is forced
by the contract rather than chosen: `resolve()` exists to turn loose arguments
into concrete, speakable targets, and if the argument is already `(847, 312)`
then `resolve()` has no work to do and therefore nothing to say.

Everything here obeys four rules that the rest of the package enforces:

* **The readback comes from the tree.** `naming.compose_target` reads AX
  attributes; the model's phrasing rides along in `args["requested_target"]`
  and is never the target. A press the model calls "the OK button" reads back
  as "press the Delete Account button in Account Settings in Safari".
* **Unnameable is unperformable.** No label, no press action, disabled,
  ambiguous, secure field, app not running, empty tree -- all refusals, never
  a weaker sentence and never a screenshot.
* **`run()` re-verifies.** Every tool re-reads the tree and re-finds its
  element by identity before it acts. The screen moves between `resolve()` and
  "yes": two seconds of spoken readback is plenty of time for a sheet to
  appear. If the element the sentence named is gone, the tool stops and says so.
* **No inverse exists.** Command-Z is not an inverse. It is another press whose
  meaning the app defines, it can be a no-op, and it can undo something the
  user did by hand five minutes ago. So every mutating tool here pays the full
  four-part irreversibility price: `irreversible = True`, the `"irreversible"`
  tag, a warning in the activation hint, and a floor of CONFIRM_VOICE or above.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

from daa.contracts import ResolvedAction, RiskTier, ToolResult, ToolSpec
from daa.tools.base import BaseTool, Degraded, as_sentence, param, rank_candidates
from daa.tools.computer.ax import (
    Element,
    Snapshot,
    activate,
    click_center,
    find_by_identity,
    focus,
    focused_element,
    frontmost_pid,
    modifier_mask,
    post_key,
    press,
    reread,
    snapshot,
    token,
    type_text,
)
from daa.tools.computer.keys import (
    denied_text_reason,
    describe_text,
    normalise_steps,
    parse_combo,
)
from daa.tools.computer.naming import (
    UNDO_CONSEQUENCE,
    assess,
    compose_target,
    is_terminal,
    role_noun,
    searchable,
)

# Mirrors windows.MATCH_FLOOR. Below this a fuzzy hit on a control label is a
# coincidence, and pressing the wrong control is the one mistake a user
# notices immediately.
MATCH_FLOOR = 0.55
# Two candidates this close together are not a winner and a runner-up, they are
# a question. "I found three things called Save -- which?" is the honest answer.
AMBIGUITY_GAP = 0.06
# How many rival names a refusal may hand back. Every one of them is a label
# read off the user's screen, so the list is short on purpose.
MAX_ALTERNATES = 3
MAX_STEPS = 8
# How long to let the screen settle between steps of a sequence.
STEP_SETTLE_S = 0.35


def _spec(
    name: str,
    description: str,
    params: Mapping[str, Any],
    *,
    floor: RiskTier,
    activation_hint: str,
    tags: Sequence[str],
    grantable: bool = True,
) -> ToolSpec:
    """Like `base.spec`, but carries `grantable`.

    `inverses` is spelled `()` on every tool in this package, out loud, because
    "this tool has no inverse" is a stronger statement than "somebody forgot".
    """
    return ToolSpec(
        name=name,
        description=description,
        params=dict(params),
        floor=floor,
        activation_hint=" ".join(activation_hint.split()),
        tags=tuple(tags),
        inverses=(),
        grantable=grantable,
    )


# ---------------------------------------------------------------------------
# Shared resolution
# ---------------------------------------------------------------------------


class _UiTool(BaseTool):
    """Common machinery: bounded snapshot, ranking, refusal, re-verification."""

    def _refuse(
        self,
        reason: str,
        *,
        requested_target: str = "",
        app: str = "",
        alternates: Sequence[str] = (),
        remedy: str = "",
        **extra: Any,
    ) -> ResolvedAction:
        """A resolution that found nothing. Still verb-first, still speakable.

        Refusals are the normal case for this capability, not the error case:
        roughly half the apps on a real Mac expose nothing nameable. "I can't
        see anything I can name in Warp" is a correct product.
        """
        return ResolvedAction(
            tool=self.spec.name,
            args={
                "reason": reason,
                "remedy": remedy,
                "requested_target": requested_target,
                "app": app,
                "alternates": list(alternates)[:MAX_ALTERNATES],
                **extra,
            },
            targets=(),
            explicit=False,
            verb=self.verb,
        )

    def _snapshot(self, app: str, window: str | None = None) -> Snapshot:
        # Module-level indirection on purpose: tests replace `snapshot` with a
        # recorded tree, so every decision in this file is exercised on a
        # machine with zero permissions granted.
        return snapshot(app, window=window)

    def _pick(
        self,
        snap: Snapshot,
        query: str,
        *,
        predicate,
    ) -> tuple[Element | None, str, list[str]]:
        """Rank the nameable candidates. Returns (element, reason, alternates)."""
        pool = [el for el in snap.elements if el.label and predicate(el)]
        if not pool:
            return None, f"I cannot see anything like that in {snap.app}.", []
        ranked = [
            (score, el)
            for score, el in rank_candidates(query, pool, key=searchable, limit=6)
            if score >= MATCH_FLOOR
        ]
        if not ranked:
            return None, f"I could not find {query} in {snap.app}.", []
        best_score, best = ranked[0]
        rivals = [el for score, el in ranked[1:] if best_score - score <= AMBIGUITY_GAP]
        if rivals:
            names = [compose_target(el) or el.label for el in (best, *rivals)]
            names = [n for n in names if n][:MAX_ALTERNATES + 1]
            joined = ", ".join(names[:-1]) + f", and {names[-1]}" if len(names) > 1 else names[0]
            return None, f"I found more than one: {joined}. Which one?", names[1:]
        return best, "", []

    def _verify(self, action: ResolvedAction) -> tuple[Element | None, ToolResult | None]:
        """Re-read the tree and re-find the element the readback named.

        The sentence the user answered described one specific element. If that
        element is no longer there, consent does not transfer to whatever took
        its place -- the tool stops.
        """
        identity = str(action.args.get("identity") or "")
        app = str(action.args.get("app_name") or action.args.get("app") or "")
        label = action.targets[0] if action.targets else "that"
        snap = self._snapshot(app, action.args.get("window") or None)
        if snap.degraded is not None:
            return None, snap.degraded.as_result(f"I could not act on {label}.")
        matches = find_by_identity(snap, identity)
        if not matches:
            return None, self.failed(
                "The screen changed, so I stopped.",
                "the element named in the readback is no longer in the tree",
                identity=identity,
            )
        if len(matches) > 1:
            return None, self.failed(
                "There is now more than one of those, so I stopped.",
                "the element named in the readback is no longer unique",
                identity=identity,
            )
        return matches[0], None


def _effect(before: Element, after: Element | None) -> str:
    """Transport success is not semantic success.

    `confirmed` -- the element changed or went away, so something happened.
    `suspected_noop` -- it is byte-identical, so probably nothing did.
    `unverifiable` -- we could not look. Said plainly rather than guessed.
    """
    if after is None:
        return "confirmed"
    if (after.enabled, after.title, after.focused) != (before.enabled, before.title, before.focused):
        return "confirmed"
    return "suspected_noop"


# ---------------------------------------------------------------------------
# ui_describe -- the only read
# ---------------------------------------------------------------------------


# How long to wait for an activation request to take effect. Short, and a
# module attribute so tests can make it instant: an unbounded or long wait here
# is exactly what hung an earlier attempt at this fix, because a test process
# that is never allowed to become frontmost makes every call wait the maximum.
ACTIVATE_WAIT_S = 0.5
_ACTIVATE_POLL_S = 0.02
_sleep = time.sleep
_now = time.monotonic


def _bring_to_front(pid: object, app: str) -> str | None:
    """Make `pid` frontmost and CONFIRM it, or return why not.

    Keystrokes are delivered to the frontmost app, not to the element that
    was named, so this is what makes the readback's "in <app>" true. Returns
    None when the app is confirmed frontmost, else a spoken reason -- and the
    caller must then send nothing. An unknown pid, a failed activation, or a
    frontmost app we cannot read all refuse: we never type into an app we
    could not confirm is the one receiving the keystrokes.
    """
    try:
        want = int(pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return f"I couldn't tell which process {app} is, so I didn't type anything"
    if frontmost_pid() == want:
        return None
    activate(want)
    deadline = _now() + max(0.0, ACTIVATE_WAIT_S)
    while True:
        if frontmost_pid() == want:
            return None
        if _now() >= deadline:
            return f"I couldn't bring {app} to the front, so I didn't type anything"
        _sleep(_ACTIVATE_POLL_S)


class UiDescribe(_UiTool):
    verb = "read the controls of"
    mutates = False

    spec = _spec(
        "ui_describe",
        "List the named, actionable controls of an application's windows.",
        {
            "app": param("string", "Application name, e.g. 'TextEdit'", required=True),
            "window": param("string", "Restrict to windows whose title contains this"),
            "kind": param("string", "buttons, fields, menus, or all (the default)"),
        },
        # ANNOUNCE, not SILENT, and the difference is the point. list_windows
        # at SILENT returns titles; this returns the CONTROLS of a window --
        # the buttons of your banking page, the rows of your password manager.
        # Reading the screen is a thing the assistant should say it did.
        floor=RiskTier.ANNOUNCE,
        activation_hint="""
            what buttons fields menus or controls does this app have, what can I click in
            safari finder textedit, show me what is on screen in that window, list the
            controls before pressing one, find out whether there is a save button. Reads
            the accessibility tree and names things; changes nothing. Needs Accessibility
            permission, and apps that publish no accessibility tree return nothing.
        """,
        tags=("ui", "accessibility", "read"),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        app = str(kwargs.get("app") or "").strip()
        window = str(kwargs.get("window") or "").strip() or None
        kind = str(kwargs.get("kind") or "all").strip().lower()
        if not app:
            return self._refuse("you did not say which application to look at.")
        target = f"{app}" + (f", the window {window}" if window else "")
        return ResolvedAction(
            tool=self.spec.name,
            args={"app": app, "window": window, "kind": kind},
            targets=(target,),
            explicit=bool(kwargs.get("explicit", True)),
            verb=self.verb,
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        app = str(action.args.get("app") or "")
        if not app:
            return self.failed(
                "I did not know which app to look at.", str(action.args.get("reason") or "no app")
            )
        kind = str(action.args.get("kind") or "all")
        snap = self._snapshot(app, action.args.get("window") or None)
        if snap.degraded is not None:
            return snap.degraded.as_result(f"I could not read the controls of {app}.")

        wanted = _kind_filter(kind)
        rows = [
            {
                "name": el.label,
                "role": el.role,
                "kind": role_noun(el),
                "window": el.window_title,
                "enabled": el.enabled,
                "pressable": el.is_pressable,
            }
            for el in snap.elements
            if wanted(el)
        ]
        if not rows:
            return ToolResult(
                ok=True,
                summary=as_sentence(f"I cannot see any {kind} controls in {snap.app}"),
                data={"app": snap.app, "controls": [], **snap.shape()},
            )
        head = ", ".join(r["name"] for r in rows[:3])
        more = f" and {len(rows) - 3} more" if len(rows) > 3 else ""
        return ToolResult(
            ok=True,
            summary=as_sentence(f"{snap.app} has {head}{more}"),
            data={
                "app": snap.app,
                "bundle_id": snap.bundle_id,
                "controls": rows,
                "truncated": snap.truncated,
                **snap.shape(),
            },
        )


def _kind_filter(kind: str):
    if kind.startswith("button"):
        return lambda el: el.is_pressable and not el.is_text_entry
    if kind.startswith("field"):
        return lambda el: el.is_text_entry
    if kind.startswith("menu"):
        return lambda el: el.role in {"AXMenuItem", "AXMenuBarItem", "AXMenu", "AXMenuButton"}
    return lambda el: el.is_pressable or el.is_text_entry


# ---------------------------------------------------------------------------
# ui_click
# ---------------------------------------------------------------------------


class UiClick(_UiTool):
    verb = "press"
    mutates = True
    irreversible = True

    spec = _spec(
        "ui_click",
        "Press one named control -- a button, checkbox, menu item, tab or link.",
        {
            "target": param("string", "What to press, in words", required=True),
            "app": param("string", "Which application", required=True),
            "window": param("string", "Restrict to windows whose title contains this"),
        },
        floor=RiskTier.CONFIRM_VOICE,
        activation_hint="""
            last resort: press a button checkbox menu item tab or link in an app that has
            no shortcut and no dedicated tool, click save cancel ok send in that window,
            tick that box, choose that menu item. Prefer run_shortcut, open_app or the file
            tools whenever one fits. Cannot be undone -- command Z is another click, not an
            inverse -- and the button it presses is named from the screen, not from me.
        """,
        tags=("ui", "accessibility", "irreversible"),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        requested = str(kwargs.get("target") or "").strip()
        app = str(kwargs.get("app") or "").strip()
        window = str(kwargs.get("window") or "").strip() or None
        explicit = bool(kwargs.get("explicit", True))
        if not requested or not app:
            return self._refuse(
                "you did not say what to press, or in which app.",
                requested_target=requested, app=app,
            )
        snap = self._snapshot(app, window)
        if snap.degraded is not None:
            return self._refuse(
                snap.degraded.remedy, requested_target=requested, app=app,
                remedy=snap.degraded.remedy, degraded=snap.degraded.capability,
            )
        element, reason, alternates = self._pick(
            snap, requested, predicate=lambda el: el.is_pressable and not el.is_text_entry
        )
        if element is None:
            return self._refuse(
                reason, requested_target=requested, app=app, alternates=alternates
            )
        if not element.enabled:
            name = compose_target(element) or element.label
            return self._refuse(
                f"{name} is there, but it is greyed out.",
                requested_target=requested, app=app,
            )
        target = compose_target(element)
        if target is None:  # unreachable while _pick requires a label; kept as the rule
            return self._refuse(
                "I can see something there but I cannot name it, so I will not press it.",
                requested_target=requested, app=app,
            )
        hint, consequences = assess(element, app_query=app, verb=self.verb)
        return ResolvedAction(
            tool=self.spec.name,
            args={
                # The model's word for it, kept beside the truth and never used
                # as the truth. The card shows both; the sentence shows one.
                "requested_target": requested,
                "app": app,
                "app_name": element.app,
                "window": element.window_title or None,
                "identity": element.identity,
                "token": token(snap.snapshot_id, element),
                "role": element.role,
                "kind": role_noun(element),
                "label": element.label,
            },
            targets=(target,),
            explicit=explicit,
            verb=self.verb,
            floor_hint=hint,
            origin=element.bundle_id or None,
            consequences=consequences,
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        if not action.targets:
            return self.failed(
                as_sentence(str(action.args.get("reason") or "I could not find that")),
                str(action.args.get("reason") or "nothing resolved"),
                alternates=action.args.get("alternates") or [],
            )
        label = action.targets[0]
        if self.dry_run:
            return self.dry(f"I would press {label}.", app=action.args.get("app_name"))

        element, failure = self._verify(action)
        if failure is not None:
            return failure
        assert element is not None
        if not element.enabled:
            return self.failed(
                "That is greyed out now, so I stopped.", "the element became disabled"
            )

        ok, detail = press(element)
        path = "axpress"
        if not ok:
            ok, detail = click_center(element)
            path = "synthetic"
        if not ok:
            return Degraded("ui_press", f"I could not press {label}.", detail).as_result(
                f"I could not press {label}."
            )
        effect = _effect(element, reread(element))
        return ToolResult(
            ok=True,
            summary=as_sentence(f"Pressed {label}"),
            data={
                "app": element.app,
                "role": element.role,
                "path": path,
                "effect": effect,
                "verified": effect != "unverifiable",
            },
            # No UndoAction, deliberately: see the module docstring.
        )


# ---------------------------------------------------------------------------
# ui_type
# ---------------------------------------------------------------------------


class UiType(_UiTool):
    verb = "type"
    mutates = True
    irreversible = True

    spec = _spec(
        "ui_type",
        "Type literal text into one named text field.",
        {
            "text": param("string", "Exactly what to type", required=True),
            "target": param("string", "Which field, in words", required=True),
            "app": param("string", "Which application", required=True),
        },
        floor=RiskTier.CONFIRM_VOICE,
        activation_hint="""
            last resort: type words into a named text box search field or form field of an
            app that has no shortcut and no dedicated tool, fill in that field, put this
            text in the search box. Never types into a password field. The exact text is
            read back before anything is typed, and it cannot be undone -- taking it back
            is up to the app, not to me.
        """,
        tags=("ui", "accessibility", "irreversible"),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        text = str(kwargs.get("text") or "")
        requested = str(kwargs.get("target") or "").strip()
        app = str(kwargs.get("app") or "").strip()
        explicit = bool(kwargs.get("explicit", True))
        if not text.strip() or not requested or not app:
            return self._refuse(
                "you did not say what to type, where, or in which app.",
                requested_target=requested, app=app,
            )
        denial = denied_text_reason(text)
        if denial:
            # Checked BEFORE any approval is asked for: the confirmation for
            # "type this into the search box" is not the confirmation the
            # sentence would have described.
            return self._refuse(denial, requested_target=requested, app=app)
        snap = self._snapshot(app)
        if snap.degraded is not None:
            return self._refuse(
                snap.degraded.remedy, requested_target=requested, app=app,
                remedy=snap.degraded.remedy, degraded=snap.degraded.capability,
            )
        element, reason, alternates = self._pick(
            snap, requested, predicate=lambda el: el.is_text_entry
        )
        if element is None:
            return self._refuse(
                reason, requested_target=requested, app=app, alternates=alternates
            )
        if element.is_secure:
            return self._refuse(
                "that is a password field, and I do not type into password fields.",
                requested_target=requested, app=app,
            )
        if not element.enabled:
            return self._refuse(
                f"{element.label} is there, but it is greyed out.",
                requested_target=requested, app=app,
            )
        target = compose_target(element)
        if target is None:
            return self._refuse(
                "I can see a field there but I cannot name it, so I will not type into it.",
                requested_target=requested, app=app,
            )
        hint, consequences = assess(element, app_query=app, verb=self.verb)
        # The body of what is about to be written is part of what a yes means.
        # Same discipline as run_shortcut reading out the text it will send.
        consequences["text"] = f"typing this: {describe_text(text)}"
        if is_terminal(element):
            consequences["terminal"] = (
                f"{element.app} is a terminal, so this text is a command line"
            )
            hint = RiskTier.CONFIRM_VISUAL
        return ResolvedAction(
            tool=self.spec.name,
            args={
                "requested_target": requested,
                "app": app,
                "app_name": element.app,
                "window": element.window_title or None,
                "identity": element.identity,
                "token": token(snap.snapshot_id, element),
                "text": text,
                "role": element.role,
                "kind": role_noun(element),
                "label": element.label,
            },
            targets=(target,),
            explicit=explicit,
            verb=self.verb,
            floor_hint=hint,
            origin=element.bundle_id or None,
            consequences=consequences,
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        if not action.targets:
            return self.failed(
                as_sentence(str(action.args.get("reason") or "I could not find that field")),
                str(action.args.get("reason") or "nothing resolved"),
            )
        label = action.targets[0]
        text = str(action.args.get("text") or "")
        if self.dry_run:
            return self.dry(f"I would type into {label}.", app=action.args.get("app_name"))

        element, failure = self._verify(action)
        if failure is not None:
            return failure
        assert element is not None
        if element.is_secure:
            return self.failed(
                "That turned into a password field, so I stopped.",
                "the element is now a secure text field",
            )
        refusal = _bring_to_front(element.pid, element.app)
        if refusal is not None:
            return self.failed(as_sentence(refusal), "target app not confirmed frontmost")
        ok, detail = focus(element)
        if not ok:
            return Degraded(
                "ui_focus", f"I could not put the cursor in {label}.", detail
            ).as_result(f"I could not type into {label}.")
        ok, detail = type_text(text)
        if not ok:
            return Degraded("ui_type", f"I could not type into {label}.", detail).as_result(
                f"I could not type into {label}."
            )
        return ToolResult(
            ok=True,
            summary=as_sentence(f"Typed that into {label}"),
            data={
                "app": element.app,
                "role": element.role,
                "chars": len(text),
                "effect": _effect(element, reread(element)),
            },
        )


# ---------------------------------------------------------------------------
# ui_key
# ---------------------------------------------------------------------------


class UiKey(_UiTool):
    verb = "press the keys"
    mutates = True
    irreversible = True

    spec = _spec(
        "ui_key",
        "Press one named key combination in an application.",
        {
            "keys": param("string", "The combination, e.g. 'command s'", required=True),
            "app": param("string", "Which application", required=True),
        },
        floor=RiskTier.CONFIRM_VOICE,
        # NOT grantable. Every other tool here names a thing; this one can only
        # name a keystroke, because what a keystroke MEANS is defined by the
        # app. A grant answers a confirmation in advance, and there is no
        # honest sentence here to answer in advance.
        grantable=False,
        activation_hint="""
            last resort: send a keyboard shortcut to an app that has no other way in, press
            command S command W escape return in that window, hit the shortcut for it.
            Prefer run_shortcut or a named button. What a shortcut does is defined by the
            app, so I can say which keys I will press but not what will happen, and it
            cannot be undone.
        """,
        tags=("ui", "accessibility", "irreversible"),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        raw = str(kwargs.get("keys") or "").strip()
        app = str(kwargs.get("app") or "").strip()
        explicit = bool(kwargs.get("explicit", True))
        if not raw or not app:
            return self._refuse("you did not say which keys, or in which app.", app=app)
        combo = parse_combo(raw)
        if combo.denied_reason:
            return self._refuse(
                f"I will not press that -- {combo.denied_reason}.",
                requested_target=raw, app=app,
            )
        if not combo.ok:
            return self._refuse(combo.error, requested_target=raw, app=app)
        snap = self._snapshot(app)
        if snap.degraded is not None:
            return self._refuse(
                snap.degraded.remedy, requested_target=raw, app=app,
                remedy=snap.degraded.remedy, degraded=snap.degraded.capability,
            )
        window = next((el.window_title for el in snap.elements if el.window_title), "")
        target = f"{combo.spoken()} in {snap.app}"
        if window:
            target = f"{combo.spoken()} in {window} in {snap.app}"
        return ResolvedAction(
            tool=self.spec.name,
            args={
                "requested_target": raw,
                "app": app,
                "app_name": snap.app,
                "keys": combo.spoken(),
                "keycode": combo.keycode,
                "modifiers": list(combo.modifiers),
                "pid": snap.pid,
            },
            targets=(target,),
            explicit=explicit,
            verb=self.verb,
            origin=snap.bundle_id or None,
            consequences={
                # The honest weakness of this tool, said out loud every time.
                "meaning": f"what that does is up to {snap.app}",
                "focus": f"this will bring {snap.app} to the front",
                "undo": UNDO_CONSEQUENCE,
            },
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        if not action.targets:
            return self.failed(
                as_sentence(str(action.args.get("reason") or "I could not send that")),
                str(action.args.get("reason") or "nothing resolved"),
            )
        label = action.targets[0]
        if self.dry_run:
            return self.dry(f"I would press {label}.", app=action.args.get("app_name"))
        keycode = int(action.args.get("keycode") or 0)
        modifiers = list(action.args.get("modifiers") or [])
        app_name = str(action.args.get("app_name") or "that app")
        refusal = _bring_to_front(action.args.get("pid"), app_name)
        if refusal is not None:
            return self.failed(as_sentence(refusal), "target app not confirmed frontmost")
        ok, detail = post_key(keycode, modifier_mask(modifiers))
        if not ok:
            return Degraded("ui_key", f"I could not press {label}.", detail).as_result(
                f"I could not press {label}."
            )
        return ToolResult(
            ok=True,
            summary=as_sentence(f"Pressed {label}"),
            # Unverifiable on purpose: a keystroke has no element to re-read,
            # so claiming otherwise would be the lie this package is about.
            data={"app": action.args.get("app_name"), "effect": "unverifiable"},
        )


# ---------------------------------------------------------------------------
# ui_sequence
# ---------------------------------------------------------------------------


class UiSequence(_UiTool):
    verb = "carry out"
    mutates = True
    irreversible = True

    spec = _spec(
        "ui_sequence",
        "Carry out a short, bound chain of UI steps in one application.",
        {
            "app": param("string", "Which application", required=True),
            "steps": param(
                "array",
                "Ordered steps; each is {action: click|type|key, target, text, keys}",
                required=True,
                maxItems=MAX_STEPS,
            ),
        },
        # A plan is a program, and the precedent for "the target IS the
        # program" is run_applescript. Voice alone can never authorise it.
        floor=RiskTier.CONFIRM_VISUAL,
        # NOT grantable. A multi-step plan approved in advance is exactly the
        # blanket permission the whole consent model exists to refuse.
        grantable=False,
        activation_hint="""
            last resort: a short chain of UI steps in one app, press save then type a name
            then press return, fill that field and click the button, do those few things in
            that window in order. Prefer run_shortcut or a single named press. Every step is
            shown on screen first, it stops the moment the screen stops matching, and none
            of it can be undone.
        """,
        tags=("ui", "accessibility", "irreversible", "escape-hatch"),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        app = str(kwargs.get("app") or "").strip()
        raw_steps = kwargs.get("steps") or []
        explicit = bool(kwargs.get("explicit", True))
        if not app or not raw_steps:
            return self._refuse("you did not say which app, or which steps.", app=app)
        steps = normalise_steps(raw_steps)
        if len(steps) > MAX_STEPS:
            return self._refuse(
                f"that is more than {MAX_STEPS} steps, which is more than I will do at once.",
                app=app,
            )
        snap = self._snapshot(app)
        if snap.degraded is not None:
            return self._refuse(
                snap.degraded.remedy, app=app, remedy=snap.degraded.remedy,
                degraded=snap.degraded.capability,
            )

        plan: list[dict[str, Any]] = []
        phrases: list[str] = []
        consequences: dict[str, str] = {}
        hint: RiskTier | None = None
        deferred = 0

        for index, step in enumerate(steps, start=1):
            kind = step.get("action") or ""
            if kind == "click":
                element, reason, alternates = self._pick(
                    snap, step.get("target") or "",
                    predicate=lambda el: el.is_pressable and not el.is_text_entry,
                )
                if element is None:
                    # "Click whatever appears" is precisely the unspeakable
                    # action. A plan whose clicks cannot all be named now does
                    # not resolve at all -- which also means an agent can never
                    # blind-press the Allow button of a dialog it provoked.
                    return self._refuse(
                        f"step {index} wants to press something I cannot see yet: {reason}",
                        app=app, alternates=alternates,
                    )
                if not element.enabled:
                    return self._refuse(
                        f"step {index} wants to press {element.label}, which is greyed out.",
                        app=app,
                    )
                target = compose_target(element)
                if target is None:
                    return self._refuse(
                        f"step {index} wants to press something I cannot name.", app=app
                    )
                step_hint, step_consequences = assess(element, app_query=app, verb="press")
                if step_hint is not None:
                    hint = step_hint if hint is None else max(hint, step_hint)
                for key, value in step_consequences.items():
                    if key in {"wording", "system", "modal"}:
                        consequences[f"step{index}_{key}"] = value
                plan.append(
                    {
                        "action": "click", "bound": True, "identity": element.identity,
                        "token": token(snap.snapshot_id, element), "target": target,
                        "window": element.window_title or None,
                    }
                )
                phrases.append(f"press {target}")
            elif kind == "type":
                text = step.get("text") or ""
                if not text.strip():
                    return self._refuse(f"step {index} has nothing to type.", app=app)
                denial = denied_text_reason(text)
                if denial:
                    return self._refuse(f"step {index}: {denial}.", app=app)
                element, _reason, _alts = self._pick(
                    snap, step.get("target") or "", predicate=lambda el: el.is_text_entry
                )
                if element is not None and element.is_secure:
                    return self._refuse(
                        f"step {index} targets a password field.", app=app
                    )
                if element is None:
                    deferred += 1
                    plan.append({"action": "type", "bound": False, "text": text})
                    phrases.append(f"type {describe_text(text, 60)!r} wherever the cursor is")
                else:
                    plan.append(
                        {
                            "action": "type", "bound": True, "identity": element.identity,
                            "token": token(snap.snapshot_id, element), "text": text,
                            "target": compose_target(element),
                        }
                    )
                    phrases.append(
                        f"type {describe_text(text, 60)!r} into {compose_target(element)}"
                    )
            elif kind == "key":
                combo = parse_combo(step.get("keys") or "")
                if combo.denied_reason:
                    return self._refuse(
                        f"step {index}: I will not press that -- {combo.denied_reason}.", app=app
                    )
                if not combo.ok:
                    return self._refuse(f"step {index}: {combo.error}.", app=app)
                plan.append(
                    {
                        "action": "key", "bound": False, "keys": combo.spoken(),
                        "keycode": combo.keycode, "modifiers": list(combo.modifiers),
                    }
                )
                phrases.append(f"press {combo.spoken()}")
            else:
                return self._refuse(
                    f"step {index} is not a click, some text or a key combination.", app=app
                )

        count = _count_word(len(plan))
        target = f"these {count} steps in {snap.app}: " + ", then ".join(phrases)
        if deferred:
            consequences["unseen"] = (
                f"{_count_word(deferred)} of those happen wherever the cursor ends up, "
                "in a window I cannot see yet"
            )
        consequences["stop"] = "I stop as soon as the screen stops matching what I just said"
        consequences["undo"] = UNDO_CONSEQUENCE
        return ResolvedAction(
            tool=self.spec.name,
            args={"app": app, "app_name": snap.app, "pid": snap.pid, "steps": plan,
                  "requested_steps": steps},
            # One target, not one per step: `_targets_phrase` collapses a list
            # of four or more into "and N more", and a plan the user only half
            # hears is a plan they did not agree to.
            targets=(target,),
            explicit=explicit,
            verb=self.verb,
            floor_hint=hint,
            origin=snap.bundle_id or None,
            consequences=consequences,
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        if not action.targets:
            return self.failed(
                as_sentence(str(action.args.get("reason") or "I could not work that out")),
                str(action.args.get("reason") or "nothing resolved"),
            )
        app = str(action.args.get("app_name") or action.args.get("app") or "")
        plan = list(action.args.get("steps") or [])
        if self.dry_run:
            return self.dry(f"I would carry out {len(plan)} steps in {app}.", app=app)

        done = 0
        for index, step in enumerate(plan, start=1):
            kind = str(step.get("action") or "")
            if kind in ("type", "key"):
                # Re-confirmed before EVERY keystroke step, not once up front:
                # an earlier click may have opened another app or sheet.
                refusal = _bring_to_front(action.args.get("pid"), app)
                if refusal is not None:
                    return self._stopped(done, index, refusal, app)
            if kind == "click" or (kind == "type" and step.get("bound")):
                snap = self._snapshot(app, step.get("window") or None)
                if snap.degraded is not None:
                    return self._stopped(done, index, snap.degraded.remedy, app)
                matches = find_by_identity(snap, str(step.get("identity") or ""))
                if len(matches) != 1:
                    return self._stopped(
                        done, index, "the screen changed, so I stopped", app
                    )
                element = matches[0]
                if not element.enabled:
                    return self._stopped(done, index, "that step was greyed out", app)
                if kind == "click":
                    ok, detail = press(element)
                    if not ok:
                        ok, detail = click_center(element)
                else:
                    if element.is_secure:
                        return self._stopped(
                            done, index, "that field is now a password field", app
                        )
                    ok, detail = focus(element)
                    if ok:
                        ok, detail = type_text(str(step.get("text") or ""))
                if not ok:
                    return self._stopped(done, index, detail or "that step did not work", app)
            elif kind == "type":
                # A deferred typing step goes wherever the cursor is, so what
                # has the cursor is checked before a single character is sent.
                current = focused_element()
                # "I cannot tell what has the cursor" is NOT "it is not a
                # password field". Without an Accessibility grant this read
                # returns nothing every single time, and the old guard --
                # `current is not None and current.is_secure` -- read that as
                # safe and typed. Unknown is never safe.
                if current is None:
                    return self._stopped(
                        done, index, "I couldn't tell what has the cursor, so I stopped", app
                    )
                if current.is_secure:
                    return self._stopped(
                        done, index, "a password field had the cursor, so I stopped", app
                    )
                ok, detail = type_text(str(step.get("text") or ""))
                if not ok:
                    return self._stopped(done, index, detail or "that step did not work", app)
            elif kind == "key":
                ok, detail = post_key(
                    int(step.get("keycode") or 0), modifier_mask(step.get("modifiers") or [])
                )
                if not ok:
                    return self._stopped(done, index, detail or "that step did not work", app)
            else:
                return self._stopped(done, index, "I did not understand that step", app)
            done += 1
            time.sleep(STEP_SETTLE_S)

        return ToolResult(
            ok=True,
            summary=as_sentence(f"Did all {_count_word(done)} steps in {app}"),
            data={"app": app, "steps_done": done, "steps_total": len(plan)},
        )

    def _stopped(self, done: int, step: int, why: str, app: str) -> ToolResult:
        return ToolResult(
            ok=False,
            summary=as_sentence(
                f"I did {_count_word(done)} of the steps and then stopped: {why}"
                if done
                else f"I stopped before the first step: {why}"
            ),
            data={"app": app, "steps_done": done, "stopped_at": step},
            error=why,
        )


_NUMBERS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight")


def _count_word(n: int) -> str:
    return _NUMBERS[n] if 0 <= n < len(_NUMBERS) else str(n)


COMPUTER_TOOL_CLASSES: tuple[type[BaseTool], ...] = (
    UiDescribe,
    UiClick,
    UiType,
    UiKey,
    UiSequence,
)

__all__ = [
    "AMBIGUITY_GAP",
    "COMPUTER_TOOL_CLASSES",
    "MATCH_FLOOR",
    "MAX_STEPS",
    "UiClick",
    "UiDescribe",
    "UiKey",
    "UiSequence",
    "UiType",
]
