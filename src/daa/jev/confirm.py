"""Did the user just say yes?

Never by string matching. "Yeah no" is a refusal, "sure but not that one" is a
refusal, "no, the other one" is a correction, and every one of them contains a
token that a keyword parser would act on. The only safe reading of spoken
consent is a semantic one, judged against WHAT WAS PROPOSED -- which is why the
pending ResolvedAction goes into the state alongside the reply.
"""

from __future__ import annotations

from typing import Literal

from daa.config import Settings
from daa.contracts import JevProvider, ResolvedAction
from daa.jev import questions as Q
from daa.jev.client import JevUnavailable

Verdict = Literal["yes", "no", "unclear"]


class ConfirmParser:
    def __init__(self, provider: JevProvider, settings: Settings) -> None:
        self._provider = provider
        self._settings = settings

    def interpret(self, reply: str, pending: ResolvedAction) -> Verdict:
        """Three-way on purpose. "unclear" is an answer, and the caller re-asks.

        The band between `confirm_no` and `confirm_yes` is deliberately wide.
        Collapsing it into a binary forces every ambiguous reply to become
        either an unauthorised action or a silent refusal; re-asking costs one
        sentence and is the only option that cannot be wrong.
        """
        if not reply.strip():
            # Silence is not consent, and it is not a refusal either.
            return "unclear"

        state = {
            "reply": reply,
            "proposed_action": {
                "tool": pending.tool,
                "spoken_description": pending.describe(),
                "targets": [str(t) for t in pending.targets],
            },
            "assistant_asked": f"Should I {pending.describe()}?",
        }

        try:
            answers = self._provider.ask(state, Q.consent_questions(), timeout_s=1.5)
        except JevUnavailable:
            # Fail closed: an unreadable reply to "shall I delete these?" is
            # never a yes.
            return "unclear"

        p = answers.noul(Q.Q_CONSENT)
        # Both thresholds are inclusive on their decisive side, so the three
        # verdicts tile the whole 0..1 interval with no unreachable value.
        if p >= self._settings.confirm_yes:
            return "yes"
        if p <= self._settings.confirm_no:
            return "no"
        return "unclear"
