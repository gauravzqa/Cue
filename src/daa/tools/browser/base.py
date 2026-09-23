"""Shared plumbing for the browser tools.

Three things live here rather than in each tool, because a tool author who has
to remember them will eventually forget one:

1. **One node, or no action.** `resolve()` must resolve to precisely one
   element. Zero matches is nothing to confirm; more than one is ambiguous, and
   the honest answer to an ambiguous name is already documented in this
   codebase as "I found three -- which?", not a coin flip on document order.
2. **The fingerprint is taken in `resolve()` and checked in `run()`.** Every
   acting tool goes through `recheck()`, which re-reads the live DOM and aborts
   on any difference. No tool gets to skip it, and no tool gets to "retry".
3. **The readback is built by `facts.py` from the live DOM**, never by the
   tool from its own arguments. `browser_action()` takes facts, not strings.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from daa.config import Settings
from daa.contracts import ResolvedAction, RiskTier, ToolResult, ToolSpec
from daa.tools.base import BaseTool, Degraded, normalize_spoken, rank_candidates
from daa.tools.browser import facts as factlib
from daa.tools.browser import urls
from daa.tools.browser.facts import PageFacts
from daa.tools.browser.page import PageHandle, ReadOnlyPage
from daa.tools.browser.session import BrowserOptions, PlaywrightSession, get_session

# The page moved under us between the readback and the yes. There is exactly
# one honest response and it is not "try again".
STALE_SUMMARY = "The page changed while I was asking you."

# How close a spoken label has to be to a control's accessible name before it
# counts as "that one". Deliberately far above `rank_candidates`'s own noise
# floor: the cost of a wrong answer here is a wrong button, not a wrong row in
# a list the user is looking at.
NAME_MATCH_FLOOR = 0.6


@dataclass(frozen=True, slots=True)
class Located:
    """The outcome of pointing at one element. Exactly one of facts/error."""

    facts: PageFacts | None = None
    selector: str = ""
    candidates: tuple[str, ...] = ()     # accessible names of the runners-up
    error: str = ""                      # spoken

    @property
    def ok(self) -> bool:
        return self.facts is not None


class BrowserTool(BaseTool):
    """A tool that needs the browser. Constructing one starts nothing."""

    # Browser tools are never pre-authorised by a scoped grant by default; the
    # ones that may be say so individually. Set on the ToolSpec, not here.
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        session: Any = None,
        options: BrowserOptions | None = None,
    ) -> None:
        super().__init__(settings)
        self._session = session
        self._options = options

    # -- the browser -----------------------------------------------------

    @property
    def options(self) -> BrowserOptions:
        """The session's options, not a fresh default.

        This used to read `self._session`, which is None until `backend` binds
        it -- so a tool built without an explicit session answered from
        `BrowserOptions()` while the browser it would actually drive ran on
        whatever the process-wide session was configured with. Two sources of
        truth for `allow_private_hosts` and `summarize_private_pages`, which
        are the two settings here that decide what daa will refuse.

        Nothing misbehaves today, because `build_loop` passes no options and
        both sides land on the defaults. That is exactly what made it worth
        closing: the same shape as the `dry_run` registry trap, where an
        explicit setting was silently answered by a tool that had decided for
        itself. Reading `backend` constructs the session object; it does not
        start a browser.
        """
        if self._options is None:
            self._options = getattr(self.backend, "options", None) or BrowserOptions()
        return self._options

    @property
    def backend(self) -> Any:
        if self._session is None:
            self._session = get_session(self._options)
        return self._session

    def ensure(self) -> Degraded | None:
        ensure = getattr(self.backend, "ensure", None)
        return ensure() if callable(ensure) else None

    def degraded_result(self, degraded: Degraded) -> ToolResult:
        return degraded.as_result("I can't use the browser right now.")

    def page_for(self, tab_id: str | None = None) -> Any:
        return self.backend.page(tab_id)

    # -- actions ---------------------------------------------------------

    def browser_action(
        self,
        *,
        targets: Sequence[str] = (),
        verb: str = "",
        explicit: bool = True,
        consequences: Mapping[str, str] | None = None,
        floor_hint: RiskTier | None = None,
        origin: str | None = None,
        **args: Any,
    ) -> ResolvedAction:
        """`BaseTool.action()` plus the two fields browser actions need.

        `floor_hint` is how a per-invocation fact ("this click is a payment")
        raises the tier without convincing a judgment model, and `origin` is
        what grant-scope matching needs so that `safety/` never has to parse a
        URL. Both are on `ResolvedAction` and neither is reachable through
        `BaseTool.action()`, which predates them.
        """
        return ResolvedAction(
            tool=self.spec.name,
            args=args,
            targets=tuple(targets),
            explicit=explicit,
            verb=verb or self.verb,
            consequences=dict(consequences or {}),
            floor_hint=floor_hint,
            origin=origin or None,
        )

    def refusal(
        self,
        *,
        targets: Sequence[str],
        verb: str,
        reason: str,
        consequences: Mapping[str, str] | None = None,
        floor_hint: RiskTier | None = None,
        origin: str | None = None,
        **args: Any,
    ) -> ResolvedAction:
        """A resolved action whose run() will do nothing, and says so first.

        The refusal is in the READBACK, not only in the result: a user who says
        yes to "press Pay" and is then told no has been asked a question that
        did not describe what would happen.
        """
        merged = dict(consequences or {})
        merged["refused"] = reason
        return self.browser_action(
            targets=targets, verb=verb, consequences=merged,
            floor_hint=floor_hint, origin=origin, refused=reason, **args,
        )

    # -- locating --------------------------------------------------------

    def locate(
        self,
        page: ReadOnlyPage,
        *,
        selector: str = "",
        text: str = "",
        logged_in: bool | None = None,
    ) -> Located:
        """Point at exactly one element, by name if possible and by CSS if not.

        Name first, deliberately: a named element is the unit of action, and a
        selector is a handle the model wrote. When the caller gives a name, the
        name is matched against what the browser computed, and the CSS path is
        only how the chosen node is reached again.
        """
        chosen = str(selector or "").strip()
        candidates: tuple[str, ...] = ()
        if not chosen:
            wanted = str(text or "").strip()
            if not wanted:
                return Located(error="you did not say which control you meant")
            controls = [c for c in page.controls() if c.get("selector")]
            exact = [
                c for c in controls
                if normalize_spoken(str(c.get("name", ""))) == normalize_spoken(wanted)
            ]
            if len(exact) > 1:
                return Located(
                    candidates=tuple(str(c.get("name", "")) for c in exact[:4]),
                    error=f"there is more than one control called {wanted} on that page",
                )
            if exact:
                pool = exact
            else:
                # `rank_candidates`'s 0.34 noise floor is tuned for picking an
                # app out of a list the user can see. Pressing the wrong
                # control is not that, so a near-miss here is "I could not find
                # it", never "this was the closest thing on the page".
                pool = [
                    c for score, c in rank_candidates(
                        wanted, controls, key=lambda c: str(c.get("name", "")), limit=5
                    )
                    if score >= NAME_MATCH_FLOOR
                ]
            if not pool:
                return Located(error=f"I could not find anything called {wanted} on that page")
            if len(pool) > 1:
                return Located(
                    candidates=tuple(str(c.get("name", "")) for c in pool[:4]),
                    error=f"I found {len(pool)} things that could be {wanted}",
                )
            chosen = str(pool[0].get("selector") or "")
            candidates = (str(pool[0].get("name", "")),)

        raw = page.element_facts(chosen, limit=4)
        if not raw:
            return Located(selector=chosen, error="that control is not on the page any more")
        if len(raw) > 1:
            names = tuple(
                " ".join(str(r.get("name") or "").split()) or "an unlabelled control"
                for r in raw[:4]
            )
            return Located(
                selector=chosen,
                candidates=names,
                error=f"that matches {len(raw)} things on the page",
            )
        page_url = page.url()
        signed_in = page.has_session() if logged_in is None else bool(logged_in)
        found = factlib.facts_from_raw(
            raw[0],
            page_url=page_url,
            page_title=page.title(),
            logged_in=signed_in,
            pending_dialog=page.pending_dialog(),
        )
        return Located(facts=found, selector=chosen, candidates=candidates)

    # -- the fingerprint check -------------------------------------------

    def recheck(self, page: PageHandle, action: ResolvedAction) -> tuple[PageFacts | None, str]:
        """Re-read the element and compare fingerprints. (facts, error).

        Called by EVERY acting tool at the top of run(), before anything
        happens. A mismatch is not retried and not worked around: the sentence
        the user agreed to described a node that is no longer there, so the
        consent no longer covers anything.
        """
        selector = str(action.args.get("selector") or "")
        expected = str(action.args.get("dom_fingerprint") or "")
        if not selector or not expected:
            return None, "I lost track of which control you meant"
        raw = page.element_facts(selector, limit=2)
        if len(raw) != 1:
            return None, STALE_SUMMARY
        found = factlib.facts_from_raw(
            raw[0],
            page_url=page.url(),
            page_title=page.title(),
            logged_in=page.has_session(),
            pending_dialog=page.pending_dialog(),
        )
        if factlib.dom_fingerprint(found) != expected:
            return None, STALE_SUMMARY
        return found, ""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def browser_spec(
    name: str,
    description: str,
    params: Mapping[str, Any] | None = None,
    *,
    floor: RiskTier,
    activation_hint: str,
    tags: Sequence[str] = (),
    inverses: Sequence[str] = (),
    grantable: bool = True,
) -> ToolSpec:
    """`tools.base.spec()` plus `grantable`, which that helper predates.

    `grantable` is static and human-authored, exactly like `floor`: it says
    whether a scoped grant may ever answer this tool's confirmation on the
    user's behalf. Spending money and sending a page's contents to a remote
    model are never answered in advance, no matter how narrow the scope.
    """
    return ToolSpec(
        name=name,
        description=description,
        params=dict(params or {}),
        floor=floor,
        activation_hint=" ".join(activation_hint.split()),
        tags=tuple(tags),
        inverses=tuple(inverses),
        grantable=grantable,
    )


def expiry(options: BrowserOptions) -> float:
    """When a browser undo row stops being replayable.

    `~/.daa/undo.jsonl` has no pruning, so an `open_tab` undo carrying a URL is
    a browsing-history entry that lives forever. Browser undos expire, and an
    expired row is INERT rather than dangerous.
    """
    return time.time() + float(options.undo_ttl_s)


def expired(args: Mapping[str, Any]) -> bool:
    raw = args.get("expires_at")
    try:
        return bool(raw) and float(raw) < time.time()
    except (TypeError, ValueError):
        return False


def site_of(raw_url: str) -> str:
    return urls.speakable_host(raw_url)


__all__ = [
    "STALE_SUMMARY",
    "BrowserTool",
    "Located",
    "PlaywrightSession",
    "browser_spec",
    "expired",
    "expiry",
    "site_of",
]
