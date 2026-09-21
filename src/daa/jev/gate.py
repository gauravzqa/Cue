"""Should the assistant wake up at all?

This runs on EVERY utterance in always-on mode, including the 95% of speech in
a room that was never meant for it. It is therefore the only place in daa where
latency is a hard budget rather than a preference, and the reason the three
questions it needs are asked in a single batched call: TypeSafe answers N
questions in one parallel pass, so `addressed` + `end_of_turn` + `needs_planner`
costs what `addressed` alone would.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from daa.config import Settings
from daa.contracts import JevProvider
from daa.jev import questions as Q
from daa.jev.client import JevUnavailable


@dataclass(frozen=True, slots=True)
class WakeDecision:
    wake: bool
    end_of_turn: bool
    needs_planner: bool
    addressed_p: float
    latency_ms: float
    synthetic: bool = False
    # "Stop what you are doing." Rides the SAME single call as everything else
    # -- a revocation that costs a second round trip is a revocation that
    # arrives after the thing it was meant to prevent.
    #
    # Deliberately NOT gated on `wake`. Someone shouting "stop" at a machine
    # that is mid-action is the one case where demanding they first satisfy the
    # address gate is indefensible: the gate exists to stop daa acting on
    # speech that was not for it, and halting is not acting.
    stop: bool = False
    stop_p: float = 0.0


class AddressGate:
    def __init__(self, provider: JevProvider, settings: Settings) -> None:
        self._provider = provider
        self._settings = settings

    def should_wake(self, transcript: str, ctx: Mapping[str, Any]) -> WakeDecision:
        """Exactly one Jev call. Never two, whatever the transcript looks like."""
        if not transcript.strip():
            # Silence is not addressed to anyone. Skipping the call here is not
            # an optimisation, it is refusing to ask a question with no subject.
            return WakeDecision(
                wake=False,
                end_of_turn=False,
                needs_planner=False,
                addressed_p=0.0,
                latency_ms=0.0,
                synthetic=True,
                stop=False,
                stop_p=0.0,
            )

        try:
            answers = self._provider.ask(
                Q.gate_state(transcript, ctx),
                Q.gate_questions(),
                # Tighter than the 2.0s default: a wake decision that arrives
                # after the user has given up and repeated themselves is worse
                # than no wake decision at all.
                timeout_s=1.0,
            )
        except JevUnavailable:
            # Fail closed. An assistant that wakes when it cannot tell whether
            # it was addressed is an assistant that acts on other people's
            # conversations.
            return WakeDecision(
                wake=False,
                end_of_turn=False,
                needs_planner=False,
                addressed_p=0.0,
                latency_ms=0.0,
                synthetic=True,
                stop=False,
                stop_p=0.0,
            )

        addressed_p = answers.noul(Q.Q_ADDRESSED)
        end_p = answers.noul(Q.Q_END_OF_TURN)
        planner_p = answers.noul(Q.Q_NEEDS_PLANNER)
        stop_p = answers.noul(Q.Q_STOP)

        # `wake` and `end_of_turn` are thresholded INDEPENDENTLY and reported
        # separately. They answer different questions -- "is this for me" versus
        # "have they stopped talking" -- and the loop needs both: an addressed
        # utterance that is still mid-sentence must keep the mic open rather
        # than be discarded as not-for-me.
        # A SYNTHETIC judgment may not open a hot mic. FakeJev answers an
        # unseeded noul at 0.5 -- maximum uncertainty -- which used to sit
        # safely below the 0.85 threshold. Calibrating that threshold against
        # evals/ moved it to 0.42, and "I don't know" is now ABOVE the bar: a
        # keyless machine in always-on mode would wake on every sound in the
        # room. Push-to-talk is unaffected, because there the user pressing a
        # key IS the address signal and no judgment is being trusted.
        wake = addressed_p >= self._settings.address_gate
        if wake and answers.synthetic and getattr(self._settings, "always_on", False):
            wake = False

        return WakeDecision(
            wake=wake,
            end_of_turn=end_p >= self._settings.end_of_turn,
            needs_planner=planner_p >= self._settings.needs_planner,
            stop=stop_p >= getattr(self._settings, "stop_p", 0.5),
            stop_p=stop_p,
            addressed_p=addressed_p,
            latency_ms=answers.latency_ms,
            synthetic=answers.synthetic,
        )
