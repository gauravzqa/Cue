"""Provider seam: translation both ways, canned answers, and failure collapse."""

from __future__ import annotations

import json

import pytest
import typesafe_sdk as ts

from daa.config import Settings
from daa.contracts import Choice, ChoiceAnswer, Noul, Score
from daa.jev.client import (
    FakeJev,
    JevUnavailable,
    RealJev,
    ReplayJev,
    build_provider,
    jsonable,
)

NOUL = Noul(instructions="is it so", criteria="the thing is so")
CHOICE = Choice(instructions="which", criteria={"a": "an a", "b": "a b", "c": None})
SCORE = Score(instructions="how far", criteria=["none", "some", "lots", "all"])


class StubClient:
    """Stands in for TypeSafeClient. Records what RealJev sent, returns what we say."""

    def __init__(self, response=None, raises: BaseException | None = None) -> None:
        self.response = response
        self.raises = raises
        self.calls: list[dict] = []

    def system_one(self, state, questions, **kwargs):
        self.calls.append({"state": state, "questions": questions, "kwargs": kwargs})
        if self.raises is not None:
            raise self.raises
        return self.response


def _response(answers):
    return ts.SystemOneResponse(model="m", usage=ts.Usage(input_tokens=1), answers=answers)


# --- build_provider -------------------------------------------------------


def test_build_provider_is_fake_without_a_key():
    assert isinstance(build_provider(Settings()), FakeJev)


def test_build_provider_is_real_with_a_key():
    assert isinstance(build_provider(Settings(typesafe_api_key="sk-test")), RealJev)


# --- FakeJev --------------------------------------------------------------


def test_fake_defaults_sit_on_the_fence():
    """An unseeded question must not accidentally satisfy any threshold."""
    answers = FakeJev().ask({}, {"n": NOUL, "c": CHOICE, "s": SCORE})
    assert answers.noul("n") == 0.5
    assert answers.answers["n"].confidence == 0.0
    assert answers.choice("c").confidence == 0.5
    assert answers.score("s").score == pytest.approx(1.5)  # midpoint of 4 levels
    assert answers.synthetic is True


def test_fake_default_choice_probabilities_are_uniform_and_normalised():
    c = FakeJev().ask({}, {"c": CHOICE}).choice("c")
    assert set(c.probabilities) == {"a", "b", "c"}
    assert sum(c.probabilities.values()) == pytest.approx(1.0)
    assert c.choice == "a"  # first declared option, deterministically


def test_fake_seeds_scalars_by_question_type():
    fake = FakeJev({"n": 0.93, "c": "b", "s": 2.25})
    a = fake.ask({}, {"n": NOUL, "c": CHOICE, "s": SCORE})
    assert a.noul("n") == pytest.approx(0.93)
    assert a.choice("c").choice == "b"
    assert a.score("s").score == pytest.approx(2.25)
    # A continuous score spreads over the two levels it falls between.
    assert a.score("s").probabilities[2] == pytest.approx(0.75)
    assert a.score("s").probabilities[3] == pytest.approx(0.25)
    assert sum(a.score("s").probabilities) == pytest.approx(1.0)


def test_fake_accepts_a_full_answer_object_for_exact_control():
    canned = ChoiceAnswer(choice="a", probabilities={"a": 0.4, "b": 0.35, "c": 0.25}, confidence=0.4)
    a = FakeJev({"c": canned}).ask({}, {"c": CHOICE})
    assert a.choice("c") is canned


def test_fake_accepts_a_bare_probability_map_for_a_choice():
    a = FakeJev({"c": {"a": 0.2, "b": 0.7, "c": 0.1}}).ask({}, {"c": CHOICE})
    assert a.choice("c").choice == "b"


def test_fake_rejects_a_mistyped_canned_answer():
    """A fixture that does not fit its question must fail loudly, not default."""
    with pytest.raises(JevUnavailable):
        FakeJev({"n": "yes"}).ask({}, {"n": NOUL})
    with pytest.raises(JevUnavailable):
        FakeJev({"c": "not-an-option"}).ask({}, {"c": CHOICE})


def test_fake_records_every_call_for_call_count_assertions():
    fake = FakeJev()
    fake.ask({"u": "hi"}, {"n": NOUL}, timeout_s=0.5)
    assert len(fake.calls) == 1
    state, questions, timeout = fake.calls[0]
    assert state == {"u": "hi"} and timeout == 0.5 and set(questions) == {"n"}


def test_fake_is_deterministic_across_identical_calls():
    fake = FakeJev({"n": 0.7})
    first = fake.ask({"u": "x"}, {"n": NOUL, "c": CHOICE})
    second = fake.ask({"u": "x"}, {"n": NOUL, "c": CHOICE})
    assert first.answers == second.answers
    assert first.latency_ms == second.latency_ms


def test_fake_can_be_told_to_fail():
    fake = FakeJev(fail=JevUnavailable("down"))
    with pytest.raises(JevUnavailable):
        fake.ask({}, {"n": NOUL})


# --- RealJev: daa -> SDK --------------------------------------------------


def test_real_translates_daa_questions_into_sdk_objects():
    stub = StubClient(_response({"n": ts.NoulAnswer(noul=0.8)}))
    RealJev(stub).ask({"u": "hi"}, {"n": NOUL})
    sent = stub.calls[0]["questions"]["n"]
    assert isinstance(sent, ts.Noul)
    # daa models noul criteria as one sentence; the SDK wants a {true,false} pair.
    assert sent.criteria == {"true": "the thing is so"}
    assert sent.instructions == NOUL.instructions


def test_real_omits_noul_criteria_when_daa_has_none():
    stub = StubClient(_response({"n": ts.NoulAnswer(noul=0.1)}))
    RealJev(stub).ask({}, {"n": Noul(instructions="bare")})
    assert stub.calls[0]["questions"]["n"].criteria is None


def test_real_translates_choice_and_score_questions():
    stub = StubClient(
        _response(
            {
                "c": ts.ChoiceAnswer(choice="a", confidence=0.6, probabilities={"a": 0.6, "b": 0.4}),
                "s": ts.ScoreAnswer(
                    score=1.0, confidence=0.5, legend={0: "none"}, probabilities={0: 1.0}
                ),
            }
        )
    )
    RealJev(stub).ask({}, {"c": CHOICE, "s": SCORE})
    sent = stub.calls[0]["questions"]
    assert isinstance(sent["c"], ts.Choice) and sent["c"].criteria == dict(CHOICE.criteria)
    assert isinstance(sent["s"], ts.Score) and sent["s"].criteria == list(SCORE.criteria)


def test_real_serialises_unjsonable_state():
    from pathlib import Path

    stub = StubClient(_response({"n": ts.NoulAnswer(noul=0.5)}))
    RealJev(stub).ask({"path": Path("/tmp/x"), "n": {1: (2, 3)}}, {"n": NOUL})
    state = stub.calls[0]["state"]
    json.dumps(state)  # must not raise
    assert state["path"] == "/tmp/x"
    assert state["n"] == {"1": [2, 3]}


def test_real_passes_the_timeout_through():
    stub = StubClient(_response({"n": ts.NoulAnswer(noul=0.5)}))
    RealJev(stub).ask({}, {"n": NOUL}, timeout_s=0.25)
    assert stub.calls[0]["kwargs"]["timeout"] == 0.25


# --- RealJev: SDK -> daa --------------------------------------------------


def test_real_orders_the_score_legend_by_level_not_dict_order():
    """The SDK hands back int-keyed dicts; daa's ScoreAnswer is positional."""
    stub = StubClient(
        _response(
            {
                "s": ts.ScoreAnswer(
                    score=2.4,
                    confidence=0.77,
                    legend={2: "lots", 0: "none", 3: "all", 1: "some"},
                    probabilities={3: 0.4, 0: 0.05, 2: 0.5, 1: 0.05},
                )
            }
        )
    )
    s = RealJev(stub).ask({}, {"s": SCORE}).score("s")
    assert list(s.legend) == ["none", "some", "lots", "all"]
    assert [round(p, 2) for p in s.probabilities] == [0.05, 0.05, 0.5, 0.4]
    assert s.score == pytest.approx(2.4) and s.confidence == pytest.approx(0.77)


def test_real_falls_back_to_asked_levels_when_the_legend_is_missing():
    stub = StubClient(
        _response(
            {
                "s": ts.ScoreAnswer(
                    score=1.0, confidence=0.5, legend={}, probabilities={0: 0.3, 1: 0.7}
                )
            }
        )
    )
    s = RealJev(stub).ask({}, {"s": SCORE}).score("s")
    assert list(s.legend) == ["none", "some"]


def test_real_marks_answers_live_and_measures_latency():
    stub = StubClient(_response({"n": ts.NoulAnswer(noul=0.42)}))
    a = RealJev(stub).ask({}, {"n": NOUL})
    assert a.synthetic is False
    assert a.latency_ms >= 0.0
    assert a.noul("n") == pytest.approx(0.42)


# --- RealJev: everything that can go wrong becomes JevUnavailable ---------


@pytest.mark.parametrize(
    "exc",
    [
        ts.TypeSafeAPIConnectionError("no route to host"),
        ts.TypeSafeAPITimeoutError(1.0),
        ts.TypeSafeError("something else"),
        RuntimeError("sdk churned under us"),
    ],
)
def test_real_collapses_every_failure_into_jev_unavailable(exc):
    with pytest.raises(JevUnavailable):
        RealJev(StubClient(raises=exc)).ask({}, {"n": NOUL})


def test_real_chains_the_original_cause():
    original = ts.TypeSafeAPITimeoutError(1.0)
    with pytest.raises(JevUnavailable) as caught:
        RealJev(StubClient(raises=original)).ask({}, {"n": NOUL})
    assert caught.value.__cause__ is original


def test_real_rejects_an_answer_of_the_wrong_type():
    """Jev cannot emit a type error, so a mismatch means we are talking to
    something that is not Jev -- which is a failure, not a value."""
    stub = StubClient(
        _response({"n": ts.ChoiceAnswer(choice="a", confidence=1.0, probabilities={"a": 1.0})})
    )
    with pytest.raises(JevUnavailable):
        RealJev(stub).ask({}, {"n": NOUL})


def test_real_rejects_a_missing_answer():
    stub = StubClient(_response({"n": ts.NoulAnswer(noul=0.5)}))
    with pytest.raises(JevUnavailable):
        RealJev(stub).ask({}, {"n": NOUL, "other": NOUL})


def test_real_refuses_an_empty_question_set():
    stub = StubClient(_response({}))
    with pytest.raises(JevUnavailable):
        RealJev(stub).ask({}, {})
    assert stub.calls == []  # never left the process


# --- ReplayJev ------------------------------------------------------------


def _fixture(tmp_path, records):
    path = tmp_path / "run.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def test_replay_plays_records_back_in_order(tmp_path):
    path = _fixture(
        tmp_path,
        [
            {"answers": {"n": 0.9}, "latency_ms": 120.0},
            {"answers": {"n": 0.1}, "latency_ms": 90.0},
        ],
    )
    replay = ReplayJev.from_path(path)
    first = replay.ask({}, {"n": NOUL})
    second = replay.ask({}, {"n": NOUL})
    assert (first.noul("n"), first.latency_ms) == (pytest.approx(0.9), 120.0)
    assert second.noul("n") == pytest.approx(0.1)
    assert first.synthetic is True and second.synthetic is True


def test_replay_raises_when_the_fixture_runs_out(tmp_path):
    replay = ReplayJev.from_path(_fixture(tmp_path, [{"answers": {"n": 0.9}}]))
    replay.ask({}, {"n": NOUL})
    assert replay.remaining == 0
    with pytest.raises(JevUnavailable):
        replay.ask({}, {"n": NOUL})


def test_replay_is_strict_about_questions_the_recording_never_saw(tmp_path):
    replay = ReplayJev.from_path(_fixture(tmp_path, [{"answers": {"n": 0.9}}]))
    with pytest.raises(JevUnavailable):
        replay.ask({}, {"n": NOUL, "added_later": NOUL})


def test_replay_can_be_lenient_about_new_questions(tmp_path):
    replay = ReplayJev.from_path(
        _fixture(tmp_path, [{"answers": {"n": 0.9}}]), strict=False
    )
    a = replay.ask({}, {"n": NOUL, "added_later": NOUL})
    assert a.noul("n") == pytest.approx(0.9)
    assert a.noul("added_later") == 0.5  # defaulted to maximum uncertainty


def test_replay_reads_recorded_score_distributions(tmp_path):
    path = _fixture(
        tmp_path,
        [{"answers": {"s": {"type": "score", "score": 2.4, "confidence": 0.8,
                            "probabilities": {"0": 0.0, "1": 0.1, "2": 0.5, "3": 0.4}}}}],
    )
    s = ReplayJev.from_path(path).ask({}, {"s": SCORE}).score("s")
    assert s.score == pytest.approx(2.4)
    assert s.confidence == pytest.approx(0.8)
    assert [round(p, 2) for p in s.probabilities] == [0.0, 0.1, 0.5, 0.4]


# --- jsonable -------------------------------------------------------------


def test_jsonable_leaves_primitives_alone_and_stringifies_the_rest():
    from pathlib import Path

    assert jsonable({"a": 1, "b": True, "c": None, "d": 1.5}) == {
        "a": 1, "b": True, "c": None, "d": 1.5
    }
    assert jsonable([Path("/x"), {"k": Path("/y")}]) == ["/x", {"k": "/y"}]
