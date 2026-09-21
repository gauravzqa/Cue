# Scoped grants, async execution, and checkpoints

**Status:** design, not implemented. Nothing under `src/` or `tests/` changed.
**Owns:** the consent primitive and the execution model that computer-use,
browser-use and the dock UI all have to fit inside.

---

## Summary

The safety model is not broken by async work; it is broken by a conflation.
Today the **unit of consent** and the **unit of execution** are the same object
(`ResolvedAction`), which is why a 40-step browser flow has nothing to consent
to and 30 stacked inverses have nothing to undo. This design splits them. The
unit of execution stays exactly what it is — one `ResolvedAction`, resolved,
assessed, gated by `policy.decide()`, run by `_execute`, recorded in the
`UndoJournal`. **Every single step of every agent run still goes through that
path, unchanged.** What changes is the unit of consent: a new `Grant` — a goal,
a plan readback, a `RiskTier` ceiling, an allow-list of tools and origins, and
step / wall-clock / spend budgets — which does exactly one thing: it *supplies
the answer to a confirmation that policy has already demanded*, for steps that
fall inside it. A grant is never an input to `tier = max(spec.floor, derived)`;
it is consulted strictly after, and it can only ever answer a tier that the
channel it was granted on could itself have answered. Execution moves onto a
worker thread, but **authorization does not**: the agent thread proposes a
`ResolvedAction` and blocks; the loop thread computes the disposition, consults
the grant, prompts if it must, and hands back a single-use, short-lived,
action-bound `Warrant`. Long-running tools are a *separate* protocol
(`LongRunningTool`) shaped as a generator that yields actions and receives
results, so the existing `Tool` protocol — and therefore all 13 tools and every
`isinstance(tool, Tool)` registration — is untouched. Undo composes not by
stacking but by **checkpointing**: a checkpoint is a window of journal entry ids
plus fingerprints, rolled back as N individually re-validated inverses that stop
honestly at the first stale one, and *sealed* the moment something irreversible
happens inside it. Irreversible operations are never grant-satisfiable, at any
ceiling, ever.

---

## A. The scoped grant

### A.0 The one sentence this whole section defends

```
tier      = max(spec.floor, derived)     # unchanged. The grant is NOT an input.
satisfied = grant_satisfies(grant, tier, action, spec, assessment)
```

`policy.decide()` keeps its exact signature, its purity, and its 52,800-case
proof. The grant lives in a **new, equally pure module** `safety/grant.py` that
is called by the loop *after* `decide()` returns. Policy decides *what must
happen before this runs*. The grant decides *whether the user already did that
thing*. Those are different questions and they must not share a function.

### A.1 What is in a grant

```python
@dataclass(frozen=True, slots=True)
class GrantScope:
    """Where a grant is allowed to reach. Everything is an ALLOW-list."""
    tools: frozenset[str] = frozenset()      # exact ToolSpec.name values
    origins: frozenset[str] = frozenset()    # "https://mail.google.com" — scheme+host, no path
    apps: frozenset[str] = frozenset()       # bundle ids: "com.apple.Safari"
    path_prefixes: tuple[str, ...] = ()      # absolute, expanded, no globs
    # Empty set means EMPTY, never "any". A grant with no tools covers nothing.
    # This is the single most important line in the type: the permissive
    # reading of an empty allow-list is how every scoped-permission system
    # eventually becomes a blanket one.

@dataclass(frozen=True, slots=True)
class Budget:
    steps: int = 0
    wall_clock_s: float = 0.0
    spend_cents: int = 0          # 0 means MAY NOT SPEND. There is no "unlimited".
    def exhausted(self, *, steps: int, elapsed_s: float, spent_cents: int) -> bool: ...

@dataclass(frozen=True, slots=True)
class Grant:
    id: str
    goal: str                 # the user's own words, verbatim where possible
    plan_summary: str         # what daa SAID it would do — this is the readback
    ceiling: RiskTier         # the highest tier a step may reach and stay covered
    granted_via: Literal["voice", "visual"]   # the CHANNEL consent arrived on
    scope: GrantScope
    budget: Budget
    issued_at: float
    expires_at: float
    # A mid-run re-confirmation CHAINS. It never edits or replaces its parent,
    # so the audit log can always reconstruct the original bargain separately
    # from every widening of it.
    parent_id: str | None = None
    # Consequence classes the readback actually named ("overwrite", "leaves the
    # Trash"). A step whose ResolvedAction.consequences contains a key that is
    # NOT in here was not described at consent time, so it re-confirms.
    briefed_consequences: frozenset[str] = frozenset()

    @property
    def channel_cap(self) -> RiskTier:
        """A grant can never satisfy a confirmation stronger than the channel
        it arrived on. A spoken yes does not become a typed yes by being
        made in advance and for a wider scope — it becomes weaker, not
        stronger, because it was made with less information."""
        return (RiskTier.CONFIRM_VISUAL if self.granted_via == "visual"
                else RiskTier.CONFIRM_VOICE)

    @property
    def max_satisfiable(self) -> RiskTier:
        return RiskTier(min(int(self.ceiling), int(self.channel_cap)))

    def describe(self) -> str: ...   # see A.2
```

`Grant` is **frozen**. Consumption lives beside it in a mutable `GrantState`
(steps used, cents spent, first-seen origins, `revoked_at`) owned by
`safety/grants.py`, exactly the way `UndoJournal` owns `UndoEntry`. Freezing the
bargain and mutating only the meter is what makes "who authorized what" a
reconstructible fact rather than a last-write-wins field.

### A.2 How it is spoken, and shown

Same honesty bar as `ResolvedAction.describe()`: **verb-first, consequences
named, and it must not be able to lie by omission.** A grant readback has four
required clauses, and I want a test that asserts all four are present for every
grant daa issues — the same way `test_tools_readback` (61 tests) pins the
action readback today.

1. **The goal, in the user's words.** Not daa's paraphrase. If daa has to
   paraphrase, it says so ("I think you mean…").
2. **The plan — concrete, and naming the most destructive thing in scope, not
   the average thing.** "I'll open the three invoices and rename them" is a lie
   of framing if the rename can overwrite. This is the direct analogue of
   `consequences` being appended verbatim.
3. **The bounds, in units a person has.** Steps *and* time *and* place:
   "about twenty steps, five minutes, only on mail.google.com."
4. **The stop line, and what will still interrupt.** "I'll stop and ask before
   I send anything. Say stop any time."

A fifth clause is required whenever it is true, and it is the clause that makes
this design honest rather than clever:

5. **What the grant cannot promise.** "I can't see the page until I'm in it, so
   I don't know exactly what I'll be clicking." An agent grant that fabricates
   `targets` it does not have is `ResolvedAction` with a made-up verb. Say the
   gap out loud.

Spoken form:

> "I'll clear out the downloads folder like you asked. That means looking at
> everything older than a month and moving it to the Trash — about forty files,
> and some of them I'll have to guess about. Only in Downloads, up to sixty
> steps or three minutes. I'll stop and ask before anything leaves the Trash
> for good. Say stop any time. Okay?"

Visual form (`granted_via="visual"` — required for any ceiling of
`CONFIRM_VISUAL`), reusing `_visual_detail`'s discipline of printing
*everything*: goal; plan; each allowed tool by its human `description`, not its
name; every allowed origin and app; all three budgets; the ceiling rendered in
plain English; **the explicit list of things that will still be asked about
individually despite this grant**; and the wall-clock expiry time. The dock UI
sibling should render this card and nothing less — a prettier card that shows
less is a downgrade of the tier.

The dock UI must also show a **live** grant: goal, steps used of budget, time
left, and a stop control that does not require speech. That is not a nicety; it
is the only revocation path that works while daa is talking.

### A.3 The satisfaction rule, precisely

This is the part that usually rots into blanket permission, so it is written as
one function with no branches that can widen:

```python
def grant_satisfies(
    grant: Grant, state: GrantState, action: ResolvedAction,
    spec: ToolSpec, disposition: Disposition, now: float,
) -> tuple[bool, str]:
    """True only if EVERY clause holds. The reason string is SPOKEN."""
```

Clauses, all conjunctive, all fail-closed:

| # | Clause | Why |
|---|---|---|
| 1 | `state.revoked_at is None` | "stop" means stop |
| 2 | `now < grant.expires_at` | consent decays; a 40-minute-old yes is not a yes |
| 3 | `disposition.tier <= grant.max_satisfiable` | ceiling **and** channel cap |
| 4 | `disposition.tier is not RiskTier.REFUSE` | REFUSE is never satisfiable, by anything |
| 5 | `spec.grantable` | human-authored, reviewed, static (§C.2) |
| 6 | `"irreversible" not in spec.tags` | belt and braces on 5 |
| 7 | `assessment.unrecoverable <= UNRECOVERABLE` | runtime **raise** — always allowed |
| 8 | `spec.name in grant.scope.tools` | allow-list, empty means empty |
| 9 | `action.origin is None or action.origin in grant.scope.origins` | new origin ⇒ re-confirm |
| 10 | every path in `action` under some `scope.path_prefixes` | keeps a file agent in its lane |
| 11 | `set(action.consequences) <= grant.briefed_consequences` | an undescribed consequence was never consented to |
| 12 | `not grant.budget.exhausted(...)` | including `spend_cents == 0` ⇒ any spend re-confirms |

Note what clause 3 does **not** do: it does not change `disposition.tier`. The
tier that was computed is the tier that is logged, the tier the warrant carries,
and the tier the audit log reports. A step that ran at CONFIRM_VOICE under a
grant is recorded as a CONFIRM_VOICE step whose confirmation arrived
`via="grant"`. There is no tier in this system that means "was skipped."

**Why this is not a floor lowering, stated for the record.** `spec.floor` says
*this tool's reach always warrants at least this much care*. A grant does not
touch it: a `CONFIRM_VOICE`-floored tool inside a grant is still assessed at
`CONFIRM_VOICE` or above, still produces a `disposition`, still produces a
`confirmation` audit event, and still cannot run without an authorization token.
What the grant changes is only **where that confirmation's "yes" came from** —
from a specific, bounded, time-limited, logged, revocable sentence the user said
about this exact class of work, instead of from a fresh prompt. The user did not
consent to less; they consented earlier, to more at once, with the bound written
down. The three things that make that defensible are: the channel cap (clause 3)
so a spoken grant can never answer a typed question; the static `grantable` list
(clause 5) so a whole category is outside the mechanism; and the fact that a
grant with an empty scope covers nothing (`GrantScope` defaults).

### A.4 What forces a re-confirmation mid-run

Any failed clause above. Additionally, one Jev-judged trigger, because the
clauses are all syntactic and the interesting failure is semantic:

- **Drift.** `Q_WITHIN_GRANT` — a `Noul`: *"is this step part of accomplishing
  the stated goal?"* — batched into the existing risk-gate call for each step,
  so it costs nothing extra (TypeSafe answers N questions in one pass; this is
  exactly the `gate_questions()` pattern). Below `settings.grant_drift`
  (default 0.6) ⇒ re-confirm. Fail-closed: no assessment ⇒ re-confirm.

A re-confirmation is a **new grant with `parent_id` set**, not an edit. It gets
its own readback ("I'm on gmail.com now, which you didn't mention — carry on
there too?"), its own budget (the remaining budget, restated), its own audit
row, and its own channel. Re-confirming for a `CONFIRM_VISUAL` step requires the
visual channel even if the parent grant was spoken — because the parent's
channel cap is what stopped it, and a spoken "yeah, go on" cannot lift it.

While waiting, the job is **`AWAITING_CONSENT`**, and its wall-clock budget
keeps running (§B.5).

### A.5 Revocation

Three properties, in priority order: it must work without the LLM, it must work
without the tool router, and it must never claim more than it did.

**Hearing it.** Add `Q_STOP` (a `Noul`) to `gate_questions()`, evaluated only
when `jobs.any_live()`. It rides the existing single address-gate call, so it is
free in latency and money. Threshold `settings.stop_threshold` default **0.5** —
deliberately lax, unlike every other threshold in `config.py`, because this one
is an escalation toward safety: a false stop costs a restart, a missed stop
costs whatever the agent does next. Additionally, `_on_speech_start` already
fires on the audio thread and already counts `barge_ins`; a barge-in during a
job's own spoken progress line should **pause** the job pending the gate's
verdict. Pausing on maybe and resuming on no is correct here; the reverse is not.

**Doing it.** `JobRegistry.revoke(job_id, reason)` sets `GrantState.revoked_at`
and sets the job's `threading.Event`. It is idempotent and it always succeeds.
Every warrant issued under that grant and not yet spent becomes invalid — which
is the whole reason warrants are single-use and short-lived.

**Latency, honestly.** Cancellation is cooperative. Checked (a) by the runner
between steps, (b) inside `_authorize` before a warrant is issued, (c) by the
generator if it chooses. A tool already inside `run_argv` finishes or hits its
own 20s timeout. So daa says: *"Stopping — I'll finish the step I'm on."* It
does not say "stopped" until the runner has actually exited. An assistant that
says "stopped" while a click is still in flight has taught you not to believe it.

**What happens to work already done: nothing, automatically.** Rolling back on
revocation would make "stop" more destructive than letting it finish, which is
precisely backwards. daa reports and offers:

> "Stopped. I'd moved four files. Want me to put them back?"

which is a checkpoint rollback (§C), which goes through `CONFIRM_VOICE` per the
existing rule that undo never executes below `CONFIRM_VOICE`.

---

## B. Async execution

### B.1 The fork, and the pick

**Threads, with a hard split between authorization and execution.** Not
asyncio, not subprocesses.

- **Not asyncio.** There are zero `async def` in the voice package. Adopting it
  means `async` propagates up through `_execute` → `_dispatch` → `_handle_call`
  → `_act` → `handle_chunk`, i.e. through all 73 voice-loop tests, and you end
  up maintaining a sync path *and* an async path to `tool.run` — which is
  exactly the invariant the structural test at
  `test_voice_loop.py:511` exists to protect. The 13 existing tools are
  `subprocess.run`-based and blocking; asyncio would wrap them in a thread pool
  anyway. All of the cost, none of the benefit.
- **Not subprocesses.** Tempting for isolation and for surviving a restart, but
  it means marshalling `ResolvedAction` / `Disposition` / `Warrant` across an
  IPC boundary, which means either re-implementing the gate on the far side or
  trusting the far side — and a trusted IPC peer is a second authority. There is
  exactly one authority in this system. (A *tool* may drive a subprocess
  internally. That is its business and it already does.)
- **Threads.** The codebase already has thread discipline: `AudioSource.set_speech_listener`
  documents that the listener runs on the audio callback thread. Threads keep
  `ResolvedAction` objects as objects, keep one registry, one journal, one audit
  sink, and keep the gate literally the same code.

### B.2 The agent thread proposes; the loop thread disposes

This is the architectural core.

```
worker thread                          loop thread (owns mic, console, TTS)
-------------                          ------------------------------------
gen.send(prev_result)
  -> yields ResolvedAction
_authorize(action, job) ───────────►   drain authorize queue
                                       assessment = self._assess(action, goal)
        (blocks)                       disposition = self._decide(action, spec, a)
                                       ok, why = grant_satisfies(...)
                                       if not ok: _confirm / _confirm_visual
                                       issue Warrant(bound to this action)
              ◄─────────────────────   or Refusal(reason)
_execute(tool, action, disposition,
         confirmed=..., warrant=w)
  -> tool.run(action)     [BLOCKS HERE, on the worker thread]
  -> journal.record(...)
gen.send(result)
```

Why execution goes on the worker thread and authorization does not:

1. **The loop thread must stay free to hear "stop."** A revocation that can only
   be processed after a 20-second `osascript` timeout is not a revocation. This
   is the deciding argument.
2. Authorization is pure computation plus, rarely, a user prompt. Serializing it
   costs nothing and preserves the single-authority property exactly.
3. Prompts must happen on the thread that owns the mic, the scripted-reply
   queue, and the console. Two threads calling `console.ask()` is a bug you
   cannot test your way out of.

**The "only caller" invariant survives literally**: `_execute` is still the only
function that calls `tool.run`. What is new is that it may be *called from*
another thread. That was never the stated invariant, and the assertion block at
the top of `_execute` still runs on every call.

### B.3 The `Warrant` — what crosses the thread boundary

A boolean cannot cross a thread boundary safely: by the time the worker acts on
`confirmed=True`, the grant may have been revoked, the action may have been
mutated, or the readback may be two minutes stale.

```python
@dataclass(frozen=True, slots=True)
class Warrant:
    """Single-use authorization for exactly ONE ResolvedAction.

    Replaces nothing. It is what `confirmed=True` and the private _VISUAL_OK
    token become when they have to survive a queue hop and a clock.
    """
    id: str
    action_digest: str            # sha256 over the canonicalised ResolvedAction
    disposition: Disposition
    via: Literal["voice", "visual", "grant", "implicit"]
    grant_id: str | None
    job_id: str | None
    step_index: int | None
    issued_at: float
    expires_at: float             # default issued_at + 30s
```

`_execute` gains one keyword-only parameter, `warrant: Warrant | None = None`,
and three assertions alongside the existing ones:

```python
if warrant is not None:
    assert warrant.action_digest == digest(action), "warrant is for a different action"
    assert now < warrant.expires_at,                "warrant expired"
    assert registry_of_warrants.spend(warrant.id),  "warrant already spent"
```

The 30-second expiry matters more than it looks: *"Should I move these three?"*
— yes — 90 seconds of agent work — then the move, against a folder that has
moved on, is a confirmation that has become a lie by elapsed time. Expiry forces
re-authorization, which re-runs `resolve()` and re-reads the world.

`_VISUAL_OK` stays exactly as it is for the synchronous path. The visual
assertion becomes `visual is _VISUAL_OK or (warrant is not None and
warrant.via == "visual" and warrant.disposition.tier >= CONFIRM_VISUAL)`.

### B.4 The long-running tool protocol

**Do not add a method to `Tool`.** `Tool` is `@runtime_checkable`, and
`ToolRegistry.register` does `isinstance(tool, Tool)` and raises `TypeError` on
failure. I verified the behaviour: a runtime-checkable Protocol's `isinstance`
returns `False` for a missing method. Adding `steps()` to `Tool` therefore
breaks all 13 registrations at import of `daa.tools`, taking out the 53 registry
tests and the 27 undo-coverage tests with it. A separate protocol:

```python
# Yields an action, receives its result, returns a final summary result.
StepStream = Generator[ResolvedAction, ToolResult, ToolResult]

@runtime_checkable
class LongRunningTool(Protocol):
    """A tool that reaches its goal in many gated steps.

    SEPARATE from Tool on purpose. A LongRunningTool is also a Tool — it keeps
    resolve() and run() — so it registers, routes and gates like anything else.
    run() is the one-step fallback and the thing the risk gate sees when the
    user asks for the *whole* goal; steps() is how the goal is actually reached.
    """
    spec: ToolSpec
    def resolve(self, **kwargs: Any) -> ResolvedAction: ...
    def run(self, action: ResolvedAction) -> ToolResult: ...
    def steps(self, action: ResolvedAction, grant: Grant) -> StepStream: ...
```

A generator, not a callback, not a handle-returning `start()`. Reasons:

- The generator makes "the agent proposes, the loop disposes" **structural**.
  The agent literally cannot call a tool; it can only `yield` a request. There
  is no API surface through which it could route around the gate.
- All the slow, model-driven, perception-heavy work (screenshotting, planning,
  reading a page) happens inside the generator on the worker thread, where it
  belongs.
- The return value is an ordinary `ToolResult`, so the finishing path — summary,
  undo, audit — is the path that already exists.
- `ToolSpec.long_running: bool = False` lets the router and the loop discover
  this without `isinstance`.

### B.5 The job registry

```python
class JobStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_CONSENT = "awaiting_consent"
    PAUSED = "paused"                  # barge-in, pending the gate's verdict
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"            # the user said stop
    EXPIRED = "expired"                # a budget ran out
    INTERRUPTED = "interrupted"        # the process died under it

@dataclass(frozen=True, slots=True)
class JobProgress:
    steps_done: int
    step_budget: int
    elapsed_s: float
    wall_budget_s: float
    spent_cents: int
    phase: str        # a SPOKEN fragment: "filling in the form". Same rules as
                      # ToolResult.summary: no paths, no ids, no markdown.

@dataclass(frozen=True, slots=True)
class JobRecord:
    id: str
    grant_id: str
    goal: str
    status: JobStatus
    progress: JobProgress
    started_at: float
    ended_at: float | None = None
    checkpoint_ids: tuple[str, ...] = ()
    summary: str = ""          # spoken, one sentence
    error: str | None = None
```

- **Identity:** `uuid4().hex[:12]`, matching `AuditEvent.id` and `UndoEntry.id`.
- **Concurrency:** a bounded pool, `max_jobs` default **1**. Two agents driving
  the same GUI is not a concurrency problem, it is a correctness problem. A
  second request while one is live is a question, not a queue entry.
- **Progress** is push, not poll: the runner posts a `JobProgress` after each
  step; the dock UI subscribes. Never spoken unless asked.
- **Cancellation:** one `threading.Event` per job (§A.5).
- **Persistence:** `~/.daa/jobs.jsonl`, through the *existing* `safety/store.py`
  (`secure_dir` / `open_append` / `harden_file`), 0700 dir, 0600 file,
  append-only with tombstones — the same shape as the undo journal, for the same
  reasons, and treated as the same kind of untrusted input.

**What survives a restart: the record, never the run.** A job does not resume.
A browser agent that wakes up mid-checkout in a world that moved on is the worst
possible thing this design could produce. On startup, any row still `RUNNING` is
rewritten `INTERRUPTED`, and at the next wake daa says, once:

> "I was part way through clearing Downloads when I stopped. I'd moved four
> things. Want me to put them back?"

That offer is a checkpoint rollback and it inherits all of the staleness
machinery. Resuming would require re-consent anyway (the grant has expired), at
which point it is a new job — so there is no resume path worth the risk.

### B.6 Reporting back by voice, minutes later

The rule: **a background job never speaks. It posts a `Notice`, and the loop
drains notices at a safe moment.**

```python
@dataclass(frozen=True, slots=True)
class Notice:
    id: str
    job_id: str
    text: str                       # SPOKEN. Same rules as ToolResult.summary.
    urgency: Literal["low", "normal"]
    created_at: float
    expires_at: float               # a stale notice is DROPPED, never spoken late
```

There is deliberately **no "high" or "interrupt" urgency.** There is no
background job urgent enough to talk over a human being. If a job needs
something urgently it parks and expires (below), which is fail-closed; speaking
over a conversation is not.

**Safe moments**, in priority order, all expressible from what `handle_chunk`
already computes:

1. **Piggyback.** Immediately after daa finishes speaking in a turn the user
   addressed to it. "…done. Also, that download finished." This is free and it
   is where 90% of notices should land.
2. **Silence.** No speech segment at all (`heard.empty` → the existing
   `dropped_reason == "silence"` path) for `settings.notice_quiet_s`, default
   20s. Silence is the only room-state daa can be *sure* it is not interrupting.
3. **Never** on an utterance the gate judged not-addressed. That is another
   person talking, and speaking into it is precisely the failure the address
   gate exists to prevent. This is the hard interaction with the address gate:
   **notices consume the gate's output, they never bypass it.**

Three further constraints:

- A notice is **not an utterance**. It must never re-enter the address gate as
  input, must never reach the LLM, and must never be able to start a turn or a
  tool call. TTS only, from a string the job produced.
- Pending notices are capped (default 3) and **coalesced**: "two things
  finished" beats two separate announcements 200ms apart.
- A notice interrupted by a barge-in is **dropped, not retried**. `_on_speech_start`
  already gives the signal.

**When a job needs consent and the room is busy**, the job parks in
`AWAITING_CONSENT` and *its wall-clock budget keeps running*. If the budget
expires while parked, the job goes `EXPIRED` and posts a notice. This is
deliberate and will occasionally feel broken: a browser flow will time out
because you were on a call. That is the correct failure. An unanswerable
question is a no, and the alternative is either shouting over you or proceeding
without you.

---

## C. Checkpoints instead of stacked undo

### C.1 What a checkpoint is

Not a snapshot. daa cannot snapshot macOS, and a checkpoint that implies it can
is the same category of lie as a readback that omits a consequence. A checkpoint
is **a named marker plus the window of journal entries recorded after it, plus
the fingerprints of the world at the moment it was taken.**

```python
@dataclass(frozen=True, slots=True)
class Checkpoint:
    id: str
    job_id: str
    at: float
    label: str                                        # SPOKEN: "before I started on the form"
    entry_ids: tuple[str, ...]                        # UndoJournal entries in this window
    fingerprints: Mapping[str, Mapping[str, Any]]     # SAME shape as UndoEntry.fingerprints
    sealed: bool = False                              # something irreversible happened inside
    sealed_by: str | None = None                      # spoken: "the email was sent"
```

It **composes with** `UndoJournal` rather than replacing it. Nothing about
per-step undo changes: every mutating step still records its inverse, still
gets fingerprinted, still carries `produced_by`, still fails
`test_undo_coverage` if it returns `undo=None`. The checkpoint is an *index over
those rows*, persisted in the same journal file as a third row kind
(`{"kind": "checkpoint", ...}`) so it survives a restart on the existing
append-only machinery and needs no new file, no new hardening code, and no new
crash semantics.

### C.2 Rollback, and why it is not 30 stacked inverses

Rolling back a checkpoint replays `entry_ids` **in reverse, each one through the
full existing path**: `peek`-equivalent → `entry.trusted` → `entry.tool in
registry.get(entry.produced_by).spec.inverses` → `staleness()` → `CONFIRM_VOICE`
→ run → `commit`. Three differences from stacking, and they are the whole point:

1. **One consent, N validated executions.** The user hears "put back the six
   things I did on that form?" once. Each inverse is still individually
   validated — the forgery check, the staleness check, the inverses check. The
   consent is aggregated; the *verification* is not.
2. **It stops at the first problem and says so.** The existing `staleness()`
   already returns a spoken sentence. Rollback reports truthfully:
   > "I put back four of the six. The other two have changed since, so I've
   > left them alone."
   This is the behaviour that makes rollback usable: a rollback that is
   all-or-nothing is nothing, and a rollback that ploughs through stale entries
   is a second mutation wearing an undo's clothes — exactly what `undo.py`'s
   docstring warns about.
3. **Sealing.** The instant a step inside the window is irreversible, the
   checkpoint is sealed. Sealing does not disable rollback; it **changes the
   sentence**:
   > *unsealed:* "I can put that back."
   > *sealed:*   "I can put back the files, but the email has already gone."

   That is the claim this whole section rests on: **checkpoints do not make
   irreversible things reversible. They make the boundary speakable.**

A rollback is itself a job, with its own audit rows and its own checkpoint (so
you can see that a rollback happened, and what it did not manage).

### C.3 What is checkpointable, and what is never

Checkpointable — anything with a real, fingerprintable inverse:

- file moves, trash, renames, copies (the existing `{"pairs": [[src, dst]]}` shape)
- clipboard set/restore
- window and app state, best effort, declared as best effort
- browser navigation, scrolling, tab open/close
- **form field entry, before submit** — this is the important one for the
  browser sibling: the whole flow up to the submit button is checkpointable, and
  the submit is not. Designing the browser agent so that as much as possible
  lives on the reversible side of that line is the single highest-leverage thing
  the browser-use sibling can do.

**Never checkpointable, always confirmed individually regardless of any grant.**
Represented as a new `ToolSpec.grantable: bool = True`, set `False` by hand on
this list. That is a human-authored, reviewed, static property of a tool — the
same nature as `floor` and `inverses`, and deliberately *not* something a
runtime score can assert, mirroring "policy never invents REFUSE":

- anything a tool already declares `irreversible = True` / tags `"irreversible"`
  — **reuse the existing machinery, do not invent a parallel list**
- anything that sends a message to another human: email, iMessage, Slack, a
  Shortcut that texts (`run_shortcut` already reads back its payload for this
  reason)
- anything that spends money, or enters payment details
- anything that changes an authentication state: login, logout, password
  change, MFA, granting OAuth consent
- anything that deletes past the Trash, or empties it
- any write to a remote system whose effect cannot be read back
- execution of generated code — `run_applescript`, `run_shell` — already
  `CONFIRM_VISUAL`-floored and already covered by
  `test_generated_code_can_never_be_authorised_by_voice_alone`
- **granting another grant.** A job must never be able to widen its own scope.

Plus one runtime raise, which is always permitted:
`assessment.unrecoverable > UNRECOVERABLE` ⇒ not grant-satisfiable, even if the
tool claims `grantable=True`.

---

## D. Contract changes

I own nothing here; this is the precise list for you to apply.

### D.1 New types (all frozen, slots, defaults last)

| Type | Why |
|---|---|
| `GrantScope` | allow-lists for tools / origins / apps / path prefixes. Empty means empty. |
| `Budget` | steps, wall clock, spend. `spend_cents=0` means *may not spend*. |
| `Grant` | the consent primitive. `channel_cap` and `max_satisfiable` are the two properties that keep it from becoming blanket permission. |
| `Warrant` | single-use, action-bound, expiring authorization. The thing that crosses the thread boundary in place of `confirmed: bool`. |
| `Checkpoint` | window of journal entry ids + fingerprints + `sealed`. |
| `JobStatus` (`enum.StrEnum`) | nine states; `INTERRUPTED` and `EXPIRED` are the fail-closed ones. |
| `JobProgress` | push-model progress, with a **spoken** `phase`. |
| `JobRecord` | identity, status, budgets, checkpoints, spoken summary. |
| `Notice` | deferred spoken report. No `"high"` urgency exists, on purpose. |
| `StepStream` alias | `Generator[ResolvedAction, ToolResult, ToolResult]`. |
| `LongRunningTool` Protocol | **separate from `Tool`** — see D.4. |

### D.2 Field additions to existing types

| Type | Field | Reasoning |
|---|---|---|
| `ToolSpec` | `grantable: bool = True` | The static, human-authored "a grant can never answer for this" list (§C.3). Same nature as `floor`. Defaulting `True` keeps all 13 tools unchanged; the destructive ones get it set to `False` by hand. |
| `ToolSpec` | `long_running: bool = False` | Cheap discovery for the router and the loop without `isinstance`. |
| `ToolResult` | `job_id: str \| None = None` | So `start_browser_task` can return "I've started on that" and the loop knows what to watch. |
| `ToolResult` | `checkpoint_id: str \| None = None` | Lets a step declare the checkpoint it belongs to without the loop guessing. |
| `ResolvedAction` | `origin: str \| None = None` | Scheme+host, or a bundle id. The browser and computer-use siblings both need scope matching, and re-parsing args in `safety/` to find it would put URL parsing in the one module that must stay pure. Tool-author supplied at `resolve()` time. **Must not appear in `describe()`** — see D.5. |

**`Disposition` is unchanged.** I considered adding `grant_id` / `satisfied_by`
and decided against it: `Disposition` is the output of `policy.decide()`, which
knows nothing about grants and must keep knowing nothing. The grant linkage
lives on the `Warrant` and in the audit payloads. This also keeps all 102
`test_safety_policy` tests untouched.

**`policy.decide()` signature is unchanged.** The grant check is
`safety/grant.py::grant_satisfies`, pure, no I/O, no clock beyond an injected
`now`, called by the loop after `decide()`.

### D.3 Audit (in `safety/audit.py`, not `contracts.py`)

New builders: `grant_event`, `grant_revoked_event`, `job_event`,
`job_step_event`, `checkpoint_event`, `rollback_event`, `notice_event`.

Existing builders gain keyword-only, defaulted fields: `judgment_event`,
`disposition_event`, `confirmation_event`, `execution_event` each take
`grant_id`, `warrant_id`, `job_id`, `step_index`, all `None` by default and all
**hoisted to the top level of the payload**, matching the existing convention in
`judgment_event` ("the questions asked six months later should not need a JSON
path").

`confirmation_event.via` gains `"grant"` as a value (it is already typed as a
bare `str` in the payload, so this is additive).

**The reconstruction requirement is then satisfied by a single query on
`grant_id`:**

```
grant          id, goal, plan_summary (the EXACT spoken readback), ceiling,
               granted_via, scope, budget, granted, parent_id
  └ job        job_id, grant_id, goal
     └ per step: judgment + disposition + execution, each carrying
                 grant_id, warrant_id, job_id, step_index, tier, via
     └ confirmation rows for every mid-run re-confirm, carrying parent_id
     └ checkpoint / rollback rows
  └ grant_revoked  reason, at, steps_completed
```

Storing the *spoken readback verbatim* on the grant row is the load-bearing
part. "Who authorized what" is not answerable from a scope object; it is
answerable from the sentence the user actually heard before they said yes.

### D.4 The one thing that must not be done

**Do not add a method to the `Tool` protocol.** Verified empirically:

```
>>> isinstance(tool_without_new_method, RuntimeCheckableProtocolWithIt)
False
```

and `ToolRegistry.register` raises `TypeError(f"{spec.name} does not satisfy the
Tool protocol")` on that. Adding `steps()` to `Tool` breaks all 13 registrations
at import of `daa.tools`, which takes out `test_tools_registry` (53),
`test_undo_coverage` (27), and most of `test_voice_loop` (73) — roughly 150 of
the 698 currently collected tests, for one line. `LongRunningTool` is separate
and structurally identical up to the extra method, so a long-running tool still
satisfies `Tool` and still registers, routes and gates normally.

### D.5 What breaks, and what does not

**Safe (verified by inspection):**

- New fields on `ToolSpec` / `ToolResult` / `ResolvedAction`, appended with
  defaults. Every construction site in `tests/` and `src/` uses keywords —
  checked `ToolSpec(` (7 sites) and `ToolResult(` (no positional uses).
- `_execute` gaining keyword-only `warrant: Warrant | None = None`. The four
  tests that call `_execute` directly (`test_voice_loop.py` 527, 536, 545, 1482)
  all pass `confirmed=` as a keyword and assert on `AssertionError`; a defaulted
  new parameter cannot change any of them.
- `Disposition` and `policy.decide` untouched ⇒ 102 policy tests untouched.
- `Grant`/`Job`/`Checkpoint` types are additive.

**Will break, or needs care:**

1. **`test_voice_loop.py::test_execute_is_the_only_caller_of_tool_run` is scoped
   to `daa.voice.loop` only.** It does `inspect.getsource(loop_mod)` and asserts
   `source.count("tool.run(") == 1`. A job runner in a *new* module
   (`voice/jobs.py`) could host a second call site and this test would stay
   green. **Extend it to walk every module in `daa.voice` and `daa.safety`
   before writing the runner**, or the invariant quietly stops being enforced by
   exactly the change that most threatens it. This is the most important item
   in this section.
2. **`UndoJournal` is not thread-safe.** `_entries`, `_consumed` and
   `_loaded_sig` are mutated without a lock, and `_refresh()` can `reload()`
   concurrently with `record()`. With `_execute` running off the loop thread,
   this needs a `threading.RLock` **inside** `UndoJournal` (not held by the
   caller — a second caller will forget). The file-level writes are already
   safe: `open_append` is `O_APPEND` and lines are small, so the 51 undo tests
   should stay green with an internal lock.
3. **`Transcript`** likewise needs a lock, or jobs must not write to it. I
   prefer the latter: a job's steps are not conversation.
4. **`_next_reply()` re-entrancy.** It pops from `self._scripted_replies`, which
   is how `daa say` and most of the 73 loop tests drive confirmations. A job
   asking for consent on the loop thread would steal a reply intended for the
   foreground turn. **Rule: notices and job consents are drained only at
   explicit drain points, never re-entrantly inside `_confirm` or
   `_confirm_visual`.** Needs a test.
5. **`TurnOutcome`** is per-utterance and will not hold job results. Jobs get
   their own `JobOutcome`; do not try to thread them into `outcome.results`, or
   `ran_anything` and `handled_calls` start lying.
6. `daa doctor` should grow "jobs: N live, M interrupted"; new `daa jobs`,
   `daa stop <id>`, `daa rollback <checkpoint>` subcommands — additive to the 22
   CLI tests.
7. `Settings` gains `max_jobs`, `notice_quiet_s`, `grant_drift`,
   `stop_threshold`, `grant_ttl_s`, `warrant_ttl_s`. `Settings` is frozen with
   `slots` and all-defaulted, so additive; but `Settings.load()` must be updated
   in the same commit or the new fields silently ignore their env vars.

### D.6 Testing, with no keys and no network

The existing pattern holds: every seam has an offline fake.

- `JobExecutor` is a seam. `InlineExecutor` runs the generator on the calling
  thread, step by step, so every job test is deterministic and single-threaded.
  The threaded executor gets a small number of tests of its own.
- `FakeAgent` is a `LongRunningTool` whose `steps()` yields a scripted list of
  `ResolvedAction`s — including ones deliberately outside the grant, to prove
  each of the 12 satisfaction clauses fires.
- The clock is injected (`now: Callable[[], float]`), never `time.time()` inside
  the runner, so budgets, TTLs and warrant expiry are testable without sleeping.
- Jev stays `FakeJev`: `Q_STOP` and `Q_WITHIN_GRANT` answer at maximum
  uncertainty, which with the thresholds above means **no stop is heard and
  every step re-confirms** — fail-closed in both directions, and the same
  honest "no gate quality number in this repo is real yet" caveat the README
  already carries.
- No live API keys anywhere. The browser and computer-use tools get fake
  drivers; the grant machinery never touches the network.

---

## What breaks, what I am unsure about, and the biggest risk

### What breaks
Summarized above in D.5. The three that will actually bite: the "only caller"
structural test not covering new modules (1); `UndoJournal` thread-safety (2);
and `_next_reply()` re-entrancy stealing confirmations (4).

### What I am unsure about

- **`origin` on `ResolvedAction` vs. a `scope_key` computed in `safety/`.** I
  put it on the action to keep URL parsing out of the pure module, but it means
  every tool author has to remember to set it, and a forgotten `origin` is
  `None`, which clause 9 currently treats as "no origin constraint" — i.e. the
  permissive reading. That is the wrong default and I have not found a
  non-annoying fix. Candidate: make `origin` required (no default) on any tool
  whose spec sets `long_running=True`, enforced by a registry test.
- **Whether the generator contract survives a real computer-use agent.** Such an
  agent wants to batch perception with action (screenshot → click → screenshot)
  and will feel every queue hop. The pressure to let it call tools directly is
  exactly the pressure to resist, but I do not know yet whether a per-step
  round trip is 2ms or 50ms in practice. If it is 50ms, expect a proposal to
  "just let the agent do read-only tools inline," and that proposal should be
  refused — a read-only screenshot is how you exfiltrate a password manager.
- **Park-and-expire (§B.6) vs. one gentle prompt at the next quiet moment.** I
  chose park-and-expire because it is fail-closed, but it will feel broken the
  first time a five-minute browser flow dies because you took a call.
- **Whether spend limits belong here at all**, given no tool spends money yet. I
  included `spend_cents` with a default of `0` = may not spend, because adding
  it later means every existing grant is retroactively silent about money.
- **`max_jobs = 1`.** Probably right for GUI-driving agents, probably wrong for
  a pure-research background job. I would ship at 1 and find out.

### The single biggest risk

**Consent fatigue turning the grant into blanket permission — not through a bug,
through habituation.**

Every technical invariant in §A holds under adversarial analysis: the ceiling is
capped by the channel, `grantable=False` puts a whole category outside the
mechanism, the scope allow-lists default to empty, REFUSE is never satisfiable,
budgets are finite, and the audit log can reconstruct the bargain. Those bound
the *worst* case. They do nothing about the *typical* case, which is that the
user hears a structurally similar four-clause sentence twenty times, learns its
shape, and starts saying "yeah" at the second clause. At that point the system
is formally correct and substantively a blanket permission, and no test will
ever fail.

This is the failure mode of every scoped-permission system ever shipped, and the
per-action confirmation model daa has today is *immune* to it in a way this
design is not — because the readback names a specific file you recognize, and a
readback naming the wrong file is jarring in a way that a readback naming a
slightly-too-wide scope is not.

Mitigations, none of them sufficient alone:

- **Short expiries and no persistence.** A grant dies with the job. No "remember
  this," no templates, no "always allow for this site." The friction is the
  feature, exactly as with the typed `yes`.
- **The readback names the most destructive thing in scope, not the average
  one** (§A.2, clause 2). This is the clause that makes the sentence *stop*
  sounding the same each time.
- **Narrow scope defaults**, and a grant proposal that asks for a wide scope
  should itself be treated as a signal — the assistant asking for more than it
  needs is the thing you want to notice.
- **Measure it.** The existing `evals/` harness is the right home: a labelled
  set where the metric is *can the user, sixty seconds later, state what they
  authorized?* If that number is bad, the grant readback is decoration and this
  design should not ship in its current form. Unlike the address-gate threshold,
  this one cannot be tuned — it has to be designed for, and I would rather find
  out before computer-use ships than after.
