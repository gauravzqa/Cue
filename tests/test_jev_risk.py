"""The risk gate: resolved targets in, minimum confidence out, fail closed."""

from __future__ import annotations

import pytest

from daa.config import Settings
from daa.contracts import Choice, ChoiceAnswer, NoulAnswer, ResolvedAction, Score, ScoreAnswer
from daa.jev import questions as Q
from daa.jev.client import FakeJev, JevUnavailable
from daa.jev.risk import FAILED_CLOSED, RiskGate

SETTINGS = Settings()

ACTION = ResolvedAction(
    tool="trash_files",
    args={"paths": ["/Users/x/Desktop/Screenshot 2026-09-19.png"], "recursive": False},
    targets=("Screenshot 2026-09-19.png", "Screenshot 2026-09-20.png"),
    explicit=False,
)


def assess(canned=None, action=ACTION, utterance="get rid of the old ones", history=()):
    fake = FakeJev(canned or {})
    return RiskGate(fake, SETTINGS).assess(action, utterance, history), fake


# --- the batched call -----------------------------------------------------


def test_the_risk_gate_makes_exactly_one_call_with_all_four_questions():
    _, fake = assess()
    assert len(fake.calls) == 1
    _, questions, _ = fake.calls[0]
    assert set(questions) == {
        Q.Q_BLAST_RADIUS,
        Q.Q_UNRECOVERABLE,
        Q.Q_EXPLICITLY_REQUESTED,
        Q.Q_TARGET_CONFIDENCE,
    }


def test_blast_radius_uses_exactly_the_agreed_four_level_spectrum():
    _, fake = assess()
    question = fake.calls[0][1][Q.Q_BLAST_RADIUS]
    assert isinstance(question, Score)
    assert list(question.criteria) == [
        "read-only",
        "trivially undoable",
        "recoverable with effort",
        "irreversible or externally visible",
    ]


def test_target_confidence_is_a_choice_over_certain_probable_guessing():
    _, fake = assess()
    question = fake.calls[0][1][Q.Q_TARGET_CONFIDENCE]
    assert isinstance(question, Choice)
    assert set(question.criteria) == {"certain", "probable", "guessing"}


# --- the state must describe the RESOLVED action --------------------------


def test_the_state_carries_the_resolved_targets_not_just_the_utterance():
    """Assessing 'get rid of the old ones' instead of the two named files is
    exactly how the wrong thing gets deleted."""
    _, fake = assess()
    state, _, _ = fake.calls[0]
    assert state["action"]["targets"] == [
        "Screenshot 2026-09-19.png",
        "Screenshot 2026-09-20.png",
    ]
    assert state["action"]["target_count"] == 2
    assert state["action"]["tool"] == "trash_files"
    assert state["action"]["spoken_description"] == ACTION.describe()
    assert state["action"]["arguments"]["paths"] == [
        "/Users/x/Desktop/Screenshot 2026-09-19.png"
    ]
    assert state["action"]["user_named_the_target"] is False
    assert state["utterance"] == "get rid of the old ones"


def test_recent_history_is_included_but_bounded():
    _, fake = assess(history=[f"turn {i}" for i in range(20)])
    turns = fake.calls[0][0]["recent_turns"]
    assert turns == [f"turn {i}" for i in range(14, 20)]


def test_state_survives_arguments_that_are_not_json():
    from pathlib import Path

    action = ResolvedAction(tool="t", args={"p": Path("/tmp/x")}, targets=("x",))
    _, fake = assess(action=action)
    assert fake.calls[0][0]["action"]["arguments"] == {"p": "/tmp/x"}


# --- answer translation ---------------------------------------------------


def test_the_assessment_reports_what_jev_said():
    a, _ = assess(
        {
            Q.Q_BLAST_RADIUS: ScoreAnswer(
                score=2.4,
                legend=tuple(Q.BLAST_RADIUS_LEVELS),
                probabilities=(0.0, 0.1, 0.5, 0.4),
                confidence=0.82,
            ),
            Q.Q_UNRECOVERABLE: 0.2,
            Q.Q_EXPLICITLY_REQUESTED: 0.95,
            Q.Q_TARGET_CONFIDENCE: "probable",
        }
    )
    assert a.blast_radius == pytest.approx(2.4)
    assert a.unrecoverable == pytest.approx(0.2)
    assert a.explicitly_requested == pytest.approx(0.95)
    assert a.target_confidence == "probable"
    assert a.synthetic is True


def test_blast_radius_is_continuous_not_bucketed():
    a, _ = assess({Q.Q_BLAST_RADIUS: 1.75})
    assert a.blast_radius == pytest.approx(1.75)


# --- confidence is a MINIMUM ---------------------------------------------


def test_confidence_is_the_minimum_across_contributing_answers():
    a, _ = assess(
        {
            Q.Q_BLAST_RADIUS: ScoreAnswer(
                score=1.0, legend=tuple(Q.BLAST_RADIUS_LEVELS),
                probabilities=(0.0, 1.0, 0.0, 0.0), confidence=0.9,
            ),
            # noul confidence is derived: |0.8 - 0.5| * 2 == 0.6
            Q.Q_UNRECOVERABLE: NoulAnswer(noul=0.8),
            Q.Q_EXPLICITLY_REQUESTED: NoulAnswer(noul=0.99),
            Q.Q_TARGET_CONFIDENCE: ChoiceAnswer(
                choice="certain", probabilities={"certain": 0.95}, confidence=0.95
            ),
        }
    )
    assert a.confidence == pytest.approx(0.6)


def test_one_coin_flip_answer_drags_the_whole_assessment_down():
    """A mean would let three confident answers hide a 50/50 on reversibility."""
    a, _ = assess(
        {
            Q.Q_BLAST_RADIUS: ScoreAnswer(
                score=3.0, legend=tuple(Q.BLAST_RADIUS_LEVELS),
                probabilities=(0.0, 0.0, 0.0, 1.0), confidence=0.99,
            ),
            Q.Q_UNRECOVERABLE: NoulAnswer(noul=0.5),  # derived confidence 0.0
            Q.Q_EXPLICITLY_REQUESTED: NoulAnswer(noul=1.0),
            Q.Q_TARGET_CONFIDENCE: ChoiceAnswer(
                choice="certain", probabilities={"certain": 1.0}, confidence=1.0
            ),
        }
    )
    assert a.confidence == 0.0


def test_a_score_can_be_the_weakest_link_too():
    a, _ = assess(
        {
            Q.Q_BLAST_RADIUS: ScoreAnswer(
                score=2.0, legend=tuple(Q.BLAST_RADIUS_LEVELS),
                probabilities=(0.0, 0.0, 1.0, 0.0), confidence=0.11,
            ),
            Q.Q_UNRECOVERABLE: NoulAnswer(noul=1.0),
            Q.Q_EXPLICITLY_REQUESTED: NoulAnswer(noul=0.0),
            Q.Q_TARGET_CONFIDENCE: ChoiceAnswer(
                choice="guessing", probabilities={"guessing": 1.0}, confidence=1.0
            ),
        }
    )
    assert a.confidence == pytest.approx(0.11)


# --- fail closed ----------------------------------------------------------


def test_a_jev_outage_returns_the_most_pessimistic_assessment():
    gate = RiskGate(FakeJev(fail=JevUnavailable("down")), SETTINGS)
    a = gate.assess(ACTION, "wipe it", ())
    assert a.blast_radius == 3.0
    assert a.confidence == 0.0
    assert a.unrecoverable == 1.0
    assert a.explicitly_requested == 0.0
    assert a.target_confidence == "guessing"
    assert a == FAILED_CLOSED


def test_the_failed_closed_assessment_is_never_logged_as_a_live_judgment():
    gate = RiskGate(FakeJev(fail=JevUnavailable("down")), SETTINGS)
    assert gate.assess(ACTION, "wipe it", ()).synthetic is True


def test_the_failed_closed_blast_radius_is_the_top_of_the_scale():
    assert FAILED_CLOSED.blast_radius == float(len(Q.BLAST_RADIUS_LEVELS) - 1)


def test_an_unseeded_provider_yields_zero_confidence_not_a_confident_pass():
    a, _ = assess()
    assert a.confidence == 0.0
