"""How dangerous is this, really?

The input is a ResolvedAction, never a raw utterance. "Delete the old ones" is
not a risk judgment anyone can make; "move Screenshot 2026-09-19.png and 41 more
to the Trash" is. Everything this module sends Jev is built from the RESOLVED
targets for that reason -- assessing the request instead of the action is how a
vague sentence ends up authorising a specific disaster.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from daa.config import Settings
from daa.contracts import JevProvider, ResolvedAction, RiskAssessment
from daa.jev import questions as Q
from daa.jev.client import JevUnavailable, jsonable

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

        # MINIMUM, not mean. These four answers are combined by policy as a
        # conjunction, so the assessment is only as trustworthy as its shakiest
        # component -- averaging would let three confident harmless answers hide
        # a coin-flip on whether the action can be undone.
        confidence = min(
            blast.confidence,
            unrecoverable.confidence,
            explicit.confidence,
            target.confidence,
        )

        return RiskAssessment(
            blast_radius=blast.score,
            unrecoverable=answers.noul(Q.Q_UNRECOVERABLE),
            explicitly_requested=answers.noul(Q.Q_EXPLICITLY_REQUESTED),
            target_confidence=target.choice,
            confidence=confidence,
            synthetic=answers.synthetic,
        )
