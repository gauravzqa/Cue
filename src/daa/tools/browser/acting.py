"""Stage 4: acting on a page, with the readback derived from the page itself.

Three tools, and the shape of the three is the safety property:

- `click_element` at `CONFIRM_VOICE` presses a named control, and is
  **structurally incapable** of pressing one that submits a form. It checks
  that in `resolve()` and checks it AGAIN in `run()` against the re-read DOM,
  so the refusal does not depend on the resolver having been right a second
  earlier.
- `submit_form` at `CONFIRM_VISUAL`, `irreversible=True`, `inverses=()` is the
  only way a form is ever submitted. Its floor is written in reviewed source
  and cannot degrade. `floor_hint` is used for the residue -- a cross-origin
  link, an unnamed control, a checkout path -- and never as a replacement for
  this split, because a hint is set by code that has to run and be right, and a
  resolver that fails to recognise a payment form would degrade silently to
  `CONFIRM_VOICE`.
- `fill_field` at `CONFIRM_VOICE` types into a named field and **refuses
  outright** on a password, a card number or a one-time code. Not "confirm
  harder" -- refuse. A voice assistant should never be the thing that types a
  card number, and there is no tier at which that becomes acceptable.

Every one of them re-reads the element before acting and aborts if the
`dom_fingerprint` taken at `resolve()` no longer matches. Between the readback
and the yes there is one to three seconds of speech, and an SPA can replace the
subtree in that time. That is a way for the confirmation to lie even when
`resolve()` was completely honest, and no other daa tool has it.
"""

from __future__ import annotations

from typing import Any

from daa.contracts import ResolvedAction, RiskTier, ToolResult, UndoAction
from daa.tools.base import as_sentence, param
from daa.tools.browser import facts as factlib
from daa.tools.browser import urls
from daa.tools.browser.base import (
    STALE_SUMMARY,
    BrowserTool,
    Located,
    browser_spec,
    expired,
    expiry,
)
from daa.tools.browser.facts import PageFacts
from daa.tools.browser.privacy import credential_reason

NO_BROWSER = "There's no page open."

# The spoken reason `click_element` hands a form submission to `submit_form`.
ROUTE_TO_SUBMIT = (
    "that button submits a form, so I will show you what it sends before it goes"
)


def _dialog_block(page_facts: PageFacts) -> str:
    if page_facts.pending_dialog:
        return "the page is waiting on a dialog box, which I will not answer for you"
    return ""


class ActingTool(BrowserTool):
    """Shared resolve-side plumbing for the three tools that touch a page."""

    mutates = True

    def _locate_or_refuse(
        self, kwargs: dict[str, Any], *, verb: str
    ) -> tuple[Located | None, ResolvedAction | None, Any]:
        """(located, refusal, page). Exactly one of located/refusal is set."""
        tab_id = str(kwargs.get("tab_id") or "").strip() or None
        selector = str(kwargs.get("selector") or "").strip()
        text = str(kwargs.get("text") or kwargs.get("target") or kwargs.get("name") or "").strip()
        label = text or "that control"
        degraded = self.ensure()
        if degraded is not None:
            return None, self.refusal(
                targets=[label], verb=verb, reason=degraded.remedy,
                selector=selector, tab_id=tab_id,
            ), None
        page = self.page_for(tab_id)
        if page is None:
            return None, self.refusal(
                targets=[label], verb=verb, reason="there is no page open",
                selector=selector, tab_id=tab_id,
            ), None
        located = self.locate(page, selector=selector, text=text)
        if not located.ok:
            return None, self.refusal(
                targets=[label], verb=verb, reason=located.error,
                consequences=(
                    {"choices": "I can list what I did find"} if located.candidates else None
                ),
                selector=located.selector, tab_id=tab_id,
                candidates=list(located.candidates),
            ), page
        return located, None, page


# ---------------------------------------------------------------------------
# click_element
# ---------------------------------------------------------------------------


class ClickElement(ActingTool):
    verb = "press"
    # There is no inverse of a click. A page's own "undo" is another click
    # whose meaning the site defines -- it can re-submit, it can act on
    # something the user did by hand, and on half the web it does not exist.
    # Declaring `irreversible` is the honest version of that, and it is what
    # pays for skipping an UndoAction under this codebase's rules.
    irreversible = True

    spec = browser_spec(
        "click_element",
        "Press a named button, link or control on the page already open in the browser.",
        {
            "text": param("string", "The label on the control, as it appears on screen"),
            "selector": param("string", "A CSS selector, when the label is not unique"),
            "tab_id": param("string", "Which open tab; the active one by default"),
        },
        floor=RiskTier.CONFIRM_VOICE,
        activation_hint="""
            click, press, tap, hit that button, follow that link, open the menu, expand
            that section, tick that box, choose that option, select that tab. Presses one
            named control on a page already open in the assistant's browser. Refuses any
            control that submits a form and hands it to the form tool instead. A click
            cannot be undone -- there is no inverse of pressing a button, so this is
            irreversible and always confirmed first.
        """,
        tags=("browser", "act", "irreversible"),
        inverses=(),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        located, refusal, _page = self._locate_or_refuse(kwargs, verb=self.verb)
        if refusal is not None:
            return refusal
        assert located is not None and located.facts is not None
        facts = located.facts
        tab_id = str(kwargs.get("tab_id") or "").strip() or None
        fingerprint = factlib.dom_fingerprint(facts)
        consequences = factlib.consequences_for(facts)
        blocked = _dialog_block(facts)

        # The categorical route. Checked here AND in run(), against separately
        # re-read facts, because this is the one that costs money when it is
        # wrong and a resolver is code like any other.
        if factlib.must_use_submit(facts):
            return self.refusal(
                targets=[factlib.target_phrase(facts)],
                verb=self.verb,
                reason=ROUTE_TO_SUBMIT,
                consequences=consequences,
                floor_hint=factlib.floor_hint_for(facts),
                origin=facts.origin,
                selector=located.selector, tab_id=tab_id,
                dom_fingerprint=fingerprint, route_to="submit_form",
            )
        if blocked:
            return self.refusal(
                targets=[factlib.target_phrase(facts)],
                verb=self.verb, reason=blocked, consequences=consequences,
                origin=facts.origin, selector=located.selector, tab_id=tab_id,
                dom_fingerprint=fingerprint,
            )
        return self.browser_action(
            targets=[factlib.target_phrase(facts)],
            verb=factlib.verb_for(facts),
            explicit=bool(kwargs.get("text") or kwargs.get("selector")),
            consequences=consequences,
            floor_hint=factlib.floor_hint_for(facts),
            origin=facts.origin,
            selector=located.selector,
            tab_id=tab_id,
            dom_fingerprint=fingerprint,
            control=facts.accessible_name,
            role=facts.role,
            site=facts.site,
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        refused = action.args.get("refused")
        if refused:
            return ToolResult(
                ok=False,
                summary=as_sentence(f"I won't press that — {refused}"),
                data={"route_to": action.args.get("route_to") or "",
                      "candidates": list(action.args.get("candidates") or [])},
                error=str(refused),
            )
        if self.dry_run:
            return self.dry("I would press that.")
        degraded = self.ensure()
        if degraded is not None:
            return self.degraded_result(degraded)
        page = self.page_for(action.args.get("tab_id"))
        if page is None:
            return self.failed(NO_BROWSER, "no page")
        facts, error = self.recheck(page, action)
        if facts is None:
            return self.failed(error, "the element changed between resolve and run")
        # The second, independent check. If resolve() somehow produced a
        # clickable action for a submitting control -- a bug, a race, a hand-
        # built ResolvedAction -- this is where it stops.
        if factlib.must_use_submit(facts):
            return self.failed(
                as_sentence(f"I won't press that — {ROUTE_TO_SUBMIT}"),
                "submitting control reached click_element",
                route_to="submit_form",
            )
        if facts.pending_dialog:
            return self.failed(
                "The page is waiting on a dialog box.", "dialog pending", dialog=True
            )
        try:
            page.click(str(action.args.get("selector") or ""))
        except Exception as exc:  # noqa: BLE001
            return self.failed("I couldn't press that.", f"click failed ({type(exc).__name__})")
        name = facts.accessible_name or "that control"
        return ToolResult(
            ok=True,
            summary=as_sentence(f"Pressed {name}"[:120]),
            data={"control": facts.accessible_name, "site": facts.site,
                  "url": urls.log_url(page.url())},
        )


# ---------------------------------------------------------------------------
# fill_field
# ---------------------------------------------------------------------------


class FillField(ActingTool):
    verb = "type into"

    spec = browser_spec(
        "fill_field",
        "Type text into a named field on the page already open in the browser.",
        {
            "text": param("string", "The label of the field, as it appears on screen"),
            "value": param("string", "What to type into it"),
            "selector": param("string", "A CSS selector, when the label is not unique"),
            "tab_id": param("string", "Which open tab; the active one by default"),
            "restore": param("string", "Internal: 'empty' clears a field this tool filled"),
        },
        floor=RiskTier.CONFIRM_VOICE,
        activation_hint="""
            type, enter, put, fill in, write in that box, set the search field to, put my
            address in, enter the quantity. Types into one named field on a page already
            open in the assistant's browser. Never types into a password, a card number,
            a security code or a one-time code field -- those are refused outright rather
            than confirmed. Clearing a field it filled is the only thing it can take
            back; replacing text that was already there cannot be undone.
        """,
        tags=("browser", "act"),
        # Its own inverse, and only in one direction: clearing a field this
        # tool filled. See run() for why the other direction cannot exist.
        inverses=("fill_field",),
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        restore = str(kwargs.get("restore") or "").strip().lower()
        value = "" if restore == "empty" else str(kwargs.get("value") or "")
        located, refusal, _page = self._locate_or_refuse(kwargs, verb=self.verb)
        if refusal is not None:
            return refusal
        assert located is not None and located.facts is not None
        facts = located.facts
        tab_id = str(kwargs.get("tab_id") or "").strip() or None
        fingerprint = factlib.dom_fingerprint(facts)

        reason = credential_reason(facts.input_type, facts.autocomplete)
        if reason:
            return self.refusal(
                targets=[factlib.target_phrase(facts)],
                verb=self.verb, reason=reason,
                floor_hint=RiskTier.CONFIRM_VISUAL,
                origin=facts.origin, selector=located.selector, tab_id=tab_id,
                dom_fingerprint=fingerprint,
            )
        blocked = _dialog_block(facts)
        if blocked:
            return self.refusal(
                targets=[factlib.target_phrase(facts)],
                verb=self.verb, reason=blocked,
                origin=facts.origin, selector=located.selector, tab_id=tab_id,
                dom_fingerprint=fingerprint,
            )

        consequences = factlib.consequences_for(facts)
        was_empty = facts.value_empty is not False
        if not was_empty:
            # The obvious undo -- "type the old value back" -- would journal
            # whatever was in the box to `~/.daa/undo.jsonl`, and the old value
            # of a field can be a half-typed password, a one-time code, a card
            # number or the draft of a private message. So there is no undo on
            # this path, and the user hears that before consenting.
            consequences["overwrite"] = (
                "replacing what is already in it, which I will not be able to put back"
            )
        if restore == "empty":
            consequences["clear"] = "clearing it"
        return self.browser_action(
            targets=[factlib.target_phrase(facts)],
            verb=self.verb,
            explicit=bool(kwargs.get("text") or kwargs.get("selector")),
            consequences=consequences,
            floor_hint=factlib.floor_hint_for(facts),
            origin=facts.origin,
            selector=located.selector,
            tab_id=tab_id,
            dom_fingerprint=fingerprint,
            # The value lives ONLY here. It is never a target, never a
            # consequence, never a summary and never an undo argument, and
            # `args` is not a field any audit event builder reads.
            value=value,
            was_empty=was_empty,
            restore=restore,
            expires_at=kwargs.get("expires_at"),
            control=facts.accessible_name,
            site=facts.site,
        )

    def run(self, action: ResolvedAction) -> ToolResult:
        refused = action.args.get("refused")
        if refused:
            return ToolResult(
                ok=False,
                summary=as_sentence(f"I won't type into that — {refused}"),
                data={}, error=str(refused),
            )
        if expired(action.args):
            return self.failed("That was too long ago for me to take back.", "undo entry expired")
        if self.dry_run:
            return self.dry("I would type into that field.")
        degraded = self.ensure()
        if degraded is not None:
            return self.degraded_result(degraded)
        page = self.page_for(action.args.get("tab_id"))
        if page is None:
            return self.failed(NO_BROWSER, "no page")
        facts, error = self.recheck(page, action)
        if facts is None:
            return self.failed(error, "the element changed between resolve and run")
        # Re-checked against the re-read DOM, not trusted from resolve(): a
        # page that swaps a search box for a password box between the question
        # and the answer must not be typed into.
        reason = credential_reason(facts.input_type, facts.autocomplete)
        if reason:
            return self.failed(as_sentence(f"I won't type into that — {reason}"), "credential field")
        if facts.pending_dialog:
            return self.failed("The page is waiting on a dialog box.", "dialog pending")
        value = str(action.args.get("value") or "")
        try:
            page.fill(str(action.args.get("selector") or ""), value)
        except Exception as exc:  # noqa: BLE001
            return self.failed("I couldn't type into that.", f"fill failed ({type(exc).__name__})")

        name = facts.accessible_name or "that field"
        undo = None
        if action.args.get("was_empty") and str(action.args.get("restore") or "") != "empty":
            undo = UndoAction(
                description=f"clear the {name} field again"[:120],
                tool="fill_field",
                # `{"restore": "empty"}` and a selector. NEVER a value: the
                # journal is a file on disk that is backed up, and the value is
                # the one thing this tool must not persist.
                args={
                    "selector": action.args.get("selector"),
                    "tab_id": action.args.get("tab_id"),
                    "restore": "empty",
                    "expires_at": expiry(self.options),
                },
            )
        did = "Cleared" if str(action.args.get("restore") or "") == "empty" else "Typed into"
        return ToolResult(
            ok=True,
            summary=as_sentence(f"{did} {name}"[:120]),
            # The character count, never the characters.
            data={"control": facts.accessible_name, "site": facts.site, "chars": len(value)},
            undo=undo,
        )


# ---------------------------------------------------------------------------
# submit_form
# ---------------------------------------------------------------------------


class SubmitForm(ActingTool):
    """The only way a form is ever submitted, and it is never cheap.

    There is no `UndoAction` for a POST. There is sometimes a *compensating*
    action ("cancel the order") but it is site-specific, time-limited and
    frequently absent, and offering one that may not work is worse than
    offering none -- the user consents to something reversible and it is not.

    So this takes `run_applescript`'s deal exactly: `irreversible = True`, the
    `irreversible` tag, the fact stated in the `activation_hint`,
    `inverses = ()`, and a `CONFIRM_VISUAL` floor that no resolver can lower.
    """

    verb = "submit"
    irreversible = True

    spec = browser_spec(
        "submit_form",
        "Submit a form on the page already open in the browser, sending its fields.",
        {
            "text": param("string", "The label on the submit button, as it appears on screen"),
            "selector": param("string", "A CSS selector for the submit control"),
            "tab_id": param("string", "Which open tab; the active one by default"),
        },
        floor=RiskTier.CONFIRM_VISUAL,
        activation_hint="""
            submit, send that form, place the order, pay, check out, post it, confirm the
            booking, sign up, log the entry. Submits a form on a page already open in the
            assistant's browser, which sends its fields to a server. There is no undo for
            a submitted form -- it is irreversible, it can spend money, and it is shown on
            screen with the destination, the field names and the amount before it can be
            approved. Voice alone never authorises it.
        """,
        tags=("browser", "act", "irreversible", "destructive"),
        # A POST has no inverse. Saying so out loud is the honest state of the
        # world, and it means no journal row can ever name a tool to "undo" a
        # purchase.
        inverses=(),
        # Never pre-authorised by a scoped grant, whatever its scope: spending
        # money and sending data are decisions made awake.
        grantable=False,
    )

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        located, refusal, _page = self._locate_or_refuse(kwargs, verb=self.verb)
        if refusal is not None:
            return refusal
        assert located is not None and located.facts is not None
        facts = located.facts
        tab_id = str(kwargs.get("tab_id") or "").strip() or None
        fingerprint = factlib.dom_fingerprint(facts)

        if not facts.submits or facts.form is None:
            return self.refusal(
                targets=[factlib.target_phrase(facts)],
                verb=self.verb,
                reason="that control does not submit a form, so pressing it is the ordinary tool",
                origin=facts.origin, selector=located.selector, tab_id=tab_id,
                dom_fingerprint=fingerprint, route_to="click_element",
            )
        blocked = _dialog_block(facts)
        if blocked:
            return self.refusal(
                targets=[self._form_phrase(facts)], verb=self.verb, reason=blocked,
                origin=facts.origin, selector=located.selector, tab_id=tab_id,
                dom_fingerprint=fingerprint,
            )

        form = facts.form
        consequences = factlib.consequences_for(facts)
        if facts.named:
            consequences["button"] = f"by pressing '{facts.accessible_name}'"
        if not form.posts:
            where = urls.speakable_host(form.action_host) or form.action_etld1 or facts.etld1
            consequences.setdefault(
                "sends", f"sending {form.field_count} fields to {where}"
            )
        consequences["irreversible"] = "I will not be able to undo this"
        return self.browser_action(
            targets=[self._form_phrase(facts)],
            verb=self.verb,
            explicit=bool(kwargs.get("text") or kwargs.get("selector")),
            consequences=consequences,
            # Belt and braces. The floor is already CONFIRM_VISUAL; the hint
            # says the same thing from the evidence side, so a future change to
            # the floor cannot quietly make this cheap.
            floor_hint=RiskTier.CONFIRM_VISUAL,
            origin=facts.origin,
            selector=located.selector,
            tab_id=tab_id,
            dom_fingerprint=fingerprint,
            # Everything the on-screen card prints, and nothing else. Field
            # NAMES and TYPES, never values; origins, never the full URLs.
            card=self.card(facts),
        )

    @staticmethod
    def _form_phrase(facts: PageFacts) -> str:
        form = facts.form
        kind = "form"
        if form is not None:
            if form.has_payment:
                kind = "payment form"
            elif form.has_password:
                kind = "sign-in form"
            elif form.has_otp:
                kind = "one-time code form"
        where = f" on {facts.site}" if facts.site else ""
        return f"the {kind}{where}"

    @staticmethod
    def card(facts: PageFacts) -> dict[str, Any]:
        """The CONFIRM_VISUAL card's contents, from PageFacts and nothing else."""
        form = facts.form
        return {
            "page_origin": facts.origin,
            "page_path": facts.path,
            "action_origin": (form.action_url if form else ""),
            "control": facts.accessible_name,
            "control_named": facts.named,
            "method": (form.method if form else ""),
            "field_names": list(form.field_names) if form else [],
            "field_types": list(form.field_types) if form else [],
            "field_count": (form.field_count if form else 0),
            "amount": facts.nearby_amount,
            "signed_in": facts.logged_in,
            "own_browser": not facts.attached_to_real_chrome,
            "cross_origin_post": facts.posts_cross_origin,
        }

    def run(self, action: ResolvedAction) -> ToolResult:
        refused = action.args.get("refused")
        if refused:
            return ToolResult(
                ok=False,
                summary=as_sentence(f"I won't submit that — {refused}"),
                data={"route_to": action.args.get("route_to") or ""},
                error=str(refused),
            )
        if self.dry_run:
            return self.dry("I would submit that form.")
        degraded = self.ensure()
        if degraded is not None:
            return self.degraded_result(degraded)
        page = self.page_for(action.args.get("tab_id"))
        if page is None:
            return self.failed(NO_BROWSER, "no page")
        facts, error = self.recheck(page, action)
        if facts is None:
            # Re-checked even here, where the confirmation was a typed yes on a
            # card: the card described a form, and a form that has been
            # replaced is a different form.
            return self.failed(error, "the form changed between resolve and run")
        if not facts.submits or facts.form is None:
            return self.failed(
                "That isn't a form submission any more.", "control no longer submits"
            )
        if facts.pending_dialog:
            return self.failed("The page is waiting on a dialog box.", "dialog pending")
        try:
            page.submit(str(action.args.get("selector") or ""))
        except Exception as exc:  # noqa: BLE001
            return self.failed("I couldn't submit that.", f"submit failed ({type(exc).__name__})")
        return ToolResult(
            ok=True,
            summary=as_sentence(f"Submitted the form on {facts.site}" if facts.site
                                else "Submitted the form"),
            data={"site": facts.site, "url": urls.log_url(page.url()),
                  "fields": facts.form.field_count},
            # No undo, and no journal row beyond the execution event. That is
            # the honest state of the world for a POST.
            undo=None,
        )


ACTING_TOOL_CLASSES = (ClickElement, FillField, SubmitForm)

__all__ = [
    "ACTING_TOOL_CLASSES",
    "ROUTE_TO_SUBMIT",
    "STALE_SUMMARY",
    "ActingTool",
    "ClickElement",
    "FillField",
    "SubmitForm",
]
