"""How dangerous is this, really?

The input is a ResolvedAction, never a raw utterance. "Delete the old ones" is
not a risk judgment anyone can make; "move Screenshot 2026-09-19.png and 41 more
to the Trash" is. Everything this module sends Jev is built from the RESOLVED
targets for that reason -- assessing the request instead of the action is how a
vague sentence ends up authorising a specific disaster.

`RiskAssessment.confidence` that comes out of here is deliberately NOT a
confidence in the whole judgment. It is the confidence in the two answers that
decide whether being wrong hurts -- blast_radius and unrecoverable. How sure
Jev was about the other two is kept on the assessment (see
DetailedRiskAssessment) for the audit log, but it does not move a tier on its
own, because each of those two answers already has its own rule in
safety/policy.py and charging their uncertainty twice is what made the
assistant ask permission before running a search.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from daa.config import Settings
from daa.contracts import JevProvider, ResolvedAction, RiskAssessment
from daa.jev import questions as Q
from daa.jev.client import JevUnavailable, jsonable


@dataclass(frozen=True, slots=True)
class DetailedRiskAssessment(RiskAssessment):
    """A RiskAssessment that also remembers how sure Jev was of EACH answer.

    `RiskAssessment.confidence` is now the DANGER confidence alone (see below),
    which is the right number for the gate and the wrong number for the log:
    "why did it not ask me?" is often answered by "because it was only 38% sure
    you had asked for it". Keeping the other two here means the audit record
    carries all four -- audit._plain() flattens dataclass fields, so the extra
    field lands in the log with no call site changing.

    A subclass rather than a field on RiskAssessment itself only because
    contracts.py is not mine to edit; `answer_confidence` belongs there, and
    this type should collapse into it the moment that is possible. It is a
    real RiskAssessment (isinstance passes, policy reads it unchanged), so
    nothing downstream needs to know it exists.
    """

    answer_confidence: Mapping[str, float] = field(default_factory=dict)

# What the gate returns when it has no judgment at all. Top of the blast-radius
# scale, zero confidence: safety/policy.py maps that to its most cautious tier,
# so a Jev outage degrades daa into asking permission for everything rather than
# into acting without it.
FAILED_CLOSED = RiskAssessment(
    blast_radius=3.0,
    unrecoverable=1.0,
    explicitly_requested=0.0,
    target_confidence="guessing",
    confidence=0.0,
    # Not a live judgment, so it must never be logged as one. `synthetic` is the
    # flag every audit consumer already checks for exactly this.
    synthetic=True,
)

# Enough turns to catch "...actually, do it to all of them" without paying for
# the whole session on every assessment.
_HISTORY_TURNS = 6


class RiskGate:
    def __init__(self, provider: JevProvider, settings: Settings) -> None:
        self._provider = provider
        self._settings = settings

    def assess(
        self,
        action: ResolvedAction,
        utterance: str,
        history: Sequence[Any] = (),
    ) -> RiskAssessment:
        state = {
            "utterance": utterance,
            "action": {
                "tool": action.tool,
                "spoken_description": action.describe(),
                # The concrete, resolved targets. This is the field the whole
                # assessment turns on.
                "targets": [str(t) for t in action.targets],
                "target_count": len(action.targets),
                "arguments": jsonable(action.args),
                "user_named_the_target": action.explicit,
            },
            "recent_turns": [str(turn) for turn in list(history)[-_HISTORY_TURNS:]],
        }

        try:
            answers = self._provider.ask(state, Q.risk_questions(), timeout_s=2.0)
        except JevUnavailable:
            return FAILED_CLOSED

        blast = answers.score(Q.Q_BLAST_RADIUS)
        unrecoverable = answers.answers[Q.Q_UNRECOVERABLE]
        explicit = answers.answers[Q.Q_EXPLICITLY_REQUESTED]
        target = answers.choice(Q.Q_TARGET_CONFIDENCE)

        # MINIMUM, not mean, over the two DANGER answers only.
        #
        # Minimum because policy combines them as a conjunction: the assessment
        # is only as trustworthy as its shakiest component, and averaging would
        # let a confident "tiny" hide a coin-flip on whether it can be undone.
        #
        # Danger answers ONLY because of what the number is used for. Policy's
        # low-confidence rule asks "if this judgment is wrong, does it hurt?",
        # and only blast_radius and unrecoverable answer that. Live Jev on
        # `spotlight_search` returned 0.98 and 0.96 on those two -- both very
        # sure the action is harmless -- alongside 0.38 on "did they ask for
        # this" and 0.42 on "is this the right target". Folding those in made
        # the product ask permission before running a Spotlight search.
        #
        # It also double-counted: explicitly_requested and target_confidence
        # each already drive their own escalation rule in policy.py, so their
        # uncertainty was being charged twice -- once through their own rule
        # and again through a global minimum that could raise the tier on its
        # own. Being unsure whether the user asked for a search is not a reason
        # to interrupt them; being unsure how much damage it does is.
        confidence = min(blast.confidence, unrecoverable.confidence)

        return DetailedRiskAssessment(
            blast_radius=blast.score,
            unrecoverable=answers.noul(Q.Q_UNRECOVERABLE),
            explicitly_requested=answers.noul(Q.Q_EXPLICITLY_REQUESTED),
            target_confidence=target.choice,
            confidence=confidence,
            synthetic=answers.synthetic,
            # Nothing is thrown away: all four are still on the record, they
            # just no longer all get a vote on the tier.
            answer_confidence={
                Q.Q_BLAST_RADIUS: blast.confidence,
                Q.Q_UNRECOVERABLE: unrecoverable.confidence,
                Q.Q_EXPLICITLY_REQUESTED: explicit.confidence,
                Q.Q_TARGET_CONFIDENCE: target.confidence,
            },
        )
