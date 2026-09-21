"""The always-on wake gate: one call, hard thresholds, fail closed."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from daa.config import Settings
from daa.contracts import Noul
from daa.jev import questions as Q
from daa.jev.client import FakeJev, JevUnavailable
from daa.jev.gate import AddressGate, WakeDecision

SETTINGS = Settings()  # address_gate 0.42 (calibrated, see evals/), end_of_turn 0.75


def gate(*, settings=None, **canned) -> tuple[AddressGate, FakeJev]:
    fake = FakeJev(canned)
    return AddressGate(fake, settings or SETTINGS), fake


def test_the_gate_makes_exactly_one_jev_call():
    """This runs on every utterance in a room. Two calls is a latency bug."""
    g, fake = gate(addressed=0.9, end_of_turn=0.9, needs_planner=0.1)
    g.should_wake("hey, what time is it", {})
    assert len(fake.calls) == 1


def test_the_single_call_batches_all_three_questions():
    g, fake = gate()
    g.should_wake("hey", {})
    _, questions, _ = fake.calls[0]
    assert set(questions) == {Q.Q_ADDRESSED, Q.Q_END_OF_TURN, Q.Q_NEEDS_PLANNER}
    assert all(isinstance(q, Noul) for q in questions.values())


def test_the_state_carries_the_transcript_and_the_caller_context():
    g, fake = gate()
    g.should_wake("open my notes", {"last_speaker": "user", "app": "Safari"})
    state, _, _ = fake.calls[0]
    assert state["utterance"] == "open my notes"
    assert state["context"] == {"last_speaker": "user", "app": "Safari"}


@pytest.mark.parametrize(
    "p, expected",
    [
        (0.84999, False),   # just under the gate (threshold pinned below)
        (0.85, True),       # exactly on it: the threshold means "at least this"
        (0.85001, True),
        (0.0, False),
        (1.0, True),
    ],
)
def test_wake_thresholds_exactly_on_address_gate(p, expected):
    # Threshold pinned, NOT read from the default: evals/ are expected to keep
    # moving that default, and this test is about the >= comparison, not the
    # value. Riding the default made this test fail the first time the gate
    # was actually calibrated, which is the wrong signal from the wrong test.
    from dataclasses import replace

    g, _ = gate(addressed=p, settings=replace(SETTINGS, address_gate=0.85))
    assert g.should_wake("do the thing", {}).wake is expected


@pytest.mark.parametrize(
    "p, expected",
    [(0.74999, False), (0.75, True), (0.9, True)],
)
def test_end_of_turn_thresholds_exactly_on_its_own_setting(p, expected):
    g, _ = gate(addressed=0.99, end_of_turn=p)
    assert g.should_wake("so I was thinking we should", {}).end_of_turn is expected


@pytest.mark.parametrize("p, expected", [(0.59999, False), (0.6, True), (0.95, True)])
def test_needs_planner_thresholds_exactly_on_its_own_setting(p, expected):
    g, _ = gate(addressed=0.99, needs_planner=p)
    assert g.should_wake("tidy up my desktop", {}).needs_planner is expected


def test_wake_and_end_of_turn_are_independent():
    """An addressed but unfinished sentence must still wake, so the loop keeps
    listening instead of discarding it as not-for-me."""
    g, _ = gate(addressed=0.97, end_of_turn=0.2)
    d = g.should_wake("could you move all the", {})
    assert d.wake is True and d.end_of_turn is False


def test_custom_thresholds_are_honoured():
    fake = FakeJev({"addressed": 0.5})
    lenient = AddressGate(fake, Settings(address_gate=0.4))
    strict = AddressGate(fake, Settings(address_gate=0.6))
    assert lenient.should_wake("hey", {}).wake is True
    assert strict.should_wake("hey", {}).wake is False


def test_the_raw_probability_and_provenance_come_back_for_the_audit_log():
    g, _ = gate(addressed=0.91)
    d = g.should_wake("hey", {})
    assert d.addressed_p == pytest.approx(0.91)
    assert d.synthetic is True
    assert d.latency_ms == 0.0


def test_silence_never_wakes_and_never_costs_a_call():
    g, fake = gate(addressed=0.99)
    d = g.should_wake("   ", {})
    assert d.wake is False
    assert fake.calls == []


def test_a_jev_outage_fails_closed():
    """No judgment must never mean 'assume it was for me'."""
    g = AddressGate(FakeJev(fail=JevUnavailable("down")), SETTINGS)
    d = g.should_wake("delete everything", {})
    assert d == WakeDecision(
        wake=False,
        end_of_turn=False,
        needs_planner=False,
        addressed_p=0.0,
        latency_ms=0.0,
        synthetic=True,
    )


def test_an_unseeded_provider_does_not_wake_a_hot_mic():
    """A fake judge may not open an always-on mic.

    This test used to read "0.5 sits below every wake threshold". That stopped
    being true the moment address_gate was calibrated against evals/ and moved
    to 0.42: FakeJev's unseeded 0.5 means "I don't know", and "I don't know" is
    now above the bar. Without the guard in gate.py, a machine with no
    TYPESAFE_API_KEY running always-on would wake on every sound in the room.
    """
    from dataclasses import replace

    g, _ = gate()
    hot = replace(g._settings, always_on=True)
    g._settings = hot
    assert g.should_wake("something", {}).wake is False


def test_push_to_talk_is_unaffected_by_the_hot_mic_guard():
    """Only always-on is gated on a real judgment.

    In push-to-talk the user pressing a key IS the address signal, so there is
    no judgment to distrust; refusing to wake there would break the keyless
    dev loop for no safety gain.
    """
    g, _ = gate(addressed=0.99)
    assert g.should_wake("open safari", {}).wake is True


def test_the_wake_decision_is_frozen():
    g, _ = gate(addressed=0.99)
    d = g.should_wake("hey", {})
    with pytest.raises(FrozenInstanceError):
        d.wake = False  # type: ignore[misc]
