"""The CLI, with every key removed and every provider faked.

`daa say` and `daa doctor` working on a laptop with no .env is not a nicety --
it is the acceptance criterion for this workstream. If these tests need a key
to pass, the design is wrong.
"""

from __future__ import annotations

import pytest

from daa import cli
from daa.config import Settings
from daa.contracts import Disposition, ResolvedAction, RiskTier, ToolResult, ToolSpec, UndoAction
from daa.voice.llm import FakeLLM, LLMTurn, ToolCall
from daa.voice.loop import VoiceLoop
from daa.voice.mic import FakeMic
from daa.voice.stt import FakeTranscriber
from daa.voice.tts import FakeSpeaker

KEYS = (
    "TYPESAFE_API_KEY",
    "DEEPSEEK_API_KEY",
    "ASSEMBLYAI_API_KEY",
    "INWORLD_API_KEY",
    "OPENAI_API_KEY",
)


@pytest.fixture(autouse=True)
def no_keys_no_home(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """No credentials, and a throwaway HOME so nothing writes to the real one."""
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.Settings, "load", classmethod(lambda cls: Settings()))
    yield


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def test_every_subcommand_parses():
    parser = cli.build_parser()
    for argv in (["listen"], ["say", "hello"], ["doctor"], ["undo"]):
        assert parser.parse_args(argv).func is not None


def test_a_missing_subcommand_is_an_error():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_say_joins_its_words():
    args = cli.build_parser().parse_args(["say", "move", "the", "screenshots"])
    assert args.text == ["move", "the", "screenshots"]


def test_say_takes_repeated_scripted_replies():
    args = cli.build_parser().parse_args(["say", "x", "--reply", "yes", "--reply", "no"])
    assert args.reply == ["yes", "no"]


def test_gate_is_off_by_default_for_typed_text():
    # Typing is an unambiguous act of address; making the dev loop wait on a
    # Jev round trip would make people stop using it.
    assert cli.build_parser().parse_args(["say", "x"]).gate is False
    assert cli.build_parser().parse_args(["say", "x", "--gate"]).gate is True


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_runs_with_no_keys_and_no_network(capsys: pytest.CaptureFixture[str]):
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    for section in ("settings", "api keys", "tcc permissions", "providers", "modules"):
        assert section in out


def test_doctor_reports_missing_keys_without_printing_them(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-super-secret-value")
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "sk-super-secret-value" not in out, "doctor leaked a key into the terminal"
    assert "TYPESAFE_API_KEY" in out and "missing" in out


def test_doctor_names_every_provider_live_or_fake(capsys: pytest.CaptureFixture[str]):
    cli.main(["doctor"])
    out = capsys.readouterr().out
    for provider in ("mic", "stt.local", "stt.cloud", "tts", "llm", "jev"):
        assert provider in out
    assert "fake" in out


def test_doctor_flags_an_unwired_safety_policy(capsys: pytest.CaptureFixture[str]):
    cli.main(["doctor"])
    out = capsys.readouterr().out
    # Either the real policy landed, or doctor says loudly that nothing can run.
    assert "safety.policy" in out
    if "safety.policy    MISSING" in out.replace("  ", "  "):
        assert "every action will be REFUSED" in out


# ---------------------------------------------------------------------------
# say
# ---------------------------------------------------------------------------


def test_say_works_end_to_end_with_no_keys(capsys: pytest.CaptureFixture[str]):
    assert cli.main(["say", "hello", "--silent"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("daa>")


def test_say_prints_every_spoken_line(monkeypatch: pytest.MonkeyPatch,
                                      capsys: pytest.CaptureFixture[str]):
    loop, _spy = _stub_loop(tier=RiskTier.ANNOUNCE)
    monkeypatch.setattr(cli, "_build", lambda settings, **kw: loop)
    cli.main(["say", "move", "the", "screenshots", "--silent"])
    assert "daa> Moved three files." in capsys.readouterr().out


def test_say_passes_scripted_replies_to_the_confirmation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    loop, spy = _stub_loop(tier=RiskTier.CONFIRM_VOICE, verdicts=["yes"])
    monkeypatch.setattr(cli, "_build", lambda settings, **kw: loop)
    cli.main(["say", "move them", "--reply", "yes", "--silent"])
    assert len(spy.runs) == 1


def test_say_without_a_reply_abandons_a_confirmation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    loop, spy = _stub_loop(tier=RiskTier.CONFIRM_VOICE, verdicts=["yes"])
    monkeypatch.setattr(cli, "_build", lambda settings, **kw: loop)
    cli.main(["say", "move them", "--silent"])
    assert spy.runs == [], "a CLI confirmation ran with nobody to answer it"


# ---------------------------------------------------------------------------
# listen
# ---------------------------------------------------------------------------


def test_listen_refuses_when_the_safety_policy_is_not_wired(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    loop, _spy = _stub_loop()
    loop.policy_decide = None
    monkeypatch.setattr(cli, "_build", lambda settings, **kw: loop)
    assert cli.main(["listen"]) == 2
    assert "doctor" in capsys.readouterr().err


def test_listen_consumes_the_mic_and_cleans_up(monkeypatch: pytest.MonkeyPatch):
    loop, spy = _stub_loop(tier=RiskTier.ANNOUNCE, utterances=["move them"])
    monkeypatch.setattr(cli, "_build", lambda settings, **kw: loop)
    assert cli.main(["listen", "--silent"]) == 0
    assert len(spy.runs) == 1
    assert loop.mic._closed is True


def test_listen_survives_ctrl_c(monkeypatch: pytest.MonkeyPatch,
                               capsys: pytest.CaptureFixture[str]):
    loop, _spy = _stub_loop()
    monkeypatch.setattr(loop, "run", _raise_keyboard_interrupt)
    monkeypatch.setattr(cli, "_build", lambda settings, **kw: loop)
    assert cli.main(["listen"]) == 0
    assert "stopped" in capsys.readouterr().err


def test_listen_reports_an_unexpected_error_instead_of_tracebacking(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """A traceback out of `daa listen` costs the user the whole session -- the
    mic, the buffered utterance, the lot -- and tells them nothing usable."""
    loop, _spy = _stub_loop()
    monkeypatch.setattr(loop, "run", _raise_boom)
    monkeypatch.setattr(cli, "_build", lambda settings, **kw: loop)

    assert cli.main(["listen"]) == 1
    err = capsys.readouterr().err
    assert "unexpected error" in err and "doctor" in err
    assert loop.mic._closed is True, "the mic was left open"


def _raise_keyboard_interrupt(**kwargs):
    raise KeyboardInterrupt


def _raise_boom(**kwargs):
    raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# undo
# ---------------------------------------------------------------------------


def test_undo_with_an_empty_journal(monkeypatch: pytest.MonkeyPatch,
                                    capsys: pytest.CaptureFixture[str]):
    loop, _spy = _stub_loop()
    monkeypatch.setattr(cli, "_build", lambda settings, **kw: loop)
    assert cli.main(["undo", "--silent"]) == 0
    assert "nothing to undo" in capsys.readouterr().out.lower()


def test_undo_reverses_the_last_mutation(monkeypatch: pytest.MonkeyPatch,
                                         capsys: pytest.CaptureFixture[str]):
    loop, spy = _stub_loop(tier=RiskTier.ANNOUNCE, utterances=["move them"])
    monkeypatch.setattr(cli, "_build", lambda settings, **kw: loop)
    cli.main(["listen", "--silent"])
    spy.runs.clear()
    # --yes: undo is never below CONFIRM_VOICE, because the instruction came
    # out of a file rather than out of the user's mouth.
    cli.main(["undo", "--yes", "--silent"])
    assert len(spy.runs) == 1
    assert loop.journal.committed == ["e0"], "the journal entry was not spent"


def test_undo_without_an_answer_leaves_the_journal_alone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """`daa undo` with nothing to answer the confirmation must not spend the
    record. Before the fix the entry was consumed before dispatch, so the undo
    was gone whether or not anything was undone."""
    loop, spy = _stub_loop(tier=RiskTier.ANNOUNCE, utterances=["move them"])
    monkeypatch.setattr(cli, "_build", lambda settings, **kw: loop)
    cli.main(["listen", "--silent"])
    spy.runs.clear()

    assert cli.main(["undo", "--silent"]) == 0
    assert spy.runs == []
    assert loop.journal.committed == []
    assert loop.journal.peek() is not None, "the undo record was thrown away"


def test_undo_list_shows_the_journal(monkeypatch: pytest.MonkeyPatch,
                                     capsys: pytest.CaptureFixture[str]):
    loop, _spy = _stub_loop(tier=RiskTier.ANNOUNCE, utterances=["move them"])
    monkeypatch.setattr(cli, "_build", lambda settings, **kw: loop)
    cli.main(["listen", "--silent"])
    capsys.readouterr()
    assert cli.main(["undo", "--list"]) == 0
    assert "move them back" in capsys.readouterr().out


def test_undo_list_without_a_journal(monkeypatch: pytest.MonkeyPatch,
                                     capsys: pytest.CaptureFixture[str]):
    loop, _spy = _stub_loop()
    loop.journal = None
    monkeypatch.setattr(cli, "_build", lambda settings, **kw: loop)
    assert cli.main(["undo", "--list"]) == 2


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

SPEC = ToolSpec(
    name="move_files",
    description="Move files",
    params={},
    floor=RiskTier.SILENT,
    inverses=("move_files",),
)


class _Tool:
    spec = SPEC

    def __init__(self) -> None:
        self.runs: list[ResolvedAction] = []

    def resolve(self, **kwargs):
        return ResolvedAction(tool="move_files", args=kwargs, targets=("three files",))

    def run(self, action):
        self.runs.append(action)
        return ToolResult(
            ok=True,
            summary="Moved three files.",
            undo=UndoAction(description="move them back", tool="move_files", args={}),
        )


class _Registry:
    """Raises on a miss, like the real ToolRegistry."""

    def __init__(self, tool):
        self._tool = tool

    def get(self, name):
        if name != "move_files":
            raise KeyError(f"no tool named {name!r}")
        return self._tool

    def specs(self):
        return [SPEC]


class _Entry:
    def __init__(self, undo, produced_by, entry_id):
        self.action = undo
        self.produced_by = produced_by
        self.id = entry_id
        self.stale = None

    trusted = True
    tool = property(lambda self: self.action.tool)
    args = property(lambda self: self.action.args)
    description = property(lambda self: self.action.description)


class _Journal:
    def __init__(self):
        self.entries: list[_Entry] = []
        self.committed: list[str] = []

    def record(self, undo, context=None, *, produced_by):
        entry = _Entry(undo, produced_by, f"e{len(self.entries)}")
        self.entries.append(entry)
        return entry

    def peek(self):
        return self.entries[-1] if self.entries else None

    def commit(self, entry):
        self.committed.append(entry.id)
        self.entries = [e for e in self.entries if e.id != entry.id]
        return True

    def pop(self):
        return self.entries.pop() if self.entries else None

    def history(self):
        return list(self.entries)


class _Confirm:
    def __init__(self, verdicts):
        self.verdicts = list(verdicts)

    def interpret(self, reply, pending):
        return self.verdicts.pop(0) if self.verdicts else "unclear"


def _stub_loop(*, tier: RiskTier = RiskTier.SILENT, utterances=(), verdicts=("yes",)):
    tool = _Tool()
    loop = VoiceLoop(
        settings=Settings(dry_run=False),
        mic=FakeMic(utterances=list(utterances)),
        local_stt=FakeTranscriber(source="local"),
        speaker=FakeSpeaker(),
        registry=_Registry(tool),
        policy_decide=lambda a, s, r, st: Disposition(tier=tier, reason="stub"),
        journal=_Journal(),
        confirm=_Confirm(verdicts),
        llm=FakeLLM(
            default=LLMTurn(tool_calls=(ToolCall("move_files", {}),)),
        ),
    )
    return loop, tool
