"""Cross-seam tests.

Every subsystem was built and tested in isolation, and all of those suites
passed while the bug below was live: `build_loop` handed work to tools that
had independently read os.environ instead of the Settings the caller passed.
No single-subsystem test could have caught it, because in isolation each half
was correct. These tests belong to the seams, not to any one module.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from daa.config import Settings
from daa.contracts import RiskTier

pytest.importorskip("daa.tools")
pytest.importorskip("daa.voice.loop")


def test_registry_tools_obey_the_callers_settings_not_the_environment(monkeypatch):
    """The safety flag must have exactly one source of truth.

    Regression: with DAA_DRY_RUN=0 in the environment and an explicit
    Settings(dry_run=True) passed to build_loop, `move_to_trash` really
    deleted the file. The in-code flag is the one a human reaches for when
    they want to be careful; the environment must not be able to overrule it.
    """
    from daa.voice.loop import build_loop

    monkeypatch.setenv("DAA_DRY_RUN", "0")  # environment says LIVE
    loop = build_loop(Settings(dry_run=True))  # caller says BE SAFE

    assert loop.registry is not None
    for spec in loop.registry.specs():
        tool = loop.registry.get(spec.name)
        assert tool.dry_run is True, f"{spec.name} ignored the caller's dry_run"


def test_environment_cannot_silently_make_a_loop_live(monkeypatch):
    """The same divergence in the direction that destroys data."""
    from daa.voice.loop import build_loop

    monkeypatch.setenv("DAA_DRY_RUN", "0")
    loop = build_loop(Settings(dry_run=True))

    d = pathlib.Path(tempfile.mkdtemp())
    victim = d / "important.txt"
    victim.write_text("do not delete")

    tool = loop.registry.get("move_to_trash")
    tool.run(tool.resolve(paths=[str(victim)]))
    assert victim.exists(), "an explicit dry_run=True did not prevent a real deletion"


def test_every_mutating_tool_floor_survives_a_maximally_reassuring_judgment():
    """A compromised or simply wrong Jev must not be able to unlock a tool.

    This is the one-way rule observed through the REAL registry rather than
    synthetic ToolSpecs, so a tool author who sets a floor too low is caught
    here and not in production.
    """
    from daa.contracts import ResolvedAction, RiskAssessment
    from daa.safety import policy
    from daa.tools.registry import REGISTRY

    reassuring = RiskAssessment(
        blast_radius=0.0,
        unrecoverable=0.0,
        explicitly_requested=1.0,
        target_confidence="certain",
        confidence=1.0,
    )
    s = Settings()
    for spec in REGISTRY.specs():
        d = policy.decide(
            ResolvedAction(tool=spec.name, args={}, targets=("x",)), spec, reassuring, s
        )
        assert d.tier >= spec.floor, f"{spec.name}: {d.tier.name} < floor {spec.floor.name}"
        if spec.floor >= RiskTier.CONFIRM_VOICE:
            assert d.tier >= RiskTier.CONFIRM_VOICE, (
                f"{spec.name} became silently executable on a confident judgment"
            )


def test_an_approved_visual_action_does_not_also_speak_a_refusal():
    """Regression: `daa say` prints spoken lines AFTER the turn completes.

    `_confirm_visual` used to speak "that has to be approved on screen, type
    yes there" as a pre-announcement. Because the CLI batches spoken output,
    it surfaced after the user had already typed yes -- so an APPROVED action
    was followed by a sentence refusing it. The card's own header carries that
    message, and that branch is only reachable when a console exists.
    """
    from daa.voice import loop as loop_mod

    assert not hasattr(loop_mod, "_NEEDS_SCREEN"), (
        "the pre-announcement is back; it contradicts an approval under `daa say`"
    )
    src = pathlib.Path(loop_mod.__file__).read_text()
    body = src.split("def _confirm_visual", 1)[1].split("\n    def ", 1)[0]
    assert "_NO_SCREEN" in body, "the no-console refusal must stay"
    # Every remaining spoken line in this method must be a REFUSAL: no console
    # (_NO_SCREEN) or the user cancelled. Nothing is said on the approved path,
    # because the approved path continues to _execute, which does the talking.
    spoken = [ln.strip() for ln in body.splitlines() if "_speak(" in ln]
    assert len(spoken) == 2, f"expected the two refusal lines, got {spoken}"
    assert any("_NO_SCREEN" in s for s in spoken)
    assert any("leaving it" in s for s in spoken)


def test_an_action_that_resolved_to_nothing_never_reaches_a_confirmation():
    """A confirmation with no content is training for the one that matters.

    `resolve()` already knows it failed -- the sentence is in
    `args["reason"]` and `run()` returns it -- but the readback in between is
    built from `targets`, so with none `describe()` degrades to the bare verb.
    At CONFIRM_VOICE that used to ask "Should I press?" and only admit "I
    could not find the ok button" AFTER the user said yes. Fail-safe, and
    still wrong: every content-free yes is practice for the real one.
    """
    import sys
    from dataclasses import replace
    from unittest.mock import patch

    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    import computer_tree as ct
    from daa.jev.client import FakeJev
    from daa.jev.confirm import ConfirmParser
    from daa.jev.risk import RiskGate
    from daa.safety import policy
    from daa.tools import install
    from daa.tools.registry import ToolRegistry
    from daa.voice.llm import FakeLLM, LLMTurn, ToolCall
    from daa.voice.loop import VoiceLoop
    from daa.voice.tts import FakeSpeaker

    s = replace(Settings(), enable_computer_use=True)
    tree = ct.tree(
        ct.el("Delete Account", app="Safari", bundle_id="com.apple.Safari", window="Settings"),
        app="Safari",
        bundle_id="com.apple.Safari",
    )
    jev = FakeJev({"blast_radius": 0.5, "unrecoverable": 0.1, "explicitly_requested": 0.9,
                   "target_confidence": "certain", "consent": 0.99})

    with patch("daa.tools.computer.tools.snapshot", ct.serve(tree)):
        spk = FakeSpeaker()
        confirm = ConfirmParser(jev, s)
        loop = VoiceLoop(
            settings=s, speaker=spk, registry=install(ToolRegistry(), s),
            risk=RiskGate(jev, s), confirm=confirm, policy_decide=policy.decide,
            llm=FakeLLM(turns=[LLMTurn(tool_calls=[
                ToolCall("ui_click", {"target": "the ok button", "app": "Safari"})])]),
        )
        loop.handle_text("click ok", replies=["yes"])

    said = " ".join(spk.said)
    assert "could not find" in said, f"the reason was never spoken: {spk.said}"
    assert "should i" not in said.lower(), (
        f"a confirmation was spent on an action that cannot happen: {spk.said}"
    )
