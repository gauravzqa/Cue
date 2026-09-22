"""The rolling window.

These tests exist because the failure mode is invisible: an unbounded
transcript works perfectly in every manual test and then, four hours into a
session, every address-gate call is paying for a 40k-token state. So the bound
is asserted directly, including the awkward edges (one huge turn, exactly at
the limit, the newest turn is never dropped).
"""

from __future__ import annotations

from daa.voice.transcript import (
    TOOL_PREFIX,
    Transcript,
    Turn,
    shape_data,
    tool_result_text,
)


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


# ---------------------------------------------------------------------------
# Tool results
#
# The agent loop feeds every step back to a REMOTE model. These tests are the
# privacy boundary written down: what a tool result may carry, and -- far more
# importantly -- what it may not.
# ---------------------------------------------------------------------------


def test_a_tool_result_is_its_own_kind_of_turn():
    t = Transcript()
    turn = t.add_tool_result(tool="move_files", status="ok", summary="Moved three files.")
    assert turn.role == "tool"
    assert turn.text.startswith(TOOL_PREFIX)
    assert "move_files" in turn.text and "ok" in turn.text
    assert "Moved three files." in turn.text


def test_a_tool_result_reaches_the_model_as_conversation_text():
    # NOT role "tool": an OpenAI-shaped tool message is only legal after an
    # assistant message carrying the matching tool_call_id, and this transcript
    # keeps no tool_call ids. The prefix carries the meaning instead.
    t = Transcript()
    t.add_user("move them")
    t.add_tool_result(tool="move_files", status="ok", summary="Moved three files.")
    messages = t.messages()
    assert [m["role"] for m in messages] == ["user", "user"]
    assert messages[-1]["content"].startswith(TOOL_PREFIX)


def test_a_blocked_step_carries_the_sentence_daa_said():
    turn = Transcript().add_tool_result(
        tool="press_button", status="blocked", summary="I could not find the ok button."
    )
    assert "blocked" in turn.text
    assert "I could not find the ok button." in turn.text


def test_a_dry_run_says_so_in_words():
    # With dry_run on nothing changes, so a model that checks its own work
    # retries forever. The result has to tell it, not leave it to infer.
    turn = Transcript().add_tool_result(
        tool="move_files", status="ok", summary="Dry run: I would move three files.",
        dry_run=True,
    )
    assert "DRY RUN" in turn.text
    assert "do not retry" in turn.text.lower()


def test_page_text_never_survives_into_a_tool_result():
    body = "the entire body of a page the user is signed into"
    text = tool_result_text(
        tool="read_page",
        status="ok",
        summary="That page is about 900 words with six headings.",
        data={
            "text": body,
            "title": "Statement for account 12 34 56",
            "url": "https://bank.example/statements/april",
            "site": "bank.example",
            "headings": ["April", "May", "June"],
            "words": 900,
            "logged_in": True,
        },
    )
    assert body not in text
    assert "Statement" not in text
    assert "statements/april" not in text
    # What DOES survive is shape: a count, a flag, a number, the host.
    assert "headings_count=3" in text
    assert "logged_in=true" in text
    assert "words=900" in text


def test_shape_data_keeps_counts_flags_and_numbers_only():
    shaped = shape_data(
        {
            "matches": ["a", "b"],
            "logged_in": False,
            "words": 12,
            "ratio": 0.12345,
            "site": "example.com",
            "label": "Send payment",
            "nothing": None,
        }
    )
    assert shaped["matches_count"] == 2
    assert shaped["logged_in"] is False
    assert shaped["words"] == 12
    assert shaped["ratio"] == 0.123
    assert shaped["site"] == "example.com"
    # A control label is content, whatever key it arrives under.
    assert "label" not in shaped
    assert "nothing" not in shaped


def test_shape_data_drops_every_content_key_the_audit_log_knows_about():
    from daa.safety.audit import REDACTED_KEYS

    shaped = shape_data({k: "payload" for k in REDACTED_KEYS} | {"words": 3})
    assert shaped == {"words": 3}


def test_shape_data_is_bounded_in_keys_and_in_value_length():
    shaped = shape_data({f"n{i}": i for i in range(50)})
    assert len(shaped) <= 6
    # A long "category" string is a payload wearing a category's name.
    assert shape_data({"site": "x" * 400}) == {}
    assert shape_data(None) == {}


def test_a_tool_result_is_scrubbed_for_shapes_that_are_secrets_anywhere():
    text = tool_result_text(
        tool="get_clipboard",
        status="ok",
        summary="The clipboard has sk-abcdefghijklmnopqrs123.",
    )
    assert "sk-abcdefghijklmnopqrs123" not in text
    assert "redacted" in text


def test_tool_results_are_held_to_a_tighter_cap_than_a_human_sentence():
    t = Transcript(max_tool_chars=60)
    turn = t.add_tool_result(tool="read_page", status="ok", summary="word " * 200)
    assert len(turn.text) <= 60
    # A human sentence still gets the full allowance.
    assert len(t.add_user("y" * 400).text) == 400


def test_the_window_is_wide_enough_for_a_whole_multi_step_turn():
    # An eight-step task is one instruction plus eight results plus whatever
    # the model says in between. A window that evicts the INSTRUCTION half way
    # through is how an agent forgets what it was doing.
    from daa.config import Settings

    t = Transcript()
    t.add_user("file the ferrari screenshots and tell me how many there were")
    for i in range(Settings().agent_max_steps):
        t.add_tool_result(tool="move_files", status="ok", summary=f"Step {i} done.")
        t.add_assistant("Working on it.")
    assert t.turns[0].text.startswith("file the ferrari screenshots")
