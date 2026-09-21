"""Scoped consent: whether a bargain the user already struck covers this step.

This module answers exactly one question, and it is NOT the question
`policy.decide()` answers:

    policy.decide()   what must happen before this action runs
    grant_satisfies() did the user already do that thing

Those are different questions and they must not share a function. `decide()`
computes `tier = max(spec.floor, floor_hint, derived)` and a grant is never an
input to it -- consulting a grant inside the tier calculation is precisely how
a scoped permission becomes a blanket one. The loop calls `decide()` first,
gets a tier, and only then asks this module whether a live grant can supply the
ANSWER to the confirmation that tier demands.

Everything here is pure in the same sense `policy.py` is pure: no filesystem,
no network, no `time.time()`. The clock arrives as `now`, so budgets, expiries
and warrant TTLs are testable without sleeping. The one concession is
`os.path.expanduser` in the path-prefix check, which reads $HOME and nothing
else.

WHY A GRANT IS NOT A FLOOR LOWERING
-----------------------------------
`spec.floor` says "this tool's reach always warrants at least this much care".
A grant does not touch it. A CONFIRM_VOICE-floored tool inside a grant is still
assessed at CONFIRM_VOICE, still produces a Disposition, still writes a
confirmation row, and still cannot run without an authorization token. What the
grant changes is only WHERE THAT CONFIRMATION'S YES CAME FROM -- a specific,
bounded, expiring, logged, revocable sentence the user said about this exact
class of work, instead of a fresh prompt. There is no tier in this system that
means "was skipped".

Three things make that defensible, and all three live in `grant_satisfies`:

    the channel cap      a spoken grant can never answer a typed question
    spec.grantable       a whole category is outside the mechanism, by hand
    empty means empty    a scope nobody filled in authorizes nothing

WHAT THIS MODULE CANNOT DEFEND AGAINST
--------------------------------------
Consent fatigue. Every clause below holds under adversarial analysis and none
of them stop a user who has heard the same four-clause sentence twenty times
from saying "yeah" at the second clause. The mitigations are structural and
live partly outside this file: short expiries, no persistence, no templates,
no "always allow", and `readback()` naming the MOST DESTRUCTIVE thing in scope
rather than the average one -- which is the only clause that makes the sentence
stop sounding the same each time.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from daa.contracts import (
    Budget,
    Disposition,
    Grant,
    GrantScope,
    ResolvedAction,
    RiskTier,
    ToolSpec,
    Warrant,
)
from daa.safety.policy import UNRECOVERABLE

__all__ = [
    "DEFAULT_GRANT_TTL_S",
    "DEFAULT_WARRANT_TTL_S",
    "SPEND_CONSEQUENCES",
    "GrantBook",
    "GrantState",
    "IssuedWarrant",
    "WarrantBook",
    "action_digest",
    "budget_exhausted",
    "grant_satisfies",
    "issue_grant",
    "readback",
    "visual_card",
]

# Short on purpose. A grant is not a setting, it is a sentence someone said a
# moment ago, and a forty-minute-old yes is not a yes. The friction IS the
# feature: there is no "remember this", no template, and no persistence, so a
# grant dies with the process that heard it.
DEFAULT_GRANT_TTL_S = 300.0

# A confirmation that has become a lie by elapsed time. "Should I move these
# three?" -- yes -- ninety seconds of agent work -- then the move, against a
# folder that has moved on. Expiry forces re-authorization, which re-runs
# resolve() and re-reads the world.
DEFAULT_WARRANT_TTL_S = 30.0

# Consequence keys that mean money changes hands. A grant whose budget allows
# no spend cannot answer for a step that declares one, whatever else is true.
SPEND_CONSEQUENCES = frozenset({"spend", "charge", "payment", "pay", "cost", "purchase"})

# Tags that put a tool outside the mechanism regardless of `grantable`.
_NEVER_GRANTABLE_TAGS = frozenset({"irreversible"})

# The two channels a grant may arrive on. Anything else is not a channel we
# know how to reason about, and `Grant.channel_cap` maps every unknown string
# to CONFIRM_VISUAL -- i.e. the PERMISSIVE reading. So the value is checked
# here rather than trusted there.
_CHANNELS = frozenset({"voice", "visual"})

# Argument keys whose value names a place on disk. Same list as the undo
# journal's, deliberately: "what counts as a path" should have one definition.
_PATH_KEYS = frozenset(
    {"path", "paths", "src", "dst", "source", "destination", "targets", "pairs", "files"}
)
_MAX_DEPTH = 12
_MAX_PATHS = 256


# ---------------------------------------------------------------------------
# Consumption
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class GrantState:
    """The meter. `Grant` is frozen; only this moves.

    Freezing the bargain and mutating only the meter is what makes "who
    authorized what" a reconstructible fact rather than a last-write-wins
    field. Same split as `UndoEntry` (frozen) beside `UndoJournal` (mutable).
    """

    grant_id: str
    steps_used: int = 0
    spent_cents: int = 0
    revoked_at: float | None = None
    revoked_reason: str = ""
    # Origins this grant has actually been used against, for the audit row and
    # for the dock UI's live card. Not a permission -- `scope.origins` is.
    seen_origins: set[str] = field(default_factory=set)

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    def revoke(self, at: float, reason: str = "") -> bool:
        """Idempotent, and it always succeeds. "Stop" means stop."""
        if self.revoked_at is not None:
            return False
        self.revoked_at = float(at)
        self.revoked_reason = reason
        return True

    def spend_step(self, action: ResolvedAction | None = None) -> None:
        self.steps_used += 1
        origin = getattr(action, "origin", None)
        if isinstance(origin, str) and origin:
            self.seen_origins.add(origin)


def budget_exhausted(
    budget: Budget,
    *,
    steps_used: int,
    elapsed_s: float,
    spent_cents: int = 0,
) -> str:
    """"" if there is room for another step, else a SPOKEN reason there is not.

    Exhaustion is a re-confirmation, not a failure: the user said yes to THIS
    much. A string rather than a bool because the user hears why.
    """
    if steps_used >= int(getattr(budget, "steps", 0) or 0):
        return "that's as many steps as you okayed"
    if elapsed_s >= float(getattr(budget, "seconds", 0.0) or 0.0):
        return "that's as long as you okayed"
    # Strictly greater: spending nothing against a zero budget is fine, and
    # spending anything against it is not. There is no sentinel for unlimited.
    if spent_cents > int(getattr(budget, "spend_cents", 0) or 0):
        return "that would cost more than you okayed"
    return ""


# ---------------------------------------------------------------------------
# The satisfaction rule
# ---------------------------------------------------------------------------


def grant_satisfies(
    grant: Grant | None,
    state: GrantState | None,
    action: ResolvedAction,
    spec: ToolSpec,
    disposition: Disposition,
    now: float,
) -> tuple[bool, str]:
    """True only if EVERY clause holds. The reason string is SPOKEN.

    Twelve conjunctive clauses, every one fail-closed, written as a flat
    sequence with no branch that can widen. Anything this function cannot
    positively show to be inside the grant is outside it.

    Note what clause 3 does NOT do: it does not change `disposition.tier`. The
    tier that was computed is the tier that is logged, the tier the warrant
    carries and the tier the audit log reports. A step that ran at
    CONFIRM_VOICE under a grant is recorded as a CONFIRM_VOICE step whose
    confirmation arrived `via="grant"`.
    """
    # 0. Nothing to consult. Not an error and not a permission.
    if grant is None or state is None:
        return False, "I don't have your go-ahead for that"
    if not isinstance(disposition, Disposition):
        return False, "I couldn't work out how careful to be, so I'm asking"
    if not isinstance(spec, ToolSpec):
        return False, "I don't recognise that tool well enough to assume"

    # 1. Revocation beats everything, including a warrant already issued.
    if state.revoked:
        return False, "you told me to stop"

    # 2. Consent decays. A forty-minute-old yes is not a yes.
    if not now < float(grant.expires_at):
        return False, "what you okayed earlier has run out"

    # 3. The ceiling AND the channel cap. A spoken yes does not become a typed
    #    yes by being made in advance for a wider scope -- it becomes weaker,
    #    because it was made with less information. `Grant.channel_cap` maps
    #    every unrecognised channel to CONFIRM_VISUAL, which is the permissive
    #    reading, so the channel itself is checked first.
    if str(grant.granted_via) not in _CHANNELS:
        return False, "I'm not sure how you okayed that, so I'm asking again"
    tier = disposition.tier
    if not isinstance(tier, RiskTier):
        return False, "I couldn't work out how careful to be, so I'm asking"
    if tier > grant.max_satisfiable:
        if tier is RiskTier.CONFIRM_VISUAL and grant.granted_via == "voice":
            return False, "this one needs approving on screen, which you can't do out loud"
        return False, "this is bigger than what you okayed"

    # 4. REFUSE is never satisfiable, by anything. Belt and braces: the loop
    #    never reaches here on a REFUSE, and if it ever does, this holds.
    if tier is RiskTier.REFUSE:
        return False, "that's not something I do automatically"

    # 5. The static, human-authored, reviewed list. Sending a message to a
    #    person, spending money, changing auth state, deleting past the Trash:
    #    never answered in advance, at any ceiling, ever.
    if not bool(getattr(spec, "grantable", False)):
        return False, "that kind of thing I always check individually"

    # 6. Belt and braces on 5, using the machinery that already exists rather
    #    than a parallel list.
    if _NEVER_GRANTABLE_TAGS & {str(t) for t in (spec.tags or ())}:
        return False, "that one can't be undone, so I'll ask every time"

    # 7. The runtime RAISE, which is always allowed. A tool that claims
    #    grantable=True does not get to overrule the judgment that THIS
    #    invocation may not be recoverable. Fail closed: no assessment at all
    #    means we could not check, which is not the same as checking and
    #    finding nothing.
    assessment = getattr(disposition, "assessment", None)
    if assessment is None:
        return False, "I couldn't check that one properly, so I'm asking"
    try:
        unrecoverable = float(getattr(assessment, "unrecoverable", 1.0))
    except (TypeError, ValueError):
        unrecoverable = 1.0
    if unrecoverable > UNRECOVERABLE:
        return False, "this one might not be undoable, so I want to check"

    # 8. The allow-list. Empty means EMPTY -- a grant with no tools covers
    #    nothing. This is the single most important line in the module: the
    #    permissive reading of an empty allow-list is how every scoped
    #    permission system eventually becomes a blanket one.
    if not grant.scope.covers_tool(spec.name):
        return False, "that isn't one of the things you okayed"

    # 9. Place. See `_origin_covered` for why an unlabelled action is not an
    #    origin-free one.
    ok, why = _origin_covered(grant.scope, action, spec)
    if not ok:
        return False, why

    # 10. Paths, which keep a file agent in its lane.
    ok, why = _paths_covered(grant.scope, action)
    if not ok:
        return False, why

    # 11. An undescribed consequence was never consented to. The readback is
    #     the bargain; a consequence discovered mid-run that was not in it is
    #     new information, and new information is a new question.
    briefed = {str(c) for c in (grant.briefed_consequences or ())}
    unbriefed = sorted({str(k) for k in (action.consequences or {})} - briefed)
    if unbriefed:
        return False, "that does something I didn't mention when you okayed it"

    # 12. Finite. Including spend_cents == 0 meaning any spend re-confirms.
    if _declares_spend(action, spec) and int(getattr(grant.budget, "spend_cents", 0) or 0) <= 0:
        return False, "that costs money, which you didn't okay"
    spent = budget_exhausted(
        grant.budget,
        steps_used=state.steps_used,
        elapsed_s=max(0.0, now - float(grant.granted_at)),
        spent_cents=state.spent_cents,
    )
    if spent:
        return False, spent

    return True, "you okayed this when you asked me to " + (grant.goal or "do this").strip()


def _origin_covered(
    scope: GrantScope, action: ResolvedAction, spec: ToolSpec
) -> tuple[bool, str]:
    """Is the place this action reaches inside the grant?

    `ResolvedAction.origin` is supplied by the resolver, and a resolver that
    forgets leaves it None. Reading None as "no origin constraint" is the
    permissive default and the wrong one, so:

      * an action that CARRIES an origin must have it named in the scope;
      * an action that carries none is covered only when the grant is not
        place-scoped at all AND the tool is not one that drives a browser or
        an app -- `long_running=True` is the marker for the tools whose whole
        job is to be somewhere, and for those a missing origin is a bug, not
        an absence.

    So a file grant (no origins, no long_running tool) works without anyone
    inventing an origin, and a browser grant cannot be widened by a resolver
    that forgot to say where it was.
    """
    origin = getattr(action, "origin", None)
    place_scoped = bool(scope.origins or scope.apps)
    if origin is None:
        if bool(getattr(spec, "long_running", False)):
            return False, "I can't tell where that would happen, so I'm checking"
        if place_scoped:
            return False, "I can't tell whether that's somewhere you okayed"
        return True, ""
    if not isinstance(origin, str) or not origin.strip():
        return False, "I can't tell where that would happen, so I'm checking"
    if origin in scope.origins or origin in scope.apps:
        return True, ""
    return False, "that's somewhere you didn't mention"


def _paths_covered(scope: GrantScope, action: ResolvedAction) -> tuple[bool, str]:
    """Every path this action names must sit under one of the allowed prefixes.

    An action that names no paths passes trivially. An action that names one
    under a grant with no prefixes does not: empty means empty here too.
    """
    paths = _candidate_paths(action)
    if not paths:
        return True, ""
    prefixes = [p for p in (_normalise(str(p)) for p in (scope.path_prefixes or ())) if p]
    if not prefixes:
        return False, "that touches files outside what you okayed"
    for raw in paths:
        norm = _normalise(raw)
        if norm is None or not any(_under(norm, prefix) for prefix in prefixes):
            return False, "that touches something outside the folder you okayed"
    return True, ""


def _normalise(raw: str) -> str | None:
    """An absolute, dot-free path, or None if it cannot be made into one.

    `os.path.realpath` is deliberately NOT used: it stats the filesystem, and
    this module is pure. `expanduser` reads $HOME and nothing else. A relative
    path cannot be shown to be inside anything -- the process's cwd at grant
    time and at step time are not the same directory -- so it is None, which
    every caller reads as "not covered".
    """
    text = str(raw or "").strip()
    if not text:
        return None
    text = os.path.expanduser(text)
    if not text.startswith("/"):
        return None
    norm = os.path.normpath(text)
    # normpath resolves "a/../b" textually; anything left is a symlink trick or
    # a malformed path, and either way we cannot vouch for where it lands.
    if ".." in norm.split("/"):
        return None
    return norm


def _under(path: str, prefix: str) -> bool:
    """Containment with a component boundary. `/Users/sa` does not contain
    `/Users/sanjay`, and a prefix check without this is how a scope leaks."""
    if path == prefix:
        return True
    return path.startswith(prefix.rstrip("/") + "/")


def _candidate_paths(action: ResolvedAction) -> list[str]:
    """Path-shaped strings anywhere in the action's args or targets.

    Same walk as the undo journal's, and for the same reason: every undo this
    product emits is shaped `{"pairs": [[src, dst], ...]}`, and a one-level
    scan finds exactly zero strings in it.
    """
    found: list[str] = []
    _collect(dict(action.args or {}), False, found)
    for target in action.targets or ():
        text = str(target)
        if _looks_like_path(text) and text not in found and len(found) < _MAX_PATHS:
            found.append(text)
    return found


def _collect(value: Any, explicit: bool, out: list[str], depth: int = 0) -> None:
    if depth > _MAX_DEPTH or len(out) >= _MAX_PATHS:
        return
    if isinstance(value, str):
        if (explicit or _looks_like_path(value)) and value not in out:
            out.append(value)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _collect(item, explicit or str(key).lower() in _PATH_KEYS, out, depth + 1)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _collect(item, explicit, out, depth + 1)


def _looks_like_path(value: str) -> bool:
    return value.startswith(("/", "~/", "./", "../"))


def _declares_spend(action: ResolvedAction, spec: ToolSpec) -> bool:
    keys = {str(k).lower() for k in (action.consequences or {})}
    tags = {str(t).lower() for t in (spec.tags or ())}
    return bool((keys | tags) & SPEND_CONSEQUENCES)


# ---------------------------------------------------------------------------
# Issuance
# ---------------------------------------------------------------------------


def issue_grant(
    *,
    goal: str,
    plan_summary: str,
    ceiling: RiskTier,
    granted_via: str,
    scope: GrantScope,
    budget: Budget,
    now: float,
    ttl_s: float = DEFAULT_GRANT_TTL_S,
    parent_id: str | None = None,
    briefed_consequences: Sequence[str] = (),
) -> Grant:
    """Mint a grant. A re-confirmation CHAINS via `parent_id`; it never edits
    its parent, so the log can always reconstruct the original bargain
    separately from every widening of it."""
    return Grant(
        id=uuid.uuid4().hex[:12],
        goal=goal,
        plan_summary=plan_summary,
        ceiling=ceiling,
        granted_via=granted_via,
        scope=scope,
        budget=budget,
        granted_at=float(now),
        expires_at=float(now) + max(0.0, float(ttl_s)),
        parent_id=parent_id,
        briefed_consequences=tuple(str(c) for c in briefed_consequences),
    )


class GrantBook:
    """Live grants and their meters. In memory, and that is the point.

    Nothing here is written to disk. There is no "remember this for next
    time", no template and no per-site allow-list, because the whole risk this
    design carries is habituation, and persistence is habituation with a
    database behind it.
    """

    def __init__(self) -> None:
        self._grants: dict[str, Grant] = {}
        self._states: dict[str, GrantState] = {}

    def add(self, grant: Grant) -> GrantState:
        self._grants[grant.id] = grant
        state = GrantState(grant_id=grant.id)
        self._states[grant.id] = state
        return state

    def get(self, grant_id: str | None) -> tuple[Grant | None, GrantState | None]:
        if not grant_id:
            return None, None
        return self._grants.get(grant_id), self._states.get(grant_id)

    def state(self, grant_id: str) -> GrantState | None:
        return self._states.get(grant_id)

    def revoke(self, grant_id: str, reason: str, *, now: float) -> bool:
        state = self._states.get(grant_id)
        if state is None:
            return False
        return state.revoke(now, reason)

    def live(self, *, now: float) -> list[Grant]:
        return [
            g
            for g in self._grants.values()
            if not self._states[g.id].revoked and now < g.expires_at
        ]

    def __len__(self) -> int:
        return len(self._grants)


# ---------------------------------------------------------------------------
# Warrants
# ---------------------------------------------------------------------------


def action_digest(action: ResolvedAction) -> str:
    """sha256 over the fields that make this action THIS action.

    A boolean cannot cross a thread boundary safely: by the time the worker
    acts on `confirmed=True`, the grant may have been revoked, the action may
    have been rebuilt, or the readback may be two minutes stale. A digest can,
    because it names what was authorized.

    `targets` and `consequences` are in the digest because they are what the
    user HEARD; re-resolving to different targets is a different bargain even
    when the arguments are byte-identical.
    """
    canonical = json.dumps(
        {
            "tool": str(action.tool),
            "args": _jsonable(action.args),
            "targets": [str(t) for t in (action.targets or ())],
            "explicit": bool(action.explicit),
            "verb": str(action.verb or ""),
            "origin": action.origin,
            "floor_hint": int(action.floor_hint) if action.floor_hint is not None else None,
            "consequences": {str(k): str(v) for k, v in dict(action.consequences or {}).items()},
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()


@dataclass(frozen=True, slots=True)
class IssuedWarrant:
    """The book's own record of a warrant. Carries the one thing the frozen
    `Warrant` cannot: whether the authorization came over a channel good
    enough for CONFIRM_VISUAL.

    It lives here rather than on the contract because `Warrant.via` is a
    three-valued string and "grant" alone does not say which channel the grant
    arrived on -- and a spoken grant answering a visual confirmation is the one
    thing the tier exists to prevent.
    """

    warrant: Warrant
    visual_ok: bool
    job_id: str | None = None
    step_index: int | None = None
    spent_at: float | None = None


class WarrantBook:
    """Issues warrants and spends them exactly once.

    Single-use and short-lived is what makes revocation mean something: every
    warrant issued under a grant and not yet spent becomes worthless the
    instant the grant is revoked, because `_execute` re-checks nothing else.
    """

    def __init__(self, *, ttl_s: float = DEFAULT_WARRANT_TTL_S) -> None:
        self.ttl_s = float(ttl_s)
        self._issued: dict[str, IssuedWarrant] = {}

    def issue(
        self,
        action: ResolvedAction,
        disposition: Disposition,
        *,
        via: str,
        now: float,
        grant: Grant | None = None,
        job_id: str | None = None,
        step_index: int | None = None,
        ttl_s: float | None = None,
    ) -> Warrant:
        if via == "grant":
            # A grant-issued warrant is only ever as good as the channel the
            # grant arrived on. Nothing about being made in advance upgrades it.
            visual_ok = grant is not None and grant.granted_via == "visual"
        else:
            visual_ok = via == "visual"
        warrant = Warrant(
            id=uuid.uuid4().hex[:12],
            action_digest=action_digest(action),
            tier=disposition.tier,
            issued_at=float(now),
            expires_at=float(now) + (self.ttl_s if ttl_s is None else float(ttl_s)),
            grant_id=grant.id if grant is not None else None,
            via=via,
        )
        self._issued[warrant.id] = IssuedWarrant(
            warrant=warrant, visual_ok=visual_ok, job_id=job_id, step_index=step_index
        )
        return warrant

    def spend(self, warrant_id: str, *, now: float | None = None) -> IssuedWarrant | None:
        """Consume a warrant. None means unknown, forged, or already spent."""
        record = self._issued.get(warrant_id)
        if record is None or record.spent_at is not None:
            return None
        spent = IssuedWarrant(
            warrant=record.warrant,
            visual_ok=record.visual_ok,
            job_id=record.job_id,
            step_index=record.step_index,
            spent_at=float(now) if now is not None else 0.0,
        )
        self._issued[warrant_id] = spent
        return spent

    def revoke_grant(self, grant_id: str) -> int:
        """Invalidate every unspent warrant issued under `grant_id`."""
        killed = 0
        for key, record in list(self._issued.items()):
            if record.warrant.grant_id == grant_id and record.spent_at is None:
                del self._issued[key]
                killed += 1
        return killed

    def get(self, warrant_id: str) -> IssuedWarrant | None:
        return self._issued.get(warrant_id)


# ---------------------------------------------------------------------------
# The readback
# ---------------------------------------------------------------------------


def readback(
    grant: Grant,
    specs: Sequence[ToolSpec] = (),
    *,
    uncertain: str = "",
) -> str:
    """The sentence the user hears before they say yes. FOUR required clauses.

    Same honesty bar as `ResolvedAction.describe()`: verb-first, consequences
    named, and it must not be able to lie by omission.

    1. THE GOAL, in the user's own words.
    2. THE PLAN, naming the MOST DESTRUCTIVE thing in scope, not the average
       one. "I'll open the three invoices and rename them" is a lie of framing
       if the rename can overwrite. This clause is the one that makes the
       sentence stop sounding the same every time, which is the only defence
       against the user learning its shape and saying yes at clause two.
    3. THE BOUNDS, in units a person has: steps AND time AND place.
    4. THE STOP LINE, and what will still interrupt.

    A fifth clause is required whenever it is true, and it is the clause that
    makes this honest rather than clever: what the grant CANNOT promise. An
    agent grant that fabricates targets it does not have is a ResolvedAction
    with a made-up verb.
    """
    goal = str(grant.goal or "").strip().rstrip(".")
    worst = _most_destructive(grant, specs)
    parts = [
        f"You asked me to {goal}." if goal else "You asked me to get on with something.",
        f"The most it could do is {worst}.",
        f"I'll stay {_bounds(grant)}.",
    ]
    if uncertain.strip():
        parts.append(uncertain.strip().rstrip(".") + ".")
    parts.append(f"{_stop_line(grant, specs)} Say stop any time. Okay?")
    return " ".join(p for p in parts if p)


def _most_destructive(grant: Grant, specs: Sequence[ToolSpec]) -> str:
    """The worst thing the scope allows, described the way a person would
    recognise it. Ties break toward the irreversible one, then alphabetically
    so the sentence is stable across runs."""
    in_scope = [s for s in specs if s.name in grant.scope.tools]
    if not in_scope:
        return "nothing, until you tell me what I'm allowed to use"

    def rank(spec: ToolSpec) -> tuple[int, int, str]:
        irreversible = 1 if _NEVER_GRANTABLE_TAGS & {str(t) for t in (spec.tags or ())} else 0
        return (irreversible, int(spec.floor), spec.name)

    worst = max(in_scope, key=rank)
    text = str(worst.description or worst.name.replace("_", " ")).strip().rstrip(".")
    return text[0].lower() + text[1:] if text else worst.name.replace("_", " ")


def _bounds(grant: Grant) -> str:
    budget = grant.budget
    steps = int(getattr(budget, "steps", 0) or 0)
    seconds = float(getattr(budget, "seconds", 0.0) or 0.0)
    bits = []
    place = _place(grant.scope)
    if place:
        bits.append(place)
    bits.append(f"under {steps} steps" if steps else "with no steps at all")
    bits.append(_minutes(seconds))
    if int(getattr(budget, "spend_cents", 0) or 0) <= 0:
        bits.append("and I won't spend anything")
    return ", ".join(bits)


def _place(scope: GrantScope) -> str:
    places = sorted(scope.origins) + sorted(scope.apps) + [
        p for p in scope.path_prefixes if str(p).strip()
    ]
    if not places:
        return ""
    shown = [_speakable_place(p) for p in places[:2]]
    if len(places) > 2:
        shown.append(f"{len(places) - 2} other places")
    return "only in " + " and ".join(shown)


def _speakable_place(raw: str) -> str:
    text = str(raw)
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            return text[len(prefix) :]
    if text.startswith(("/", "~")):
        return text.rstrip("/").rsplit("/", 1)[-1] or text
    return text


def _minutes(seconds: float) -> str:
    if seconds <= 0:
        return "for no time at all"
    if seconds < 90:
        return f"for about {round(seconds)} seconds"
    return f"for about {round(seconds / 60.0)} minutes"


def _stop_line(grant: Grant, specs: Sequence[ToolSpec]) -> str:
    """Clause 4. Names what will STILL be asked about individually, because a
    grant that does not say where it ends is one the user will assume has no
    end."""
    excluded = sorted(
        s.name.replace("_", " ")
        for s in specs
        if s.name in grant.scope.tools
        and (
            not getattr(s, "grantable", True)
            or _NEVER_GRANTABLE_TAGS & {str(t) for t in (s.tags or ())}
            or s.floor > grant.max_satisfiable
        )
    )
    if excluded:
        head = excluded[0] if len(excluded) == 1 else f"{excluded[0]} and {len(excluded) - 1} more"
        return f"I'll still stop and ask before I {head}."
    if grant.granted_via == "voice":
        return "I'll still stop and ask before anything that can't be undone."
    return "I'll still stop and ask before anything outside that."


def visual_card(grant: Grant, specs: Sequence[ToolSpec] = ()) -> str:
    """The grant, in full, on screen -- the same discipline as the loop's
    `_visual_detail`: print EVERYTHING. A prettier card that shows less is a
    downgrade of the tier, so the dock UI should render this and nothing less.
    """
    rule = "─" * 68
    by_name = {s.name: s for s in specs}
    lines = [
        "",
        rule,
        "daa needs your approval ON SCREEN for a scoped go-ahead.",
        f"  goal       {grant.goal}",
        f"  readback   {grant.plan_summary}",
        f"  ceiling    {grant.ceiling.name}",
        f"  channel    {grant.granted_via}",
        f"  expires    {_minutes(max(0.0, grant.expires_at - grant.granted_at))} from now",
    ]
    tools = sorted(grant.scope.tools)
    lines.append(f"  tools      {len(tools)}")
    for name in tools:
        spec = by_name.get(name)
        described = spec.description if spec is not None else "(not registered here)"
        lines.append(f"               - {described}")
    for label, values in (
        ("origins", sorted(grant.scope.origins)),
        ("apps", sorted(grant.scope.apps)),
        ("folders", list(grant.scope.path_prefixes)),
    ):
        lines.append(f"  {label:<10} {len(values)}")
        lines.extend(f"               - {v}" for v in values)
    lines.append(f"  budget     {grant.budget.steps} steps")
    lines.append(f"             {grant.budget.seconds:g} seconds")
    lines.append(f"             {grant.budget.spend_cents} cents")
    briefed = sorted(grant.briefed_consequences)
    lines.append(f"  told about {len(briefed)}")
    lines.extend(f"               - {c}" for c in briefed)
    lines.append(f"  ! still asked individually: {_stop_line(grant, specs)}")
    lines.append(rule)
    return "\n".join(lines) + "\n"


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple, set, frozenset)) or (
        isinstance(value, Iterable) and not isinstance(value, (str, bytes))
    ):
        return [_jsonable(v) for v in value]
    return str(value)
