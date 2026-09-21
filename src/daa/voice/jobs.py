"""Background work: the registry, the notice board, and the step pump.

THE ONE ARCHITECTURAL RULE IN THIS FILE
---------------------------------------
**The agent thread proposes; the loop thread disposes.**

A long-running tool is a generator. It YIELDS the action it wants and receives
the result back. It cannot call a tool, because there is no API surface through
which it could: `JobRunner` is the only thing holding the callbacks, and the
authorization callback runs on the loop thread while the execution callback is
`VoiceLoop._execute` -- still, literally, the only caller of `tool.run`.

Why execution runs on the worker and authorization does not:

1. **The loop thread must stay free to hear "stop."** A revocation that can
   only be processed after a 20-second `osascript` timeout is not a
   revocation. This is the deciding argument and everything else is downstream
   of it.
2. Authorization is pure computation plus, rarely, a prompt. Serializing it
   costs nothing and preserves the single-authority property exactly.
3. Prompts must happen on the thread that owns the mic, the scripted-reply
   queue and the console. Two threads calling `console.ask()` is a bug you
   cannot test your way out of.

NOTHING HERE SPEAKS
-------------------
A background job never talks. It posts a `Notice` and the loop drains notices
at a safe moment. There is deliberately no "high" urgency: no background job is
important enough to talk over a human being, and providing the option
guarantees something eventually uses it.

WHAT SURVIVES A RESTART: THE RECORD, NEVER THE RUN
--------------------------------------------------
`~/.daa/jobs.jsonl` is written through the same `safety/store.py` as the undo
journal, so it inherits 0600 inside 0700 and the same append-only tombstone
shape. On load, any row still claiming to be live is rewritten INTERRUPTED. A
job does not resume: a browser agent that wakes up mid-checkout in a world that
moved on is the worst thing this design could produce, and resuming would need
re-consent anyway because the grant has expired -- at which point it is a new
job.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from daa.contracts import (
    Disposition,
    JobProgress,
    JobRecord,
    JobStatus,
    Notice,
    ResolvedAction,
    ToolResult,
    Warrant,
)
from daa.safety.store import harden_dir, harden_file, open_append, secure_dir

__all__ = [
    "DEFAULT_JOBS_PATH",
    "LIVE_STATUSES",
    "MAX_PENDING_NOTICES",
    "JobFinished",
    "JobOutcome",
    "JobRegistry",
    "JobRunner",
    "NoticeBoard",
    "StepRequest",
    "StepVerdict",
]

DEFAULT_JOBS_PATH = Path.home() / ".daa" / "jobs.jsonl"

# Statuses that mean "this job believes it is still going". A row in one of
# these at load time did not end; the process died under it.
LIVE_STATUSES = frozenset(
    {JobStatus.PENDING, JobStatus.RUNNING, JobStatus.WAITING_CONSENT, JobStatus.PAUSED}
)

# Pending notices are capped and coalesced. Two announcements 200ms apart is
# worse than one sentence that mentions both.
MAX_PENDING_NOTICES = 3


# ---------------------------------------------------------------------------
# What crosses the thread boundary
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StepVerdict:
    """The loop thread's answer to one proposed step.

    `warrant is None` means the step may not run, and `status` is what the job
    becomes as a result. Deliberately not an exception: a refusal is an
    ordinary outcome here, and a job that ends because the user said no is not
    a job that crashed.
    """

    warrant: Warrant | None = None
    tool: Any = None
    disposition: Disposition | None = None
    reason: str = ""
    via: str = ""
    status: JobStatus = JobStatus.CANCELLED
    checkpoint_id: str | None = None


@dataclass(frozen=True, slots=True)
class StepRequest:
    """A worker asking the loop thread for permission, and where to answer."""

    job_id: str
    step_index: int
    action: ResolvedAction
    reply: queue.Queue[StepVerdict]


@dataclass(frozen=True, slots=True)
class JobFinished:
    """Posted on the SAME queue as the requests, so a loop that is draining
    that queue learns a job ended without polling for it. One queue, every
    event on it, no sleeps anywhere: that is what makes the threaded tests
    deterministic rather than merely usually-green."""

    job_id: str
    status: JobStatus
    summary: str = ""
    error: str | None = None


@dataclass(slots=True)
class JobOutcome:
    """What `TurnOutcome` is for a turn, for a job.

    Separate on purpose. `TurnOutcome.ran_anything` and `handled_calls` are
    read by the loop to decide whether to speak the model's sentence; threading
    job results through them would make both lie.
    """

    job_id: str = ""
    results: list[ToolResult] = field(default_factory=list)
    dispositions: list[Disposition] = field(default_factory=list)
    spoken: list[str] = field(default_factory=list)
    handled_calls: int = 0
    steps: int = 0


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


class JobRegistry:
    """Job records, in memory and on disk. Thread-safe by internal lock.

    The lock is internal for the same reason `UndoJournal`'s is: the worker
    thread posts progress while the loop thread reads status to decide whether
    it is safe to speak, and a lock the caller has to remember is a lock the
    second caller forgets.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        now: Callable[[], float] = time.time,
        max_jobs: int = 1,
    ) -> None:
        # Two agents driving the same GUI is not a concurrency problem, it is a
        # correctness problem. A second request while one is live is a
        # question, not a queue entry.
        self.path = Path(path) if path is not None else DEFAULT_JOBS_PATH
        self.max_jobs = max(1, int(max_jobs))
        self._now = now
        self._lock = threading.RLock()
        self._jobs: dict[str, JobRecord] = {}
        self._cancels: dict[str, threading.Event] = {}
        self.interrupted: tuple[str, ...] = ()
        self.reload()

    # --- lifecycle --------------------------------------------------------

    def create(self, *, goal: str, grant_id: str | None = None) -> JobRecord:
        with self._lock:
            if len(self.live()) >= self.max_jobs:
                raise RuntimeError("a job is already running")
            at = float(self._now())
            record = JobRecord(
                id=uuid.uuid4().hex[:12],
                goal=goal,
                status=JobStatus.PENDING,
                grant_id=grant_id,
                started_at=at,
                updated_at=at,
            )
            self._jobs[record.id] = record
            self._cancels[record.id] = threading.Event()
            self._append(_job_row(record))
            return record

    def update(
        self,
        job_id: str,
        *,
        status: JobStatus | None = None,
        progress: JobProgress | None = None,
        summary: str | None = None,
        error: str | None = None,
        checkpoint_id: str | None = None,
    ) -> JobRecord | None:
        with self._lock:
            current = self._jobs.get(job_id)
            if current is None:
                return None
            ids = current.checkpoint_ids
            if checkpoint_id and checkpoint_id not in ids:
                ids = (*ids, checkpoint_id)
            updated = JobRecord(
                id=current.id,
                goal=current.goal,
                status=current.status if status is None else status,
                grant_id=current.grant_id,
                started_at=current.started_at,
                updated_at=float(self._now()),
                progress=current.progress if progress is None else progress,
                checkpoint_ids=ids,
                summary=current.summary if summary is None else summary,
                error=current.error if error is None else error,
            )
            self._jobs[job_id] = updated
            self._append(_job_row(updated))
            return updated

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[JobRecord]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.started_at)

    def live(self) -> list[JobRecord]:
        with self._lock:
            return [j for j in self._jobs.values() if j.status in LIVE_STATUSES]

    def any_live(self) -> bool:
        return bool(self.live())

    # --- revocation -------------------------------------------------------

    def cancel_event(self, job_id: str) -> threading.Event:
        with self._lock:
            event = self._cancels.get(job_id)
            if event is None:
                event = threading.Event()
                self._cancels[job_id] = event
            return event

    def cancelled(self, job_id: str) -> bool:
        return self.cancel_event(job_id).is_set()

    def revoke(self, job_id: str, reason: str = "") -> bool:
        """Idempotent, and it always succeeds.

        Note what this does NOT do: it does not roll anything back. Undoing on
        revocation would make "stop" more destructive than letting it finish,
        which is precisely backwards. The runner exits between steps, daa says
        what it had managed, and offers the rollback as a separate question.
        """
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return False
            self.cancel_event(job_id).set()
            if record.status in LIVE_STATUSES:
                self.update(job_id, status=JobStatus.CANCELLED, summary=reason or "Stopped.")
            return True

    def revoke_all(self, reason: str = "") -> list[str]:
        with self._lock:
            ids = [j.id for j in self.live()]
            for job_id in ids:
                self.revoke(job_id, reason)
            return ids

    # --- persistence ------------------------------------------------------

    def reload(self) -> None:
        """Read the file, then rewrite anything still claiming to be live.

        This is the only place `INTERRUPTED` is written. It is not an error
        state and it is not a retry queue: it is the record that something was
        half-done when the process died, so that at the next wake daa can say
        so ONCE and offer to put it back.
        """
        with self._lock:
            self._jobs = {}
            self.interrupted = ()
            if self.path.exists():
                harden_dir(self.path.parent)
                harden_file(self.path)
                with self.path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        row = _parse(line)
                        if row is not None:
                            self._jobs[row.id] = row
            stranded = [j for j in self._jobs.values() if j.status in LIVE_STATUSES]
            self.interrupted = tuple(j.id for j in stranded)
            for job in stranded:
                self.update(job.id, status=JobStatus.INTERRUPTED)

    def _append(self, row: Mapping[str, Any]) -> None:
        # Through safety/store.py, so the file is 0600 inside a 0700 directory
        # from the first byte. It holds goals in the user's own words, which is
        # the same class of thing the undo journal holds.
        try:
            secure_dir(self.path.parent)
            line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            with open_append(self.path) as fh:
                fh.write(line)
                fh.flush()
        except OSError:
            # A job record is an observer. A full disk must not be able to stop
            # a job that is already running, or prevent it being cancelled.
            return


def _job_row(record: JobRecord) -> dict[str, Any]:
    progress = record.progress
    return {
        "kind": "job",
        "id": record.id,
        "goal": record.goal,
        "status": str(record.status),
        "grant_id": record.grant_id,
        "started_at": record.started_at,
        "updated_at": record.updated_at,
        "phase": getattr(progress, "phase", "") if progress else "",
        "step": getattr(progress, "step", 0) if progress else 0,
        "steps_total": getattr(progress, "steps_total", 0) if progress else 0,
        "checkpoint_ids": list(record.checkpoint_ids),
        "summary": record.summary,
        "error": record.error,
    }


def _parse(line: str) -> JobRecord | None:
    """One row of an untrusted file. Anything malformed is dropped, never
    defaulted into something permissive -- an unreadable status becomes
    INTERRUPTED, not RUNNING."""
    text = line.strip()
    if not text:
        return None
    try:
        row = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(row, Mapping) or row.get("kind") != "job":
        return None
    job_id = row.get("id")
    if not isinstance(job_id, str) or not job_id:
        return None
    try:
        status = JobStatus(str(row.get("status")))
    except ValueError:
        status = JobStatus.INTERRUPTED
    phase = row.get("phase")
    progress = (
        JobProgress(
            phase=str(phase),
            step=_int(row.get("step")),
            steps_total=_int(row.get("steps_total")),
        )
        if isinstance(phase, str) and phase
        else None
    )
    return JobRecord(
        id=job_id,
        goal=str(row.get("goal") or ""),
        status=status,
        grant_id=row.get("grant_id") if isinstance(row.get("grant_id"), str) else None,
        started_at=_float(row.get("started_at")),
        updated_at=_float(row.get("updated_at")),
        progress=progress,
        checkpoint_ids=tuple(
            str(c) for c in (row.get("checkpoint_ids") or []) if isinstance(c, str)
        ),
        summary=str(row.get("summary") or ""),
        error=row.get("error") if isinstance(row.get("error"), str) else None,
    )


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Notices
# ---------------------------------------------------------------------------


class NoticeBoard:
    """Things a job wants said, held until it is safe to say them.

    Drained ONLY at explicit safe moments, which the loop chooses:

      1. Piggyback -- right after daa finishes speaking in a turn the user
         addressed to it. Free, and where 90% of notices should land.
      2. Silence -- no speech at all for `quiet_s`. The only room state daa can
         be SURE it is not interrupting.
      3. Never on an utterance the gate judged not-addressed. That is another
         person talking, and speaking into it is exactly the failure the
         address gate exists to prevent.

    Notices consume the gate's output; they never bypass it.
    """

    def __init__(
        self,
        *,
        now: Callable[[], float] = time.time,
        ttl_s: float = 300.0,
        cap: int = MAX_PENDING_NOTICES,
    ) -> None:
        self._now = now
        self.ttl_s = float(ttl_s)
        self.cap = max(1, int(cap))
        self._lock = threading.RLock()
        self._pending: list[Notice] = []
        self.dropped = 0

    def post(self, job_id: str, text: str, *, urgency: str = "normal") -> Notice:
        notice = Notice(
            job_id=job_id,
            text=text,
            urgency="low" if urgency == "low" else "normal",
            created_at=float(self._now()),
        )
        with self._lock:
            self._pending.append(notice)
            while len(self._pending) > self.cap:
                # Drop the OLDEST. A stale notice is the one least worth
                # saying, and the count is spoken so nothing vanishes silently.
                self._pending.pop(0)
                self.dropped += 1
        return notice

    def pending(self) -> list[Notice]:
        with self._lock:
            return list(self._pending)

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()
            self.dropped = 0

    def drain(self) -> tuple[str, list[Notice], int]:
        """Everything sayable right now, as ONE spoken line.

        Returns (text, spoken, expired). Expired notices are DROPPED, never
        spoken late: "that download finished" is false twenty minutes later in
        a way that is worse than silence.
        """
        now = float(self._now())
        with self._lock:
            fresh = [n for n in self._pending if now - n.created_at < self.ttl_s]
            expired = len(self._pending) - len(fresh)
            dropped = self.dropped
            self._pending.clear()
            self.dropped = 0
        if not fresh:
            return "", [], expired
        texts = [str(n.text).strip().rstrip(".") for n in fresh if str(n.text).strip()]
        if not texts:
            return "", [], expired
        line = texts[0] + "."
        for extra in texts[1:]:
            line += f" Also, {extra}."
        if dropped:
            line += f" And {dropped} other thing{'s' if dropped > 1 else ''} finished."
        return line, fresh, expired


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


class JobRunner:
    """Drives one `StepStream` to its end, one gated step at a time.

    The generator is the agent. This class never decides anything: it asks
    (`ask`), it carries out what it is told (`perform`), and it stops when told
    to. Every judgment in the sequence belongs to the loop thread.
    """

    def __init__(
        self,
        *,
        job_id: str,
        stream: Any,
        ask: Callable[[StepRequest], StepVerdict],
        perform: Callable[[ResolvedAction, StepVerdict, int], ToolResult],
        registry: JobRegistry,
        cancel: threading.Event,
        notices: NoticeBoard | None = None,
        finished: Callable[[JobFinished], None] | None = None,
        max_steps: int = 64,
        phase: Callable[[ResolvedAction], str] | None = None,
    ) -> None:
        self.job_id = job_id
        self.stream = stream
        self._ask = ask
        self._perform = perform
        self.registry = registry
        self.cancel = cancel
        self.notices = notices
        self._finished = finished
        self.max_steps = max(1, int(max_steps))
        self._phase = phase
        self.outcome = JobOutcome(job_id=job_id)
        self.done = threading.Event()
        self.status = JobStatus.PENDING
        self.summary = ""
        self.error: str | None = None

    # `drive`, not `run`: this class must never own a method name that reads
    # like invoking a tool, because the package-wide chokepoint test is an AST
    # pass over `<receiver>.run(...)` and a runner called `runner.run()` would
    # be indistinguishable from the thing it exists not to be.
    def drive(self) -> JobStatus:
        self.registry.update(self.job_id, status=JobStatus.RUNNING)
        self.status = JobStatus.RUNNING
        result: ToolResult | None = None
        index = 0
        try:
            while True:
                if self.cancel.is_set():
                    self._end(JobStatus.CANCELLED, "Stopped. ")
                    break
                if index >= self.max_steps:
                    self._end(JobStatus.EXPIRED, "That took more steps than I had.")
                    break
                try:
                    action = self.stream.send(result)
                except StopIteration as stop:
                    final = stop.value
                    self._end(
                        JobStatus.DONE,
                        getattr(final, "summary", "") or "Done.",
                    )
                    break
                index += 1
                self.outcome.steps = index
                if not isinstance(action, ResolvedAction):
                    self._end(JobStatus.FAILED, "", error="the agent proposed something odd")
                    break
                self.registry.update(
                    self.job_id,
                    progress=JobProgress(
                        phase=self._phase(action) if self._phase else _phase_of(action),
                        step=index,
                        steps_total=self.max_steps,
                    ),
                )
                verdict = self._ask(
                    StepRequest(
                        job_id=self.job_id,
                        step_index=index,
                        action=action,
                        reply=queue.Queue(maxsize=1),
                    )
                )
                if verdict.warrant is None:
                    self._end(verdict.status, verdict.reason)
                    break
                # THE EXECUTION, on this thread, through the loop's one
                # chokepoint. Everything slow happens here and the loop thread
                # is free the whole time.
                result = self._perform(action, verdict, index)
                self.outcome.results.append(result)
                if verdict.disposition is not None:
                    self.outcome.dispositions.append(verdict.disposition)
        except Exception as exc:  # noqa: BLE001 -- a generator is third-party code
            # An agent that raises must not take the loop down with it, and it
            # must not look like a success. The user hears one sentence.
            self._end(JobStatus.FAILED, "That didn't work out.", error=str(exc))
        finally:
            self.done.set()
        return self.status

    def _end(self, status: JobStatus, summary: str, *, error: str | None = None) -> None:
        self.status = status
        self.summary = summary
        self.error = error
        self.registry.update(self.job_id, status=status, summary=summary, error=error)
        if self.notices is not None and summary:
            self.notices.post(self.job_id, summary)
        if self._finished is not None:
            self._finished(
                JobFinished(job_id=self.job_id, status=status, summary=summary, error=error)
            )


def _phase_of(action: ResolvedAction) -> str:
    """A SPOKEN fragment, by the same rules as `ToolResult.summary`: no paths,
    no ids, no markdown. It is what the dock UI shows and what daa would say if
    asked "what are you doing?" -- never a log line."""
    verb = str(getattr(action, "verb", "") or "").strip()
    if verb:
        return verb
    return str(action.tool).replace("_", " ")


# ---------------------------------------------------------------------------
# Channels: how a step request reaches the loop thread
# ---------------------------------------------------------------------------


class InlineChannel:
    """Same thread, no queue. The seam that makes every job test single
    threaded and therefore deterministic.

    It is not a mock: the ordering it produces (propose, authorize, execute,
    repeat) is exactly the ordering the threaded channel produces, because the
    threaded one blocks the worker for the whole authorization. The only thing
    it removes is the handoff.
    """

    def __init__(self, authorize: Callable[[StepRequest], StepVerdict]) -> None:
        self._authorize = authorize
        self.finished: list[JobFinished] = []

    def ask(self, request: StepRequest) -> StepVerdict:
        return self._authorize(request)

    def finish(self, event: JobFinished) -> None:
        self.finished.append(event)


class QueueChannel:
    """One queue, every event on it -- requests AND the finish notification.

    That last part is what makes the threaded tests deterministic. A loop that
    drains this queue blocks until something happens and is woken by the job
    ending, so there is no polling interval to tune, no sleep to flake, and no
    window in which the test decides the job is finished before it is.
    """

    def __init__(self, events: queue.Queue[Any], *, timeout_s: float = 30.0) -> None:
        self.events = events
        self.timeout_s = float(timeout_s)

    def ask(self, request: StepRequest) -> StepVerdict:
        self.events.put(request)
        try:
            return request.reply.get(timeout=self.timeout_s)
        except queue.Empty:
            # The loop stopped draining. Fail closed and say so: a worker that
            # assumed permission because nobody answered is the single worst
            # bug this file could contain.
            return StepVerdict(
                reason="I lost track of that one, so I stopped.",
                status=JobStatus.EXPIRED,
            )

    def finish(self, event: JobFinished) -> None:
        self.events.put(event)


def start_thread(runner: JobRunner, *, name: str = "daa-job") -> threading.Thread:
    """Spawn the worker. Daemon, because a background job must never be the
    reason the process will not exit."""
    thread = threading.Thread(target=runner.drive, name=f"{name}-{runner.job_id}", daemon=True)
    thread.start()
    return thread


def coalesce(records: Sequence[JobRecord]) -> str:
    """One spoken line about N interrupted jobs, said ONCE at the next wake."""
    live = [r for r in records if r.status is JobStatus.INTERRUPTED]
    if not live:
        return ""
    if len(live) == 1:
        goal = str(live[0].goal or "something").strip().rstrip(".")
        return f"I was part way through {goal} when I stopped."
    return f"I was part way through {len(live)} things when I stopped."
