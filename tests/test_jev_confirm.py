"""Spoken consent: three-way, semantic, and never string-matched."""

from __future__ import annotations

import pytest

from daa.config import Settings
from daa.contracts import Noul, ResolvedAction
from daa.jev import questions as Q
from daa.jev.client import FakeJev, JevUnavailable
from daa.jev.confirm import ConfirmParser

SETTINGS = Settings()  # confirm_yes 0.90, confirm_no 0.35

PENDING = ResolvedAction(
    tool="send_message",
    args={"to": "Dana", "body": "on my way"},
    targets=("a message to Dana",),
)


def interpret(p, reply="whatever", settings=SETTINGS, pending=PENDING):
    fake = FakeJev({Q.Q_CONSENT: p} if p is not None else {})
    return ConfirmParser(fake, settings).interpret(reply, pending), fake


# --- thresholds -----------------------------------------------------------


@pytest.mark.parametrize(
    "p, verdict",
    [
        (1.0, "yes"),
        (0.901, "yes"),
        (0.90, "yes"),      # exactly on confirm_yes: inclusive
        (0.899, "unclear"),
        (0.60, "unclear"),
        (0.351, "unclear"),
        (0.35, "no"),       # exactly on confirm_no: inclusive
        (0.349, "no"),
        (0.0, "no"),
    ],
)
def test_the_three_bands_tile_the_whole_interval(p, verdict):
    assert interpret(p)[0] == verdict


def test_the_middle_band_is_unclear_so_the_caller_re_asks():
    """Collapsing the band into a binary makes every ambiguous reply either an
    unauthorised action or a silent refusal."""
    assert interpret(0.5)[0] == "unclear"


def test_thresholds_come_from_settings_not_from_constants():
    lenient = Settings(confirm_yes=0.6, confirm_no=0.2)
    assert interpret(0.7, settings=lenient)[0] == "yes"
    assert interpret(0.7)[0] == "unclear"
    assert interpret(0.25, settings=lenient)[0] == "unclear"
    assert interpret(0.25)[0] == "no"


# --- the hard replies -----------------------------------------------------


@pytest.mark.parametrize(
    "reply, p, verdict",
    [
        # Every one of these contains a token a keyword parser would act on.
        ("yeah no", 0.05, "no"),
        ("sure but not that one", 0.08, "no"),
        ("wait--", 0.4, "unclear"),
        ("no, the other one", 0.12, "no"),
        ("yeah go ahead", 0.97, "yes"),
        ("no yeah do it", 0.94, "yes"),
        ("I guess?", 0.55, "unclear"),
    ],
)
def test_the_verdict_follows_the_probability_not_the_words(reply, p, verdict):
    assert interpret(p, reply=reply)[0] == verdict


def test_identical_words_with_opposite_meanings_get_opposite_verdicts():
    """The proof that nothing here is string matching: same text, both ways."""
    assert interpret(0.97, reply="yeah no")[0] == "yes"
    assert interpret(0.02, reply="yeah no")[0] == "no"


# --- the call itself ------------------------------------------------------


def test_it_asks_one_noul():
    _, fake = interpret(0.95)
    assert len(fake.calls) == 1
    _, questions, _ = fake.calls[0]
    assert list(questions) == [Q.Q_CONSENT]
    assert isinstance(questions[Q.Q_CONSENT], Noul)


def test_the_state_describes_what_was_actually_proposed():
    """'No, the other one' is only readable against the pending action."""
    _, fake = interpret(0.2, reply="no, the other one")
    state, _, _ = fake.calls[0]
    assert state["reply"] == "no, the other one"
    assert state["proposed_action"]["tool"] == "send_message"
    assert state["proposed_action"]["targets"] == ["a message to Dana"]
    assert state["proposed_action"]["spoken_description"] == PENDING.describe()


def test_silence_is_unclear_and_costs_no_call():
    verdict, fake = interpret(0.99, reply="   ")
    assert verdict == "unclear"
    assert fake.calls == []


def test_a_jev_outage_is_never_a_yes():
    parser = ConfirmParser(FakeJev(fail=JevUnavailable("down")), SETTINGS)
    assert parser.interpret("yes absolutely do it", PENDING) == "unclear"


def test_an_unseeded_provider_is_unclear_not_consent():
    assert interpret(None)[0] == "unclear"  # neutral 0.5 lands in the middle band
