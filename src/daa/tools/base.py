"""Shared machinery for every tool: subprocess discipline, dry-run, undo honesty.

Three invariants live here rather than in each tool, because a tool author who
has to remember them will eventually forget one:

1. Subprocess is ALWAYS an argv list with a timeout. `shell=True` is not
   reachable from this module -- a spoken filename containing a backtick must
   never become shell syntax, and a hung `osascript` waiting on a modal dialog
   must never wedge the voice loop forever.
2. `dry_run` is enforced at the point of mutation, not at the call site. A tool
   that forgets to check it still cannot change the machine, because the only
   mutating primitive it has refuses to execute.
3. Reversibility is declared, not assumed. `mutates` / `irreversible` are read
   by tests/test_undo_coverage.py; a tool that changes the machine and returns
   no UndoAction fails the build unless it has explicitly paid for that with a
   stronger confirmation tier.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from daa.config import Settings
from daa.contracts import ResolvedAction, RiskTier, ToolResult, ToolSpec

# Generous enough for a Shortcut that opens an app, short enough that a stuck
# AppleEvent gives the user their assistant back inside one breath.
DEFAULT_TIMEOUT_S = 20.0


@dataclass(frozen=True, slots=True)
class ShellResult:
    argv: tuple[str, ...]
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    # True when dry_run suppressed a mutating command. Distinct from success:
    # callers that would report "done" must report "would have" instead.
    skipped: bool = False
    not_found: bool = False

    @property
    def ok(self) -> bool:
        return self.skipped or (self.returncode == 0 and not self.timed_out and not self.not_found)

    @property
    def failure_reason(self) -> str:
        if self.not_found:
            return f"{self.argv[0]} is not installed"
        if self.timed_out:
            return f"{self.argv[0]} timed out"
        detail = (self.stderr or self.stdout).strip().splitlines()
        return detail[-1] if detail else f"{self.argv[0]} exited {self.returncode}"

    def lines(self) -> list[str]:
        return [ln for ln in self.stdout.splitlines() if ln.strip()]


def run_argv(
    argv: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    input_text: str | None = None,
    cwd: str | None = None,
    mutating: bool = False,
    dry_run: bool = False,
) -> ShellResult:
    """The only way anything in tools/ starts a process.

    Read-only commands still run under dry_run: `resolve()` has to be able to
    list and match against the real machine or the confirmation it reads back
    would be fiction.
    """
    if isinstance(argv, str):  # the exact mistake that reintroduces shell parsing
        raise TypeError("argv must be a list of strings, never a command string")
    argv = tuple(str(a) for a in argv)
    if not argv:
        raise ValueError("argv is empty")

    if mutating and dry_run:
        return ShellResult(argv=argv, skipped=True)

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
            cwd=cwd,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ShellResult(argv=argv, returncode=-1, timed_out=True)
    except FileNotFoundError:
        return ShellResult(argv=argv, returncode=-1, not_found=True)
    except OSError as exc:  # a broken binary degrades one tool, never the loop
        return ShellResult(argv=argv, returncode=-1, stderr=str(exc))
    return ShellResult(
        argv=argv,
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


def which(binary: str) -> str | None:
    return shutil.which(binary)


class BaseTool:
    """Common plumbing. Subclasses set `spec` and implement resolve/run."""

    spec: ToolSpec
    # The spoken verb for this tool, e.g. "move to the Trash". Declared once on
    # the class so that EVERY path through resolve() -- including the ones that
    # resolve to no target at all -- reads back as something that happens to a
    # thing, rather than as a bare noun phrase. A verb-less readback makes
    # "report.pdf - should I go ahead?" mean both "reveal this" and "delete
    # this", so tests/test_tools_registry.py fails the build for an empty one.
    verb: str = ""
    # Does run() change anything outside this process?
    mutates: bool = False
    # Set only when no honest inverse exists (arbitrary user code). Costs the
    # tool a stronger confirmation tier -- see test_undo_coverage.
    irreversible: bool = False

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings

    @property
    def settings(self) -> Settings:
        # Lazy so that importing daa.tools never reads .env -- registration at
        # import time must not have side effects a test cannot control.
        if self._settings is None:
            self._settings = Settings.load()
        return self._settings

    @property
    def dry_run(self) -> bool:
        return bool(self.settings.dry_run)

    # -- helpers ---------------------------------------------------------

    def action(
        self,
        *,
        targets: Sequence[str] = (),
        explicit: bool = True,
        verb: str = "",
        consequences: Mapping[str, str] | None = None,
        **args: Any,
    ) -> ResolvedAction:
        """Build the ResolvedAction the gate and the readback both run on.

        `consequences` is not decoration: anything a resolver COMPUTES that
        changes what a yes means -- a file that will be replaced, 400 items
        inside a folder, the body of a message about to be sent -- belongs
        here, because this is the only field that reaches the sentence the
        user answers. Leaving it in `args` alone means the confirmation for a
        destructive call is byte-identical to the safe one.
        """
        return ResolvedAction(
            tool=self.spec.name,
            args=args,
            targets=tuple(targets),
            explicit=explicit,
            verb=verb or self.verb,
            consequences=dict(consequences or {}),
        )

    def failed(self, summary: str, error: str, **data: Any) -> ToolResult:
        return ToolResult(ok=False, summary=summary, data=data, error=error)

    def dry(self, summary: str, **data: Any) -> ToolResult:
        """A dry run reports intent, never completion, and never fakes an undo."""
        return ToolResult(ok=True, summary=summary, data={"dry_run": True, **data})

    def resolve(self, **kwargs: Any) -> ResolvedAction:  # pragma: no cover - abstract
        raise NotImplementedError

    def run(self, action: ResolvedAction) -> ToolResult:  # pragma: no cover - abstract
        raise NotImplementedError


class ShellTool(BaseTool):
    """A BaseTool that shells out. Carries dry_run into every mutating call."""

    # Subclasses that shell out to a single binary name it here so a missing
    # binary produces a spoken sentence instead of a traceback.
    binary: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S

    def sh(
        self,
        argv: Sequence[str],
        *,
        mutating: bool,
        timeout: float | None = None,
        input_text: str | None = None,
        cwd: str | None = None,
    ) -> ShellResult:
        return run_argv(
            argv,
            timeout=self.timeout_s if timeout is None else timeout,
            input_text=input_text,
            cwd=cwd,
            mutating=mutating,
            dry_run=self.dry_run,
        )


def spec(
    name: str,
    description: str,
    params: Mapping[str, Any] | None = None,
    *,
    floor: RiskTier = RiskTier.ANNOUNCE,
    activation_hint: str = "",
    tags: Sequence[str] = (),
    inverses: Sequence[str] = (),
) -> ToolSpec:
    """`inverses` is an allowlist, so it is spelled out even when it is empty.

    The undo journal is a world-readable file: anything running as the user can
    append a row naming any tool it likes. A tool that returns an UndoAction
    names here, in reviewed source, the tools that row is allowed to invoke --
    and a tool with no inverse says `()` out loud rather than by omission.
    """
    return ToolSpec(
        name=name,
        description=description,
        params=dict(params or {}),
        floor=floor,
        activation_hint=" ".join(activation_hint.split()),
        tags=tuple(tags),
        inverses=tuple(inverses),
    )


def param(type_: str, desc: str, *, required: bool = False, **extra: Any) -> dict[str, Any]:
    return {"type": type_, "description": desc, "required": required, **extra}


@dataclass(frozen=True, slots=True)
class Degraded:
    """A capability that is missing rather than broken.

    Returned instead of raised so that one revoked TCC grant costs exactly one
    tool. The `remedy` is written to be spoken to the user.
    """

    capability: str
    remedy: str
    detail: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)

    def as_result(self, summary: str) -> ToolResult:
        return ToolResult(
            ok=False,
            summary=summary,
            data={"degraded": self.capability, "remedy": self.remedy, **dict(self.data)},
            error=self.detail or f"{self.capability} unavailable",
        )


# ---------------------------------------------------------------------------
# Fuzzy resolution
# ---------------------------------------------------------------------------

_SPOKEN_NOISE = {"app", "the", "application", "please", "my"}


def normalize_spoken(text: str) -> str:
    """Fold a dictated name toward its on-disk form.

    Speech-to-text gives back 'the Visual Studio Code app' and punctuation it
    invented. Matching on the raw string is how `resolve()` picks the wrong
    target, which is the single failure mode this whole two-phase design exists
    to catch.
    """
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in text)
    words = [w for w in cleaned.split() if w and w not in _SPOKEN_NOISE]
    return " ".join(words)


def fuzzy_score(query: str, candidate: str) -> float:
    """0..1. Exact > prefix > word-subset > character similarity."""
    from difflib import SequenceMatcher

    q, c = normalize_spoken(query), normalize_spoken(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    ratio = SequenceMatcher(None, q, c).ratio()
    q_words, c_words = set(q.split()), set(c.split())
    if c.startswith(q) or q.startswith(c):
        return max(ratio, 0.92)
    if q_words and q_words <= c_words:
        return max(ratio, 0.88)
    if q in c or c in q:
        return max(ratio, 0.8)
    overlap = len(q_words & c_words) / len(q_words | c_words) if (q_words | c_words) else 0.0
    return max(ratio, overlap * 0.75)


def rank_candidates(
    query: str, candidates: Sequence[Any], key=lambda c: c, *, limit: int = 5
) -> list[tuple[float, Any]]:
    """Every candidate above the noise floor, best first.

    Callers hand the runner-up list to the user, because the honest answer to an
    ambiguous name is "I found three -- which?", not a coin flip.
    """
    scored = [(fuzzy_score(query, str(key(c))), c) for c in candidates]
    scored = [pair for pair in scored if pair[0] >= 0.34]
    scored.sort(key=lambda pair: (-pair[0], str(key(pair[1])).lower()))
    return scored[:limit]


# ---------------------------------------------------------------------------
# Speech hygiene
# ---------------------------------------------------------------------------

_UNSPEAKABLE = ("/", "\\", "://", "http", "\t")


def spoken_snippet(text: str, limit: int = 80) -> str | None:
    """A fragment safe to read aloud, or None.

    ToolResult.summary is synthesised into speech. A path, a URL or a uuid read
    character by character is worse than useless -- it is unbearable. When the
    content cannot be spoken, callers say how much of it there is instead and
    leave the content itself in `data`, where the conversational layer can
    decide what to do with it.
    """
    flat = " ".join(str(text).split())
    if not flat or len(flat) > limit:
        return None
    lowered = flat.lower()
    if any(mark in lowered for mark in _UNSPEAKABLE):
        return None
    # Long unbroken alphanumeric runs are identifiers, not words.
    if any(len(word) > 18 and any(ch.isdigit() for ch in word) for word in flat.split()):
        return None
    return flat


def as_sentence(text: str) -> str:
    """Capitalised, terminated. Every summary is a sentence someone hears."""
    flat = " ".join(str(text).split())
    if not flat:
        return ""
    if not flat.endswith((".", "?", "!")):
        flat += "."
    return flat[0].upper() + flat[1:]
