"""Shared contracts. EVERY module in daa conforms to this file.

This is the boundary between the three subsystems that must never reach into
each other directly:

    voice/   hears and speaks          (audio in, text out)
    jev/     judges                    (state + typed questions -> typed answers)
    tools/   touches the machine       (typed args -> ToolResult + undo)
    safety/  decides what is allowed   (RiskAssessment -> Disposition)

Nothing in tools/ may import jev/. Nothing in voice/ may import tools/.
The only thing that knows about all of them is the loop in voice/loop.py,
and it only ever speaks in the types defined here.
"""

from __future__ import annotations

import enum
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------


class RiskTier(enum.IntEnum):
    """What the system must do before performing an action.

    Ordered: a higher tier is strictly more cautious. Policy may only ever
    RAISE a tier above the tool's declared floor, never lower it — see
    safety/policy.py. That one-way rule is what keeps a confident-but-wrong
    Jev answer from turning `rm` into a silent execution.
    """

    SILENT = 0          # read-only; just do it, don't even mention it
    ANNOUNCE = 1        # do it, then say what was done
    CONFIRM_VOICE = 2   # read the resolved target back, wait for a yes
    CONFIRM_VISUAL = 3  # show a card; voice alone can never authorize this
    REFUSE = 4          # never automate; tell the user to do it themselves


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Static description of a tool. Registered once at import time."""

    name: str
    description: str
    # JSON-schema-ish dict describing params. Kept as a plain dict rather than
    # a pydantic model so the LLM tool schema and the Jev `criteria` map can be
    # generated from one source without a second dependency.
    params: Mapping[str, Any]
    # The LOWEST tier this tool may ever run at, regardless of what Jev says.
    # `run_shell` declares CONFIRM_VOICE even though `ls` is harmless, because
    # the floor is about the tool's reach, not a given invocation.
    floor: RiskTier = RiskTier.ANNOUNCE
    # Free-text used by the Jev semantic router to decide whether to activate
    # this tool for a given utterance. Richer than `description`, never sent
    # to the conversational LLM.
    activation_hint: str = ""
    tags: tuple[str, ...] = ()
    # Tools that may legitimately serve as this tool's inverse. The undo
    # journal is a world-readable file on disk, which makes it UNTRUSTED INPUT
    # in exactly the way the LLM is: anything running as the user can append a
    # row naming any tool it likes. A row whose `tool` is not listed here by
    # the tool that produced it is refused. Empty means "this tool has no
    # inverse", which is a stronger statement than "undo is missing".
    inverses: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UndoAction:
    """The inverse of a mutation. Produced by the tool that made the change.

    A tool that mutates state and returns undo=None is a bug, and
    tests/test_undo_coverage.py fails the build for it.
    """

    description: str          # "move 3 files back to ~/Downloads"
    tool: str                 # tool name to call to undo
    args: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    # One short sentence, written to be SPOKEN. No paths, no IDs, no markdown.
    summary: str
    data: Mapping[str, Any] = field(default_factory=dict)
    undo: UndoAction | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedAction:
    """A tool call after arguments have been resolved to concrete targets.

    The distinction from a raw LLM tool call matters: the risk gate and the
    spoken confirmation both run against RESOLVED targets. Confirming against
    the raw utterance ("delete the old ones") is how you delete the wrong
    folder.
    """

    tool: str
    args: Mapping[str, Any]
    # Human-readable, speakable resolutions: ["Screenshot 2026-09-20.png", ...]
    targets: Sequence[str] = ()
    # True when the user named the target outright; False when we inferred it.
    # Feeds the Jev risk gate as prior context, and is a hard input to policy.
    explicit: bool = True
    # The spoken verb: "move to the Trash", "send". Without it a readback is a
    # bare noun phrase -- "report.pdf - should I go ahead?" -- which tells the
    # user WHAT but never WHAT WILL HAPPEN TO IT, and reads identically for
    # revealing a file and for deleting it.
    # A floor this PARTICULAR invocation must not go below, raised by the
    # resolver -- deterministic code that inspected the real world, not a
    # model. `ToolSpec.floor` is per-tool, so without this the only way to say
    # "this click is a payment" or "this move overwrites something" is to
    # convince a judgment model, which is precisely what floors exist to
    # defend against. Policy takes max(spec.floor, floor_hint, derived): it can
    # only ever RAISE, so a resolver cannot use it to make anything cheaper.
    floor_hint: RiskTier | None = None
    verb: str = ""
    # Consequences the user MUST hear, because they change what consent means:
    # {"overwrite": "replacing 1 file that is already there"}. Everything here
    # is appended to the readback verbatim. A destructive modifier that is
    # known at resolve time and not spoken makes the confirmation a lie.
    consequences: Mapping[str, str] = field(default_factory=dict)

    def _targets_phrase(self) -> str:
        if len(self.targets) <= 3:
            return ", ".join(self.targets)
        return f"{', '.join(self.targets[:2])} and {len(self.targets) - 2} more"

    def describe(self) -> str:
        # A SPOKEN fragment, verb-first whenever a verb is known.
        verb = self.verb or self.tool.replace("_", " ")
        body = f"{verb} {self._targets_phrase()}".strip() if self.targets else verb
        if self.consequences:
            body += ", " + ", ".join(self.consequences.values())
        return body


@runtime_checkable
class Tool(Protocol):
    spec: ToolSpec

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        """Turn loose args into concrete targets WITHOUT mutating anything.

        Always safe to call. The loop calls resolve() first, runs the risk
        gate on the result, and only then calls run().
        """
        ...

    def run(self, action: ResolvedAction) -> ToolResult:
        ...


# ---------------------------------------------------------------------------
# Jev — the judgment layer
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Choice:
    instructions: str
    criteria: Mapping[str, str | None]
    kind: Literal["choice"] = "choice"


@dataclass(frozen=True, slots=True)
class Score:
    instructions: str
    criteria: Sequence[str]        # ordered levels, low -> high
    kind: Literal["score"] = "score"


@dataclass(frozen=True, slots=True)
class Noul:
    instructions: str
    criteria: str | None = None
    kind: Literal["noul"] = "noul"


Question = Choice | Score | Noul


@dataclass(frozen=True, slots=True)
class ChoiceAnswer:
    choice: str
    probabilities: Mapping[str, float]
    confidence: float


@dataclass(frozen=True, slots=True)
class ScoreAnswer:
    score: float
    legend: Sequence[str]
    probabilities: Sequence[float]
    confidence: float


@dataclass(frozen=True, slots=True)
class NoulAnswer:
    """Jev returns a probability and NO separate confidence for nouls --
    the probability itself is the signal. `confidence` here is a derived
    convenience: distance from maximum uncertainty, rescaled to 0..1."""

    noul: float

    @property
    def confidence(self) -> float:
        return abs(self.noul - 0.5) * 2.0


Answer = ChoiceAnswer | ScoreAnswer | NoulAnswer


@dataclass(frozen=True, slots=True)
class Answers:
    answers: Mapping[str, Answer]
    latency_ms: float
    # True when served by the fake/replay provider. Anything that acts on a
    # judgment logs this, so an eval run can never be mistaken for a live one.
    synthetic: bool = False

    def choice(self, key: str) -> ChoiceAnswer:
        a = self.answers[key]
        assert isinstance(a, ChoiceAnswer), f"{key} is {type(a).__name__}"
        return a

    def score(self, key: str) -> ScoreAnswer:
        a = self.answers[key]
        assert isinstance(a, ScoreAnswer), f"{key} is {type(a).__name__}"
        return a

    def noul(self, key: str) -> float:
        a = self.answers[key]
        assert isinstance(a, NoulAnswer), f"{key} is {type(a).__name__}"
        return a.noul


class JevProvider(Protocol):
    """The ONLY seam through which daa talks to TypeSafe.

    Deliberately not the vendor SDK type. typesafe-sdk is six days old and
    will churn; when it does, exactly one file changes (jev/client.py) and
    every test keeps passing because they run on FakeJev.
    """

    def ask(
        self,
        state: Mapping[str, Any],
        questions: Mapping[str, Question],
        *,
        timeout_s: float = 2.0,
    ) -> Answers:
        ...


# ---------------------------------------------------------------------------
# Safety dispositions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    blast_radius: float          # 0..3, continuous (Score can land between levels)
    unrecoverable: float         # 0..1 noul
    explicitly_requested: float  # 0..1 noul
    target_confidence: str       # "certain" | "probable" | "guessing"
    # Minimum confidence across the DANGER answers (blast_radius,
    # unrecoverable) -- NOT across all four. Doubt about whether the user
    # explicitly asked, or about which target we resolved, is not doubt about
    # whether being wrong would hurt; folding it in here made a read-only
    # Spotlight search demand a spoken confirmation. Those two have their own
    # escalation rules, so counting their confidence here double-counted them.
    confidence: float
    # Per-answer confidence for every question, for the audit log and for
    # "why are you asking?". Judgment lives in `confidence`; this is evidence.
    answer_confidence: Mapping[str, float] = field(default_factory=dict)
    synthetic: bool = False


@dataclass(frozen=True, slots=True)
class Disposition:
    tier: RiskTier
    reason: str                  # short, spoken if the user asks "why?"
    assessment: RiskAssessment | None = None


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuditEvent:
    kind: str
    payload: Mapping[str, Any]
    at: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


AuditSink = Callable[[AuditEvent], None]
