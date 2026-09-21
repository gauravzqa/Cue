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
    # May a scoped grant ever answer this tool's confirmation on the user's
    # behalf? Static and human-authored, exactly like `floor`. Some things are
    # never answered in advance no matter how narrow the scope: sending a
    # message to a person, spending money, changing auth state, deleting past
    # the Trash. Defaulting True keeps every existing tool unchanged; the
    # dangerous ones are opted OUT by hand, in review.
    grantable: bool = True
    # Cheap discovery for the router and the loop, so neither has to reach for
    # isinstance(tool, LongRunningTool) on a hot path.
    long_running: bool = False


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
    # Set when this result starts (or belongs to) background work, so the loop
    # can say "I've started on that" and know what to watch.
    job_id: str | None = None
    # The checkpoint this step belongs to. Declared by the step rather than
    # inferred by the loop, because only the tool knows where a logical unit
    # of work begins and ends.
    checkpoint_id: str | None = None


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
    #
    # NOT a substitute for splitting a tool. A hint is set by code that has to
    # RUN and be RIGHT; a resolver that fails to recognise a payment form
    # degrades silently to the tool's ordinary floor, and a silent downgrade is
    # the worst failure this codebase has. `ToolSpec.floor` cannot degrade at
    # all. So when a capability has a genuinely dangerous mode, give that mode
    # its own tool with its own static floor (`click_element` vs `submit_form`)
    # and use the hint for the residue. `irreversible` is a per-tool property
    # that test_undo_coverage reads off the spec, so one tool covering both
    # modes would have to lie about one of them.
    floor_hint: RiskTier | None = None
    # Scheme+host ("https://stripe.com") or a bundle id ("com.apple.Safari").
    # Supplied by the resolver. Grant scope matching needs it, and the
    # alternative -- re-parsing args inside safety/ to find a URL -- would put
    # URL parsing in the one module that must stay pure. NEVER included in
    # describe(): the readback names what the user would recognise, and an
    # origin is a machine key, not a spoken phrase.
    origin: str | None = None
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


# ---------------------------------------------------------------------------
# Scoped grants, warrants, jobs
#
# The consent model splits in two. Until now the unit of CONSENT and the unit
# of EXECUTION were the same object (a ResolvedAction), which works only while
# every action is fast, atomic and nameable. A forty-step browser flow is none
# of those, and "click at (847, 312)" cannot be read back honestly at all.
#
# So: execution is unchanged. Every step still goes resolve -> assess ->
# policy.decide -> _execute -> UndoJournal.record, and _execute remains the one
# place a tool may be invoked. Only consent changes shape: the user approves a
# GOAL with a budget and a ceiling, and a Grant can then supply the ANSWER to a
# confirmation that policy already demanded.
#
# A grant is never an input to the tier calculation. `tier = max(spec.floor,
# floor_hint, derived)` is untouched and knows nothing about grants. That is
# the whole defence against a grant quietly becoming blanket permission.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GrantScope:
    """What a grant covers. EMPTY MEANS EMPTY, never "everything".

    The permissive-default bug is the one this type exists to prevent: a scope
    nobody filled in must authorize nothing, so that forgetting to set it fails
    closed and loudly rather than silently widening.
    """

    tools: frozenset[str] = field(default_factory=frozenset)
    origins: frozenset[str] = field(default_factory=frozenset)
    apps: frozenset[str] = field(default_factory=frozenset)
    path_prefixes: tuple[str, ...] = ()

    def covers_tool(self, name: str) -> bool:
        return name in self.tools

    def covers_origin(self, origin: str | None) -> bool:
        # An action with no origin is not "origin-free", it is unlabelled --
        # and an unlabelled action cannot be shown to fall inside a scope.
        return origin is not None and origin in self.origins


@dataclass(frozen=True, slots=True)
class Budget:
    """Bounds that make a grant finite. Exhaustion is a re-confirmation, not a
    failure: the user said yes to THIS much."""

    steps: int = 0
    seconds: float = 0.0
    # 0 means MAY NOT SPEND. There is no sentinel for "unlimited"; a grant that
    # can spend arbitrary money is not a grant, it is a bank account.
    spend_cents: int = 0


@dataclass(frozen=True, slots=True)
class Grant:
    """Consent to a bounded goal, given once, spendable across many steps."""

    id: str
    goal: str
    # The EXACT sentence the user heard, stored verbatim. A scope object
    # answers "what was permitted"; only this answers "what did they consent
    # to", which is the question asked six months later.
    plan_summary: str
    ceiling: RiskTier
    # How consent arrived. A grant given by voice can never answer a question
    # that exists precisely because voice is not good enough -- see channel_cap.
    granted_via: str = "voice"
    scope: GrantScope = field(default_factory=GrantScope)
    budget: Budget = field(default_factory=Budget)
    granted_at: float = 0.0
    expires_at: float = 0.0
    # Re-confirmation creates a CHILD grant rather than editing this one, so
    # the audit trail shows what was widened, when, and on what sentence.
    parent_id: str | None = None
    # Consequences the user was actually told about. A consequence discovered
    # mid-run that is not in here is new information and forces a re-confirm.
    briefed_consequences: tuple[str, ...] = ()

    @property
    def channel_cap(self) -> RiskTier:
        """The highest tier this grant could ever answer for, by channel alone.

        CONFIRM_VISUAL exists because a spoken yes is not good enough. A spoken
        grant that could pre-authorize it would defeat the tier by going around
        it, so the cap is structural rather than a policy anyone can tune.
        """
        return RiskTier.CONFIRM_VOICE if self.granted_via == "voice" else RiskTier.CONFIRM_VISUAL

    @property
    def max_satisfiable(self) -> RiskTier:
        return min(self.ceiling, self.channel_cap)


@dataclass(frozen=True, slots=True)
class Warrant:
    """Single-use authorization for ONE action, bound to that exact action.

    This is what crosses the thread boundary in place of `confirmed: bool`. A
    bare boolean is forgeable by any code that can construct one; a warrant
    carries the digest of the action it was issued for, so it cannot be reused
    for a different action or replayed after it expires.
    """

    id: str
    action_digest: str          # sha256 over the resolved action
    tier: RiskTier              # the tier this warrant actually answers
    issued_at: float
    expires_at: float
    grant_id: str | None = None      # None => answered by a live confirmation
    # "voice" | "visual" | "grant" -- carried into the audit row so the log can
    # distinguish "the user said yes" from "a grant said yes on their behalf".
    via: str = "voice"


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """A window of undo-journal entries treated as one unit of work.

    Deliberately NOT a snapshot. It records which entries belong together so a
    rollback can replay them in reverse through the existing validation. It
    does not make irreversible things reversible -- it makes the boundary
    speakable: "I put back four of the six; the other two had already moved."
    """

    id: str
    job_id: str | None = None
    entry_ids: tuple[str, ...] = ()
    created_at: float = 0.0
    # Sealed means "past here, rollback stops being a clean unwind". It does
    # not disable rollback; it changes the sentence the user hears.
    sealed: bool = False


class JobStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_CONSENT = "waiting_consent"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # Fail-closed states. A job never silently resumes across a restart, and a
    # job that could not get consent in time does not get it later by default.
    INTERRUPTED = "interrupted"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class JobProgress:
    # Written to be SPOKEN: "checking the third flight". Not a log line.
    phase: str
    step: int = 0
    steps_total: int = 0


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: str
    goal: str
    status: JobStatus
    grant_id: str | None = None
    started_at: float = 0.0
    updated_at: float = 0.0
    progress: JobProgress | None = None
    checkpoint_ids: tuple[str, ...] = ()
    # One spoken sentence for when this is reported back, minutes later.
    summary: str = ""
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Notice:
    """Something a background job wants said, queued until it is safe to say.

    There is deliberately NO "high" urgency. No background job is important
    enough to talk over a human, and providing the option guarantees something
    eventually uses it.
    """

    job_id: str
    text: str
    urgency: Literal["low", "normal"] = "normal"
    created_at: float = 0.0


# A long-running tool yields the action it WANTS to take and receives the
# result of that action back. It is a generator, so the agent structurally
# cannot invoke a tool -- it can only ask for one and be told what happened.
# That is the invariant enforced by type rather than by discipline.
StepStream = Any  # Generator[ResolvedAction, ToolResult, ToolResult]


@runtime_checkable
class LongRunningTool(Protocol):
    """Separate from `Tool`, and that separation is load-bearing.

    Adding a method to `Tool` would make `isinstance` return False for all 13
    existing tools, and ToolRegistry.register raises on that -- roughly 150
    tests die at import for one line. A long-running tool satisfies `Tool` too,
    so it still registers, routes and gates exactly like everything else.
    """

    spec: ToolSpec

    def resolve(self, **kwargs: Any) -> ResolvedAction:
        ...

    def run(self, action: ResolvedAction) -> ToolResult:
        ...

    def steps(self, action: ResolvedAction) -> StepStream:
        ...
