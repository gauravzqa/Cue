"""The risk gate: turn a judgment about ONE invocation into a Disposition.

The whole module exists to enforce a single asymmetry:

    tier = max(spec.floor, derived_tier)

`spec.floor` is a statement about a TOOL'S REACH ("run_shell can do anything").
`assessment` is a statement about THIS INVOCATION ("this particular `ls` is
harmless"). The first is authored by a human and reviewed; the second is
produced at runtime by a model that can be confidently wrong, and by an STT
stack that can mishear "delete the drafts" as "delete the draft". So the
assessment is allowed to make us MORE careful and never less. If that rule ever
becomes two-way, one over-confident Jev answer silently deletes someone's work.

Everything here is pure: same inputs, same Disposition, no I/O, no clock. That
is deliberate — the gate is the one thing that must be exhaustively testable,
and it must never import daa.jev / daa.tools / daa.voice.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from daa.contracts import Disposition, ResolvedAction, RiskAssessment, RiskTier, ToolSpec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from daa.config import Settings

__all__ = [
    "BLAST_ANNOUNCE",
    "BLAST_CONFIRM_VISUAL",
    "BLAST_CONFIRM_VOICE",
    "DRY_RUN_PREFIX",
    "INFERRED",
    "LOW_CONFIDENCE",
    "MAX_POLICY_TIER",
    "UNRECOVERABLE",
    "decide",
    "derive_tier",
]

# --- thresholds -------------------------------------------------------------
# Named rather than inlined so evals/ can sweep them against real outcomes and
# so a test failure points at a rule instead of at a magic number.

BLAST_ANNOUNCE = 0.5         # blast_radius at/above this is worth saying out loud
BLAST_CONFIRM_VOICE = 1.5    # ...worth reading the target back first
BLAST_CONFIRM_VISUAL = 2.5   # ...worth putting on screen

LOW_CONFIDENCE = 0.5         # below this, Jev does not know what it is looking at
UNRECOVERABLE = 0.5          # above this, "undo" may not exist
INFERRED = 0.5               # below this, WE proposed the action, not the user

# Policy escalates, but it never invents a REFUSE. REFUSE means "this is never
# automated", which is a property of the tool and belongs in the registry where
# a human wrote it down -- not something a runtime score should be able to
# assert. Capping here also keeps "escalate by one tier" from silently turning
# a confirmable action into an impossible one.
MAX_POLICY_TIER = RiskTier.CONFIRM_VISUAL

# Prefixed onto the spoken reason whenever settings.dry_run is on, so the
# Disposition itself carries the fact that nothing will mutate. Disposition is
# frozen and owned by contracts.py, so the reason string is the only honest
# place to put it -- and it is the field the user actually hears.
DRY_RUN_PREFIX = "Dry run, so nothing will actually change. "

# --- spoken reasons ---------------------------------------------------------
# Every one of these is read aloud when the user asks "why are you asking?", so
# they are sentences, not log lines: no tool names, no numbers, no paths.

_R_READ_ONLY = "That was just a look, nothing changed."
_R_SMALL = "Small, reversible change, so I just did it and told you."
_R_BIG = "This touches a fair amount, so I want to confirm it first."
_R_HUGE = "This touches a lot at once, so I want you to see it before I do it."
_R_LOW_CONFIDENCE = "I'm not confident I read that right, so I'd rather check with you."
_R_GUESSING = "I'm guessing at which one you meant, so let me confirm the target first."
_R_UNRECOVERABLE = "This can't be undone, so I need you to confirm it on screen."
_R_INFERRED = "You didn't ask for this directly, I worked it out, so I'm checking first."
_R_NO_ASSESSMENT = "I couldn't check this one properly, so I'm being careful."

_FLOOR_REASON = {
    RiskTier.SILENT: _R_READ_ONLY,
    RiskTier.ANNOUNCE: _R_SMALL,
    RiskTier.CONFIRM_VOICE: "I always check before doing this kind of thing.",
    RiskTier.CONFIRM_VISUAL: "This kind of thing always needs confirming on screen.",
    RiskTier.REFUSE: "I don't do that one automatically, you'll want to do it yourself.",
}

_TARGET_CONFIDENCE = ("certain", "probable", "guessing")


def decide(
    action: ResolvedAction,
    spec: ToolSpec,
    assessment: RiskAssessment | None,
    settings: Settings,
) -> Disposition:
    """Decide what must happen before `action` runs.

    Never returns a tier below `spec.floor`. That is the invariant the rest of
    the system is allowed to assume, and test_safety_policy.py asserts it for
    every tier and for a deliberately maximally-safe assessment.
    """
    derived, reason = derive_tier(action, spec, assessment)

    # THE one-way rule. Written as one line, on purpose, so it cannot be
    # refactored into something conditional without someone noticing.
    tier = RiskTier(max(int(spec.floor), int(derived)))

    # The floor won, so the floor is the honest explanation.
    if tier > derived:
        reason = _FLOOR_REASON[tier]

    if getattr(settings, "dry_run", False):
        # We do NOT drop the tier in dry run. A dry run that quietly relaxed the
        # gate would be a gate that is only ever tested in its relaxed form --
        # and the flag is read by the executor, which we do not control. We cap
        # what happens by SAYING it, and by leaving the tier exactly where the
        # live path would leave it.
        reason = DRY_RUN_PREFIX + reason

    return Disposition(tier=tier, reason=reason, assessment=assessment)


def derive_tier(
    action: ResolvedAction,
    spec: ToolSpec,
    assessment: RiskAssessment | None,
) -> tuple[RiskTier, str]:
    """The tier the ASSESSMENT argues for, before the floor is applied.

    Split out from decide() so tests can prove that the floor, not this
    function, is what makes the result safe.
    """
    if assessment is None:
        return _fail_closed(spec)

    blast = _num(assessment.blast_radius, 0.0, 3.0, nan=3.0)
    unrecoverable = _num(assessment.unrecoverable, 0.0, 1.0, nan=1.0)
    requested = _num(assessment.explicitly_requested, 0.0, 1.0, nan=0.0)
    confidence = _num(assessment.confidence, 0.0, 1.0, nan=0.0)
    target = _target_confidence(assessment.target_confidence)

    # Candidates are (tier, tie-break priority, spoken reason). We take the
    # highest tier; ties go to the most specific explanation, because "this
    # can't be undone" is a better answer to "why?" than "this is a big one".
    candidates: list[tuple[int, int, str]] = [_from_blast(blast)]

    # 1. Low confidence IN THE DANGER SIGNALS escalates. An uncertain judgment
    #    is worse than a confident wrong one: a wrong answer can be corrected
    #    by the next question, but an answer nobody can reason about cannot be
    #    corrected at all. So we hand it back to the human.
    #
    #    `assessment.confidence` is the minimum over blast_radius and
    #    unrecoverable ONLY -- the two answers that decide whether being wrong
    #    costs anything (jev/risk.py). It used to be the minimum over all four,
    #    which meant a 0.42 on "is this the right file?" asked the user to
    #    authorise a Spotlight search. The other two answers still escalate,
    #    through rules 2 and 4 below, where their uncertainty is charged once
    #    instead of twice.
    if confidence < LOW_CONFIDENCE:
        candidates.append((int(RiskTier.CONFIRM_VOICE), 2, _R_LOW_CONFIDENCE))

    # 2. "guessing" is about RESOLUTION, not size -- but resolving to the wrong
    #    thing only matters if acting on the wrong thing does damage. Deleting
    #    one wrong file is a small blast radius and a total failure, so blast
    #    radius alone must not be able to talk us out of confirming it;
    #    searching for the wrong thing costs a second search, so it must not
    #    force a confirmation either.
    #
    #    The gate is the tool's OWN declaration first (`is_mutating`, which
    #    reads the human-authored floor and tags in the registry), because the
    #    signal we are distrusting here is Jev's target resolution and it would
    #    be circular to let Jev's own blast estimate be the only thing that
    #    excuses it. Blast radius is kept as a second way IN, never a way out:
    #    if the model insists this is a big one, a guessed target confirms even
    #    when the tool claims to be read-only.
    if target == "guessing" and (is_mutating(spec) or blast >= BLAST_ANNOUNCE):
        candidates.append((int(RiskTier.CONFIRM_VOICE), 3, _R_GUESSING))

    # 3. Voice alone can never authorize destroying something the user cannot
    #    recreate. "yes" is one of the easiest words in English to hallucinate
    #    out of room noise; a visual confirmation needs a deliberate act.
    if unrecoverable > UNRECOVERABLE:
        candidates.append((int(RiskTier.CONFIRM_VISUAL), 4, _R_UNRECOVERABLE))

    tier_value, _, reason = max(candidates)

    # 4. We inferred this rather than being asked for it, so we owe the user a
    #    beat to say no. Both signals count: Jev's probability AND the
    #    resolver's own flag, because the resolver knows things Jev never sees.
    if requested < INFERRED or not action.explicit:
        bumped = min(tier_value + 1, int(MAX_POLICY_TIER))
        if bumped > tier_value:
            tier_value, reason = bumped, _R_INFERRED

    return RiskTier(tier_value), reason


def _from_blast(blast: float) -> tuple[int, int, str]:
    if blast >= BLAST_CONFIRM_VISUAL:
        return (int(RiskTier.CONFIRM_VISUAL), 1, _R_HUGE)
    if blast >= BLAST_CONFIRM_VOICE:
        return (int(RiskTier.CONFIRM_VOICE), 1, _R_BIG)
    if blast >= BLAST_ANNOUNCE:
        return (int(RiskTier.ANNOUNCE), 1, _R_SMALL)
    return (int(RiskTier.SILENT), 1, _R_READ_ONLY)


def _fail_closed(spec: ToolSpec) -> tuple[RiskTier, str]:
    """No assessment (Jev down, timed out, no key) must not read as "proceed".

    Absence of evidence is the single most common failure mode of a judgment
    layer, and it arrives exactly when things are already going wrong. So we
    raise the tool's own floor by one and, for anything that mutates, refuse to
    sit below a spoken confirmation. A read-only tool still stays cheap, so a
    Jev outage degrades the assistant instead of bricking it.
    """
    tier = min(int(spec.floor) + 1, int(MAX_POLICY_TIER))
    if is_mutating(spec):
        tier = max(tier, int(RiskTier.CONFIRM_VOICE))
    return RiskTier(max(int(spec.floor), tier)), _R_NO_ASSESSMENT


def is_mutating(spec: ToolSpec) -> bool:
    """Best available answer from the contract alone.

    ToolSpec has no `mutates` field, and safety/ may not import the registry to
    go looking. RiskTier.SILENT is documented in contracts.py as "read-only",
    so a SILENT floor is a tool author asserting exactly that; tags let an
    author be explicit either way.
    """
    tags = tuple(spec.tags)
    if "mutates" in tags:
        return True
    # "read" is the tag the registry actually uses (get_clipboard, list_windows,
    # spotlight_search, reveal_in_finder); the other two spellings were here
    # first and cost nothing to keep. Without it the only thing asserting
    # read-only-ness was the floor, which makes a tool author's explicit tag
    # decorative -- and this function is now load-bearing for rule 2, not just
    # for the no-assessment path.
    if tags and ({"read", "read_only", "readonly"} & set(tags)):
        return False
    return spec.floor > RiskTier.SILENT


def _num(value: object, lo: float, hi: float, *, nan: float) -> float:
    """Clamp a Jev number into range, resolving garbage in the SAFE direction.

    A NaN or a nonsense value means the judgment layer misbehaved, which is a
    reason to be more careful, not a reason to crash the loop mid-utterance.
    """
    try:
        x = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return nan
    if math.isnan(x):
        return nan
    return max(lo, min(hi, x))


def _target_confidence(raw: object) -> str:
    """Anything we don't recognise is treated as "guessing".

    A typo or a new label from a model update must not silently become the
    permissive branch.
    """
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in _TARGET_CONFIDENCE:
            return value
    return "guessing"
