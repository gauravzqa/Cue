"""Semantic tool activation: top tool plus its photo finishes, never more than five."""

from __future__ import annotations

import pytest

from daa.contracts import Choice, RiskTier, ToolSpec
from daa.jev import questions as Q
from daa.jev.client import FakeJev, JevUnavailable
from daa.jev.router import ToolRouter


def spec(name: str, hint: str = "", description: str = "") -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description or f"{name} description",
        params={},
        floor=RiskTier.ANNOUNCE,
        activation_hint=hint,
    )


TRASH = spec("trash_files", "the user wants files removed or cleaned up")
SEARCH = spec("search_files", "the user wants to find or locate something")
OPEN = spec("open_app", "the user wants an application launched or focused")
SPECS = [TRASH, SEARCH, OPEN]


def route(probabilities, specs=SPECS, **kwargs) -> tuple[list[ToolSpec], FakeJev]:
    fake = FakeJev({Q.Q_TOOL: probabilities})
    return ToolRouter(fake, **kwargs).activate("do a thing", specs), fake


def names(result) -> list[str]:
    return [s.name for s in result]


def test_the_choice_offers_every_tool_plus_an_explicit_none():
    _, fake = route({"trash_files": 0.9, "search_files": 0.05, "open_app": 0.03, "none": 0.02})
    _, questions, _ = fake.calls[0]
    question = questions[Q.Q_TOOL]
    assert isinstance(question, Choice)
    assert set(question.criteria) == {"trash_files", "search_files", "open_app", "none"}


def test_the_criteria_use_the_activation_hint_not_the_description():
    _, fake = route({"trash_files": 1.0})
    criteria = fake.calls[0][1][Q.Q_TOOL].criteria
    assert criteria["trash_files"] == TRASH.activation_hint


def test_a_tool_without_a_hint_falls_back_to_its_description():
    unhinted = spec("quit_app", hint="", description="quits a running application")
    _, fake = route({"quit_app": 1.0}, specs=[unhinted])
    assert fake.calls[0][1][Q.Q_TOOL].criteria["quit_app"] == "quits a running application"


def test_a_clear_winner_activates_alone():
    result, _ = route({"trash_files": 0.92, "search_files": 0.04, "open_app": 0.02, "none": 0.02})
    assert names(result) == ["trash_files"]


def test_a_near_tie_activates_both_candidates():
    """Activating two tools costs tokens; activating the wrong one costs the turn."""
    result, _ = route({"search_files": 0.45, "trash_files": 0.38, "open_app": 0.10, "none": 0.07})
    assert names(result) == ["search_files", "trash_files"]


@pytest.mark.parametrize(
    "runner_up, expected",
    [
        (0.35, ["a", "b"]),   # exactly on the 0.15 margin: inside
        (0.34, ["a"]),        # a hair outside
        (0.50, ["a", "b"]),
    ],
)
def test_the_margin_boundary_is_inclusive(runner_up, expected):
    a, b = spec("a"), spec("b")
    result, _ = route({"a": 0.5, "b": runner_up, "none": 0.0}, specs=[a, b])
    assert names(result) == expected


def test_the_margin_is_configurable():
    a, b = spec("a"), spec("b")
    probs = {"a": 0.5, "b": 0.3, "none": 0.2}
    assert names(route(probs, specs=[a, b], margin=0.15)[0]) == ["a"]
    assert names(route(probs, specs=[a, b], margin=0.25)[0]) == ["a", "b"]


def test_none_winning_outright_activates_nothing():
    result, _ = route({"none": 0.8, "trash_files": 0.1, "search_files": 0.06, "open_app": 0.04})
    assert result == []


def test_none_inside_the_margin_does_not_suppress_the_tools():
    """Torn between 'no tool' and 'this tool' still means offering the tool."""
    result, _ = route({"trash_files": 0.45, "none": 0.40, "search_files": 0.1, "open_app": 0.05})
    assert names(result) == ["trash_files"]


def test_never_returns_more_than_five_tools():
    wide = [spec(f"t{i}") for i in range(9)]
    flat = {s.name: 1.0 / 9 for s in wide}
    flat["none"] = 0.0
    result, _ = route(flat, specs=wide, margin=1.0)
    assert len(result) == 5


def test_max_active_cannot_be_raised_above_the_ceiling():
    wide = [spec(f"t{i}") for i in range(9)]
    flat = {s.name: 1.0 / 9 for s in wide}
    flat["none"] = 0.0
    result, _ = route(flat, specs=wide, margin=1.0, max_active=50)
    assert len(result) == 5


def test_results_are_ordered_most_likely_first():
    result, _ = route({"open_app": 0.4, "trash_files": 0.35, "search_files": 0.3, "none": 0.0},
                      margin=0.2)
    assert names(result) == ["open_app", "trash_files", "search_files"]


def test_an_empty_registry_never_calls_jev():
    fake = FakeJev()
    assert ToolRouter(fake).activate("anything", []) == []
    assert fake.calls == []


def test_a_jev_outage_activates_nothing():
    """Degrading to 'I can't do that' beats picking a destructive tool blind."""
    router = ToolRouter(FakeJev(fail=JevUnavailable("down")))
    assert router.activate("delete my home folder", SPECS) == []


def test_the_router_makes_exactly_one_call():
    _, fake = route({"trash_files": 1.0})
    assert len(fake.calls) == 1


def test_the_state_carries_the_utterance_and_context():
    fake = FakeJev({Q.Q_TOOL: {"trash_files": 1.0}})
    ToolRouter(fake).activate("bin those screenshots", SPECS, {"front_app": "Finder"})
    state, _, _ = fake.calls[0]
    assert state["utterance"] == "bin those screenshots"
    assert state["context"] == {"front_app": "Finder"}
