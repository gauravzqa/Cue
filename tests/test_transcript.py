"""The rolling window.

These tests exist because the failure mode is invisible: an unbounded
transcript works perfectly in every manual test and then, four hours into a
session, every address-gate call is paying for a 40k-token state. So the bound
is asserted directly, including the awkward edges (one huge turn, exactly at
the limit, the newest turn is never dropped).
"""

from __future__ import annotations

from daa.voice.transcript import Transcript, Turn


def test_turns_are_bounded_by_count():
    t = Transcript(max_turns=3)
    for i in range(10):
        t.add_user(f"utterance {i}")
    assert len(t) == 3
    assert [turn.text for turn in t.turns] == ["utterance 7", "utterance 8", "utterance 9"]
    # Eviction is not a dropped write: the counter proves the window rolled.
    assert t.total_turns == 10


def test_turns_are_bounded_by_characters_too():
    t = Transcript(max_turns=100, max_chars=50)
    for i in range(20):
        t.add_user("x" * 20)
    assert t.char_count <= 50
    assert len(t) <= 3


def test_the_newest_turn_is_never_dropped_even_if_it_alone_busts_the_budget():
    t = Transcript(max_turns=10, max_chars=10, max_turn_chars=1_000)
    t.add_user("short")
    t.add_user("y" * 500)
    assert len(t) == 1
    assert t.last_user.startswith("y")


def test_a_single_enormous_turn_is_clipped_in_the_middle():
    t = Transcript(max_turn_chars=21)
    t.add_user("START" + "z" * 400 + "END")
    text = t.last_user
    assert len(text) <= 21
    # Both the instruction and the correction survive.
    assert text.startswith("START")
    assert text.endswith("END")
    assert "…" in text


def test_whitespace_is_stripped():
    t = Transcript()
    assert t.add_user("  hello  ").text == "hello"


def test_last_user_skips_assistant_turns():
    t = Transcript()
    t.add_user("move them")
    t.add_assistant("Moved three files.")
    assert t.last_user == "move them"


def test_last_user_is_none_when_empty():
    assert Transcript().last_user is None


def test_recent_returns_plain_dicts_for_the_risk_gate():
    t = Transcript()
    t.add_user("a")
    t.add_assistant("b")
    assert t.recent() == [{"role": "user", "text": "a"}, {"role": "assistant", "text": "b"}]
    assert t.recent(1) == [{"role": "assistant", "text": "b"}]


def test_as_state_is_json_safe_and_small():
    import json

    t = Transcript(max_turns=4)
    for i in range(20):
        t.add_user(f"turn {i}")
    state = t.as_state(pending_action="move 3 files")
    blob = json.dumps(state)
    assert len(blob) < 500
    assert state["turns_total"] == 20
    assert state["pending_action"] == "move 3 files"


def test_as_state_drops_none_extras():
    # None means "not applicable", and sending it just makes the prompt bigger.
    state = Transcript().as_state(pending_action=None, speaking=False)
    assert "pending_action" not in state
    assert state["speaking"] is False


def test_messages_put_the_system_prompt_first_for_prefix_caching():
    t = Transcript()
    t.add_user("hi")
    t.add_assistant("hello")
    messages = t.messages(system="SYSTEM")
    assert messages[0] == {"role": "system", "content": "SYSTEM"}
    assert [m["role"] for m in messages] == ["system", "user", "assistant"]


def test_messages_without_a_system_prompt():
    t = Transcript()
    t.add_user("hi")
    assert t.messages() == [{"role": "user", "content": "hi"}]


def test_clear_empties_the_window_but_not_the_counter():
    t = Transcript()
    t.add_user("a")
    t.clear()
    assert len(t) == 0
    assert t.total_turns == 1


def test_turn_as_dict():
    assert Turn(role="user", text="x").as_dict() == {"role": "user", "text": "x"}


def test_iteration_yields_turns_in_order():
    t = Transcript()
    t.add_user("one")
    t.add_assistant("two")
    assert [turn.text for turn in t] == ["one", "two"]
