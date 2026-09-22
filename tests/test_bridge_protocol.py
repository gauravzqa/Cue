"""The wire format, line by line.

`ui/tools/fake-bridge` is the reference implementation of this protocol and
the Swift suite decodes its exact `confirm.request` frame as a fixture
(`ConfirmRouterTests.testTheFakeBridgeFrameDecodesCompletely`). Where the
document and the fake bridge disagree, the fake bridge is right, so the fake
bridge's frame is reproduced here and asserted field for field.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from daa.contracts import AuditEvent, Disposition, ResolvedAction, RiskAssessment, RiskTier
from daa.safety.audit import record, scrub
from daa.ui import protocol
from daa.ui.protocol import (
    CHATTY_KINDS,
    LOAD_BEARING_KINDS,
    Event,
    FrameError,
    Request,
    Response,
)

# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------


def roundtrip(frame: Any) -> Any:
    return protocol.decode(protocol.encode(frame))


def test_the_three_frame_kinds_survive_a_round_trip():
    assert roundtrip(protocol.request("r7", "method.name", a=1)) == Request(
        id="r7", method="method.name", params={"a": 1}
    )
    assert roundtrip(protocol.event("ready", daa="0.1.0")) == Event(
        method="ready", params={"daa": "0.1.0"}
    )
    assert roundtrip(protocol.response("r7", True, {"granted": True})) == Response(
        id="r7", ok=True, params={"granted": True}
    )


def test_every_frame_is_exactly_one_line_with_no_raw_newlines():
    """A script is the whole reason this tier exists, and a script is full of
    newlines. JSON escaping is what keeps one frame on one line."""
    script = 'tell application "Messages"\n  send "hi" to buddy "Alex"\nend tell'
    line = protocol.encode(protocol.request("cf_1", "confirm.request", script=script))
    assert line.count(b"\n") == 1
    assert line.endswith(b"\n")
    assert protocol.decode(line).params["script"] == script


def test_a_res_with_no_ok_field_decodes_as_an_error_never_as_success():
    """The only thing a response ever authorises is an action, and an
    unreadable answer is not an approval."""
    for line in (
        '{"t":"res","id":"cf_1"}',
        '{"t":"res","id":"cf_1","p":{"granted":true}}',
        '{"t":"res","id":"cf_1","ok":null,"p":{"granted":true}}',
        '{"t":"res","id":"cf_1","ok":"true","p":{"granted":true}}',
        '{"t":"res","id":"cf_1","ok":1,"p":{"granted":true}}',
    ):
        frame = protocol.decode(line)
        assert isinstance(frame, Response)
        assert frame.ok is False, f"{line} decoded as an approval"


def test_ok_false_carries_the_error_across():
    frame = protocol.decode('{"t":"res","id":"r1","ok":false,"err":{"code":"x","message":"y"}}')
    assert (frame.ok, frame.code, frame.message) == (False, "x", "y")


@pytest.mark.parametrize(
    "line",
    [
        "",
        "   ",
        "not json at all",
        "[1,2,3]",
        '"a string"',
        "{}",
        '{"t":"nope"}',
        '{"t":"req","m":"x"}',
        '{"t":"req","id":"r1"}',
        '{"t":"req","id":"","m":"x"}',
        '{"t":"ev"}',
        '{"t":"res"}',
        '{"t":"req","id":7,"m":"x"}',
    ],
)
def test_an_unreadable_line_raises_rather_than_guessing(line: str):
    with pytest.raises(FrameError):
        protocol.decode(line)


def test_a_non_object_p_is_an_empty_payload_not_a_crash():
    assert protocol.decode('{"t":"ev","m":"x","p":3}').params == {}
    assert protocol.decode('{"t":"ev","m":"x"}').params == {}


def test_invalid_utf8_bytes_are_a_frame_error():
    with pytest.raises(FrameError):
        protocol.decode(b'{"t":"ev","m":"\xff\xfe"}')


# ---------------------------------------------------------------------------
# The approval card
# ---------------------------------------------------------------------------

SCRIPT = 'tell application "Messages"\n  send "on my way" to buddy "Alex"\nend tell'


def a_card(**over: Any) -> dict[str, Any]:
    action = ResolvedAction(
        tool="run_applescript",
        args={"script": SCRIPT, "recipient": "Alex"},
        targets=("a script of 3 lines",),
        explicit=False,
        verb="run",
        consequences={"send": "sending this text: 'on my way'"},
    )
    assessment = RiskAssessment(
        blast_radius=2.4,
        unrecoverable=0.81,
        explicitly_requested=0.19,
        target_confidence="probable",
        confidence=0.77,
        synthetic=False,
    )
    disposition = Disposition(
        tier=RiskTier.CONFIRM_VISUAL,
        reason="unrecoverable and not explicitly requested",
        assessment=assessment,
    )
    from daa.voice.loop import _SCRIPT_KEYS, _phrase

    kwargs: dict[str, Any] = {
        "dry_run": True,
        "phrase": "run a script that sends a message to Alex",
        "script_keys": _SCRIPT_KEYS,
        "expires_in_ms": 90_000,
    }
    kwargs.update(over)
    assert callable(_phrase)
    return protocol.card_payload(action, disposition, **kwargs)


def test_the_card_matches_the_fake_bridges_frame_field_for_field():
    """`ui/tools/fake-bridge`'s `card("script")`, which the Swift suite
    decodes as a fixture. Where the document and the fake bridge disagree,
    the fake bridge is right."""
    expected = {
        "tool": "run_applescript",
        "tier": "CONFIRM_VISUAL",
        "reason": "unrecoverable and not explicitly requested",
        "phrase": "run a script that sends a message to Alex",
        "verb": "run",
        "explicit": False,
        "dryRun": True,
        "targets": ["a script of 3 lines"],
        "args": [
            {"key": "script", "isProgram": True, "value": SCRIPT},
            {"key": "recipient", "isProgram": False, "value": "Alex"},
        ],
        "consequences": {"send": "sending this text: 'on my way'"},
        "assessment": {
            "blastRadius": 2.4,
            "unrecoverable": 0.81,
            "explicitlyRequested": 0.19,
            "targetConfidence": "probable",
            "confidence": 0.77,
            "synthetic": False,
        },
        "expiresInMs": 90_000,
    }
    assert a_card() == expected
    # And it survives the wire, which is the thing the Swift fixture reads.
    assert protocol.decode(protocol.encode(protocol.request("cf_1", "confirm.request", **expected)))


def test_every_argument_is_rendered_in_full_and_never_summarised():
    long_script = "\n".join(f'do shell script "echo step {i} of 400"' for i in range(1, 401))
    action = ResolvedAction(tool="run_applescript", args={"script": long_script}, verb="run")
    card = protocol.card_payload(
        action,
        Disposition(tier=RiskTier.CONFIRM_VISUAL, reason="x"),
        dry_run=True,
        phrase="run a long script",
        script_keys=("script",),
    )
    assert card["args"][0]["value"] == long_script
    assert len(card["args"][0]["value"].splitlines()) == 400


def test_a_missing_assessment_is_the_worst_case_said_out_loud():
    card = a_card()
    bare = protocol.card_payload(
        ResolvedAction(tool="t", args={}),
        Disposition(tier=RiskTier.CONFIRM_VISUAL, reason="x"),
        dry_run=False,
        phrase="do the thing",
        script_keys=(),
    )
    assert card["assessment"]["synthetic"] is False
    assert bare["assessment"] == {
        "blastRadius": 3.0,
        "unrecoverable": 1.0,
        "explicitlyRequested": 0.0,
        "targetConfidence": "guessing",
        "confidence": 0.0,
        "synthetic": True,
    }


def test_a_nan_assessment_does_not_become_an_unparseable_frame():
    """`json.dumps` writes a bare `NaN`, which the Swift decoder rejects --
    taking the whole card with it, so a judgment glitch would become a
    silently missing approval."""
    assessment = RiskAssessment(
        blast_radius=float("nan"),
        unrecoverable=float("inf"),
        explicitly_requested=0.5,
        target_confidence="probable",
        confidence=float("nan"),
    )
    card = protocol.card_payload(
        ResolvedAction(tool="t", args={}),
        Disposition(tier=RiskTier.CONFIRM_VISUAL, reason="x", assessment=assessment),
        dry_run=True,
        phrase="do the thing",
        script_keys=(),
    )
    line = protocol.encode(protocol.request("cf_1", "confirm.request", **card))
    assert b"NaN" not in line and b"Infinity" not in line
    assert json.loads(line)["p"]["assessment"]["blastRadius"] == 3.0


def test_a_non_string_argument_is_rendered_readably_not_repred():
    card = protocol.card_payload(
        ResolvedAction(tool="t", args={"plan": {"steps": ["a", "b"]}, "n": 3}),
        Disposition(tier=RiskTier.CONFIRM_VISUAL, reason="x"),
        dry_run=True,
        phrase="do the thing",
        script_keys=(),
    )
    values = {a["key"]: a["value"] for a in card["args"]}
    assert json.loads(values["plan"]) == {"steps": ["a", "b"]}
    assert values["n"] == "3"


def test_the_card_never_carries_policy_the_dock_could_reason_about():
    """`tier` and `reason` are opaque strings on the far side. Nothing in the
    payload is a number the dock is expected to compare against a threshold."""
    card = a_card()
    assert isinstance(card["tier"], str)
    assert "floor" not in card and "threshold" not in card


# ---------------------------------------------------------------------------
# The audit tee's vocabulary
# ---------------------------------------------------------------------------


def test_the_chatty_and_load_bearing_sets_are_disjoint():
    """Swift asserts the same thing about its copy in `AuditRecord`."""
    assert CHATTY_KINDS.isdisjoint(LOAD_BEARING_KINDS)


def test_the_kind_sets_match_the_swift_sides_copy():
    """`AuditRecord.chatty` / `.loadBearing` in `ui/Sources/DaaDockCore/Payloads.swift`.
    A kind in one and not the other is a decision record the dock drops under
    load, or a chatty one it refuses to."""
    import pathlib
    import re

    payloads = pathlib.Path(__file__).resolve().parent.parent / "ui/Sources/DaaDockCore/Payloads.swift"
    if not payloads.exists():  # pragma: no cover - the Swift tree is optional
        pytest.skip("the dock sources are not in this checkout")
    text = payloads.read_text()

    def swift_set(name: str) -> set[str]:
        match = re.search(rf"static let {name}: Set<String> = \[(.*?)\]", text, re.DOTALL)
        assert match, f"AuditRecord.{name} has moved"
        return set(re.findall(r'"([^"]+)"', match.group(1)))

    assert swift_set("chatty") == set(CHATTY_KINDS)
    assert swift_set("loadBearing") == set(LOAD_BEARING_KINDS)


# ---------------------------------------------------------------------------
# audit projection
# ---------------------------------------------------------------------------


def payload_of(kind: str, **payload: Any) -> dict[str, Any]:
    return protocol.audit_payload(
        AuditEvent(kind=kind, payload=payload), record=record, scrub=scrub
    )


def test_an_audit_frame_carries_the_four_fields_the_dock_reads():
    out = payload_of("execution", tool="move_files", summary="Moved 3 files.")
    assert set(out) >= {"kind", "id", "at", "payload"}
    assert out["kind"] == "execution"


def test_heard_carries_the_shape_and_never_the_content():
    """The dock refuses to render a `heard` line for exactly this reason. The
    bridge must not send content it would then have to trust nobody displays."""
    from daa.voice.loop import _shape

    out = payload_of("heard", source="local", partial=False, **_shape("my card is 4111..."))
    assert set(out["payload"]) >= {"chars", "words", "sha256_8"}
    assert "text" not in out["payload"]


def test_the_transcript_keys_survive_so_the_dock_has_a_transcript():
    """`text` and `summary` are redacted BY KEY on the way to audit.jsonl,
    which is a record of someone's whole day. The dock is the screen in front
    of the person who just said the sentence, so the four kinds that project
    into a transcript line get theirs back."""
    assert payload_of("spoke", text="Moved 3 files to Archive.")["payload"]["text"] == (
        "Moved 3 files to Archive."
    )
    assert payload_of("woke", text="move my screenshots")["payload"]["text"] == (
        "move my screenshots"
    )
    assert payload_of("execution", summary="Moved 3 files.")["payload"]["summary"] == (
        "Moved 3 files."
    )
    assert payload_of("dry_run", summary="Dry run: I would move 3 files.")["payload"][
        "summary"
    ] == "Dry run: I would move 3 files."


def test_restoring_the_transcript_keys_does_not_lift_the_shape_rules():
    """Only the BY-KEY rule is lifted. A card number spoken aloud is still
    redacted on its way to the screen, and so is a payload-length string."""
    out = payload_of("spoke", text="your card is 4111 1111 1111 1111 apparently")
    assert "4111" not in out["payload"]["text"]
    assert "<redacted card number>" in out["payload"]["text"]

    out = payload_of("spoke", text="x" * 5000)
    assert out["payload"]["text"].startswith("<elided 5000")


def test_no_other_kind_gets_its_content_back():
    out = payload_of("confirm", verdict="yes", reply="yeah go on then")
    assert out["payload"]["reply"].startswith("<redacted ")


def test_synthetic_is_readable_off_the_payload_the_dock_is_given():
    """`record` hoists it to the top level; the dock badges a row from
    `payload.synthetic`, so it has to be in both places."""
    real = payload_of("judgment", assessment={"synthetic": False})
    fake = payload_of("judgment", assessment={"synthetic": True})
    assert real["payload"]["synthetic"] is False
    assert fake["payload"]["synthetic"] is True
    assert fake["synthetic"] is True


def test_undo_id_and_dry_run_reach_the_dock_so_the_undo_button_works():
    """The `↩︎` affordance appears only when `execution` carried
    `payload.undo_id` and `dry_run` was false. Without the field it is
    decoration."""
    from daa.safety.audit import execution_event

    event = execution_event(
        ResolvedAction(tool="move_files", args={}, targets=("a",)),
        ok=True,
        summary="Moved one thing.",
        dry_run=False,
        undo_id="u123456",
    )
    out = protocol.audit_payload(event, record=record, scrub=scrub)
    assert out["payload"]["undo_id"] == "u123456"
    assert out["payload"]["dry_run"] is False


@dataclass(slots=True)
class _Boom:
    calls: list[Any] = field(default_factory=list)


def test_the_projection_never_mutates_the_event_it_was_given():
    """The disk sink runs first and gets the same object. A projection that
    edited the payload in place would put the un-redacted text on disk."""
    event = AuditEvent(kind="spoke", payload={"text": "Moved 3 files."})
    before = dict(event.payload)
    protocol.audit_payload(event, record=record, scrub=scrub)
    assert dict(event.payload) == before
