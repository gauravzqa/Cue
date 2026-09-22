# What the dock needs from Python

Nothing under `src/` or `tests/` was touched by this work. Everything the
Swift side needs is written out here: one ~10-line edit to `_confirm_visual`,
and one new module (`src/daa/ui/`) plus a `daa bridge` subcommand, both purely
additive.

`ui/tools/fake-bridge` is a **working reference implementation of the whole
protocol** in ~280 lines of dependency-free Python. It is not a mock of the
wire format — it *is* the wire format, and the Swift suite decodes its exact
`confirm.request` frame as a fixture
(`ConfirmRouterTests.testTheFakeBridgeFrameDecodesCompletely`). Read it
alongside this document; where the two disagree, the fake bridge is right.

---

## 1. The one change to existing code

**File:** `src/daa/voice/loop.py`
**Function:** `VoiceLoop._confirm_visual`
**Lines:** 894–911 as of the version read on 2026-09-21 (the anchor is the
comment block beginning *"Deliberately NOT spoken here"*, immediately followed
by `typed = ""`).

### Why

`_visual_detail()` renders the action as a 68-column text blob. That is the
right answer for a terminal and the wrong one for the card: the dock needs the
script as a string it can put in a monospaced, scrollable, scroll-gated block,
the consequences as separate items it can flag, and `assessment.synthetic` as
a boolean it can turn into the sentence *"daa could not get a real judgment, so
it is assuming the worst."* A pre-rendered blob can be shown, but it cannot be
gated on, flagged, or read by VoiceOver as structure.

### The diff

```diff
--- a/src/daa/voice/loop.py
+++ b/src/daa/voice/loop.py
@@
         typed = ""
         self._confirming += 1
         try:
-            console.write(_visual_detail(action, disposition))
-            typed = console.ask("type yes to approve, anything else to cancel: ")
+            present = getattr(console, "present", None)
+            if present is not None:
+                # A console that can render the action itself gets the
+                # objects rather than a pre-rendered 68-column blob. Same
+                # contract as the typed path: it returns True only for a
+                # deliberate approval, and anything else -- False, None, a
+                # raise, a timeout, a dead peer -- is a refusal.
+                typed = "yes" if present(action, disposition) is True else ""
+            else:
+                console.write(_visual_detail(action, disposition))
+                typed = console.ask("type yes to approve, anything else to cancel: ")
         except Exception as exc:  # noqa: BLE001 -- isolation boundary, see comment
             self._emit("error", where="console", error=str(exc))
             typed = ""
         finally:
             self._confirming -= 1
```

### What does not move

- the `== "yes"` comparison
- the `visual_confirm` emit and `_confirmation_logged` call
- the `_NO_SCREEN` / `_console_available` branch above it
- `_VISUAL_OK` and its gating in `_execute`
- `self._confirming` bookkeeping and the `finally`

`_confirm_visual` stays the only function in the codebase that can produce
`_VISUAL_OK`. The dock returns a **boolean**; it never sees or names that
token, and neither does the protocol.

Note `is True` rather than truthiness. A `present` that returns a non-empty
string, a `Mock`, or anything else truthy is a bug, and the safe reading of a
bug on this path is "not approved".

### Tests to add alongside

In `tests/test_voice_loop.py`, next to the existing `FakeConsole` tests:

- a console with `present` returning `True` approves, and `_VISUAL_OK` is still
  what reaches `_execute`;
- `present` returning `False` refuses and answers "Okay, leaving it." (a `speak`
  frame; the dock writes it into the transcript);
- `present` returning `None`, `"yes"`, or `1` **refuses** (the `is True` rule);
- `present` raising is a refusal and emits `error where="console"`;
- a console with `present` is **never** asked to `write` or `ask`;
- `TerminalConsole` has no `present`, so every existing test is unchanged.

The last one is the reason this is a safe edit: `TerminalConsole` does not
grow an attribute, so `FakeConsole`, the `"y"`-is-not-`"yes"` test, the EOF
test and the `Exploding` console test all still exercise the same branch.

### If you would rather touch nothing at all

Stage 1 works without this diff: the dock ships a `DockConsole` implementing
only `available` / `write` / `ask` and renders `_visual_detail`'s text in a
monospaced block. It is honest and it is ugly, and it cannot support the
scroll gate (there is no way to know where the script ends in a blob), the
consequence flags, or the synthetic disclosure. Take the ten lines.

---

## 2. The new module: `src/daa/ui/`

Purely additive. `daa listen`, `daa say`, `daa undo` and `daa doctor` must be
bit-for-bit unchanged.

```
src/daa/ui/
    protocol.py    frame encode/decode, the envelope, the method names
    bridge.py      reader/writer/turn threads, BridgeMic, BridgeTranscriber,
                   DockConsole, the audit tee, stdout theft
```

plus, in `src/daa/cli.py`:

```diff
     undo = sub.add_parser("undo", help="reverse the last recorded mutation")
     ...
+    bridge = sub.add_parser(
+        "bridge",
+        help="speak the dock protocol on stdin/stdout (not for humans)",
+    )
+    bridge.set_defaults(func=cmd_bridge)
```

### 2.1 Steal `sys.stdout` first, before any import that might print

fd 1 is the protocol and nothing else. One stray `print()` anywhere in the
tree corrupts the stream, and the symptom is a dock that silently stops
updating.

```python
def cmd_bridge(args):
    import sys, os
    real_stdout = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)                 # every print() now goes to stderr
    sys.stdout = sys.stderr       # and so does anything holding the object
    ...
```

The dock tees the child's stderr to `~/Library/Logs/daa/python.log` and to its
own stderr, so nothing written there is lost.

`-u` is passed by the dock and `PYTHONUNBUFFERED=1` is set, but write the
writer thread to flush explicitly anyway. Python's stdout is *fully buffered*
on a pipe; forgetting this produces a dock that hangs waiting for `ready`.

Worth a test: import the whole `daa` tree under the bridge and assert fd 1 saw
nothing but valid frames.

### 2.2 Threads

Three, and no asyncio. Python stays fully synchronous.

1. **reader** — owns `sys.stdin.buffer`, one JSON object per line, dispatches
   into queues. Never runs handler code. **An unparseable line is logged to
   stderr and skipped; it must never kill the reader.**
2. **turn** — runs `VoiceLoop.run()` exactly as `daa listen` does, pulling
   from a `BridgeMic`. Blocking, single-threaded, unchanged semantics.
3. **writer** — a lock-guarded `real_stdout` with a bounded queue (2048).

### 2.3 `BridgeMic` and `BridgeTranscriber`

No change to `run()` is needed. `AudioChunk.pcm` already documents carrying a
non-PCM payload for the fakes, so this is the sanctioned pattern:

- **`BridgeMic(AudioSource)`** — `segments()` yields `AudioChunk`s off a queue
  fed by the reader, with `pcm = text.encode()`, `started_at` from Swift's
  clock, and `complete` from Swift's VAD (silence-cut vs release-cut), which
  is what feeds the gate's honest `end_of_turn`. `set_speech_listener`
  forwards `mic.onset`, so **barge-in keeps working**.
- **`BridgeTranscriber(Transcriber)`** — decodes the chunk and returns
  `Transcription(text=..., source="local", confidence=..., partial=not complete)`.

Because `run()` is untouched, `self._segments` still exists, so `_next_reply()`
still works and spoken "yes"/"no" answers to `CONFIRM_VOICE` arrive through the
same path as any other utterance.

**Push-to-talk vs always-on.** `mic.utterance` carries `addressed`. When it is
true the user held a key, which is an unambiguous act of address — the same
argument `handle_text`'s docstring makes for typed text — so the bridge skips
the gate. When it is false, the utterance goes through `AddressGate` unchanged.

### 2.4 The audit tee

The read path needs **no new instrumentation**. `VoiceLoop.audit` is already an
injected callable taking a structured, redacted `AuditEvent`. Wrap it:

```python
def tee(inner, emit):
    def sink(event):
        try:
            inner(event)          # ~/.daa/audit.jsonl, exactly as today
        finally:
            emit(event)           # bounded queue -> writer thread
    return sink
```

Rules, all of them because "an audit sink that throws must never take the loop
down with it":

- bounded queue of 2048;
- on overflow **drop the chatty display-only kinds first** — `heard`, `spoke`,
  `barge_in`, `buffered` — and never `disposition`, `confirmation`,
  `confirmation_event`, `execution`, `undo`, `undo_rejected`, `undo_retained`,
  `refused`, `dry_run`, `visual_confirm`, `deferred_visual`, `abandoned`,
  `error`, `judgment`;
- never block the loop thread;
- swallow every exception.

The Swift side's copy of those two sets is in `AuditRecord.chatty` /
`AuditRecord.loadBearing`, and a test asserts they are disjoint. Keep them in
sync.

---

## 3. The wire protocol

Newline-delimited JSON, UTF-8, one object per line, both directions. No
Content-Length headers. No embedded raw newlines (JSON escaping guarantees
this; the Swift encoder asserts it).

```json
{"t":"req","id":"r7","m":"method.name","p":{}}
{"t":"res","id":"r7","ok":true,"p":{}}
{"t":"res","id":"r7","ok":false,"err":{"code":"x","message":"y"}}
{"t":"ev","m":"event.name","p":{}}
```

Ids are opaque strings, unique per sender. Requests flow **both** ways.

Two rules the Swift decoder enforces and Python should mirror:

- **A `res` with no `ok` field decodes as an error, never as success.** The
  only thing a response ever authorises is an action, and an unreadable answer
  is not an approval.
- **An unparseable line is logged and skipped**, never fatal.

### 3.1 Swift → Python

| Method | Kind | Payload |
|---|---|---|
| `session.hello` | req | `{proto:1, app:"0.1.0", caps:["stt.local","hotkey","confirm.visual"]}` → res `{proto:1, daa:"0.1.0"}` |
| `mic.utterance` | ev | `{text, confidence, startedAt, complete, addressed}` |
| `mic.onset` | ev | `{}` — speech started; drives barge-in, must be cheap |
| `control.text` | ev | `{text, addressed:true}` — typed into the dock |
| `control.alwaysOn` | req | `{on:bool}` → res `{on:bool}` (the **actual** state; the dock reverts its switch if this disagrees) |
| `control.cancel` | ev | `{}` — Esc; abandon the current turn, answer nothing |
| `undo.last` | req | `{}` → res `{ok:bool, summary:str}` |
| `doctor` | req | `{}` → res: the `daa doctor` payload, structured |
| `session.shutdown` | req | `{}` → res, then exit |
| *(response)* | res | the answer to `confirm.request` — see §4 |

Unimplemented methods should answer `ok:false` rather than staying silent; the
dock times a request out at 30 s and shows the failure.

### 3.2 Python → Swift

| Method | Kind | Payload |
|---|---|---|
| `ready` | ev | see below |
| `state` | ev | `{phase, detail?, since, tasks?}` |
| `audit` | ev | `{kind, id, at, payload}` — a verbatim redacted `AuditEvent` |
| `speak` | ev | `{text}` — daa's answer. **Nothing is said out loud**: the dock writes it into the transcript as a `daa` line. Send the matching `spoke` audit record too if you have one; the dock collapses the pair into a single line |
| `confirm.request` | **req** | the approval card — §4 |
| `confirm.cancel` | ev | `{id, reason}` — withdraw a pending card |
| `task.update` | ev | `{id, title, progress?, cancellable, startedAt, tier}` |
| `task.done` | ev | `{id, outcome}` |

**`ready`** — the dock's whole boot state in one frame, sent immediately after
the `session.hello` response:

```json
{"daa":"0.1.0","dryRun":true,"alwaysOn":false,"jevLive":false,
 "providers":{"mic":"fake","stt":"fake","llm":"live","jev":"live"},
 "tools":[{"name":"run_applescript","floor":"CONFIRM_VISUAL"}],
 "missing":["pyobjc-framework-AVFoundation"]}
```

`dryRun` **must** be present. The Swift side defaults a missing `dryRun` to
`true` and shows the DRY RUN pill, because if the dock cannot tell whether the
thing behind it is live, it shows the safer of the two. Do not rely on
that default.

`providers` values are compared against the literal `"live"`; anything else
renders as a fake and is surfaced in the Set-up tab.

**`state.phase`** ∈ `idle | listening | thinking | awaiting | working |
degraded`. An unrecognised phase renders as `degraded`, not as `idle`: a dock
that looks calm for a state it does not understand is lying. There is **no
`speaking` phase** — daa does not talk, and `"speaking"` on the wire is an
unknown phase like any other, so it renders as `degraded`. While daa is
composing an answer the phase is `thinking`; the answer itself is a `speak`
frame, and afterwards the phase goes back to `idle`.

**`audit`** kinds the dock projects into transcript lines:

| kind | becomes | reads |
|---|---|---|
| `heard` | **nothing** | it carries `_shape` (chars/words/sha256_8), never content, and must not pretend otherwise |
| `woke` | a `you` line | needs `payload.text` |
| `spoke` | a `daa` line | needs `payload.text` |
| `execution` | a `daa` line | `payload.summary`, `payload.tool`, `payload.undo_id`, `payload.dry_run` |
| `dry_run` | a quiet `daa` line | `payload.summary`, `payload.tool` |
| `refused`, `abandoned`, `undo_rejected`, `deferred_visual` | a refusal line | `payload.reason` |
| `error` | a refusal line | `payload.where`, `payload.error` |

The **`↩︎` undo affordance appears only when `execution` carried
`payload.undo_id` and `dry_run` was false.** If that field is not emitted
today, emit it — it is the difference between an undo button that works and
one that is decoration.

`payload.synthetic` on any record badges it SYNTHETIC in the History window.

---

## 4. `confirm.request` — the part that matters

```json
{"t":"req","id":"cf_3b91c0ad","m":"confirm.request","p":{
  "tool":"run_applescript",
  "tier":"CONFIRM_VISUAL",
  "reason":"unrecoverable and not explicitly requested",
  "phrase":"run a script that sends a message to Alex",
  "verb":"run",
  "explicit":false,
  "dryRun":true,
  "targets":["a script of 3 lines"],
  "args":[
    {"key":"script","isProgram":true,
     "value":"tell application \"Messages\"\n  send \"on my way\" to buddy \"Alex\"\nend tell"},
    {"key":"recipient","isProgram":false,"value":"Alex"}
  ],
  "consequences":{"send":"sending this text: 'on my way'"},
  "assessment":{"blastRadius":2.4,"unrecoverable":0.81,"explicitlyRequested":0.19,
                "targetConfidence":"probable","confidence":0.77,"synthetic":false},
  "expiresInMs":90000
}}
```

Built from the objects the `present(action, disposition)` hook receives:

| field | from |
|---|---|
| `phrase` | `_phrase(action)` |
| `verb` | `action.verb` |
| `explicit` | `action.explicit` |
| `targets` | `[str(t) for t in action.targets]` |
| `args[].key/value` | `action.args`, every one, in full |
| `args[].isProgram` | `key in _SCRIPT_KEYS` |
| `consequences` | `action.consequences` |
| `tier` / `reason` | `disposition.tier.name`, `disposition.reason` |
| `assessment.*` | `disposition.assessment` (`blast_radius`, `unrecoverable`, `explicitly_requested`, `target_confidence`, `confidence`, `synthetic`) |
| `dryRun` | `settings.dry_run` |

### The response

```json
{"t":"res","id":"cf_3b91c0ad","ok":true,"p":{"granted":true,"reason":"approved"}}
```

`reason` is one of `approved`, `cancelled`, `escaped`, `timeout`,
`withdrawn: …`, `brain stopped`, `unreadable request`. Audit it: a card that
timed out is **not** the same event as a user who said no, and `daa doctor`
and the History window should be able to tell them apart.

### Requirements on Python

1. **`id` is a fresh random token, minted per card, single-use.** A response
   with an unknown, already-consumed or stale id is **dropped and audited as
   an error** (`_emit("error", where="confirm", ...)`). The model cannot mint
   one; a forged `undo.jsonl` row cannot mint one.
2. **90-second fail-closed timeout.** `console.ask()` on a terminal blocks
   forever; `present()` must not. On expiry: send `confirm.cancel`, return
   `False`, and let the existing code emit `visual_confirm granted=False` and
   answer "Okay, leaving it." as a `speak` frame. *An approval you walked away
   from is not an approval.* Audit it with `reason="timeout"` so it is distinguishable from a
   refusal.
3. **A dead or unresponsive dock is a refusal.** If the writer cannot send, or
   stdin has closed, `present()` returns `False` immediately.
4. **`present()` must be re-entrant-safe with `_next_reply()`.** It blocks the
   turn thread waiting on a queue keyed by the token; it must not consume a
   scripted reply meant for the foreground turn.

### What the dock guarantees back

These are enforced in Swift and covered by
`ui/Sources/DaaDockTests/ApprovalTests.swift` (70 tests, 632 assertions):

- **exactly one response per request** — a double-click, a hold completing as
  the timeout fires, and a `confirm.cancel` racing a hold all produce one
  frame (`PendingApprovals`, `ConfirmRouter`);
- **a frame the dock cannot render in full is answered `granted:false`** and
  no card is shown — there is no consent to something that was not displayed;
- **a second card arriving while one is up is refused**, not stacked or
  queued — stacking lets a click aimed at one land on the other, and queueing
  would run the queued card's countdown while it was invisible;
- **approval requires a completed 600 ms hold.** No default button, no
  keyboard shortcut on Approve, Esc = Cancel;
- **the card is inert for its first 400 ms**;
- **Approve is disabled until the script block has been scrolled to the end**;
- **every line of every argument is rendered**, never truncated, never behind
  a disclosure triangle;
- **child death, dock quit and `confirm.cancel` all resolve to
  `granted:false`.**

---

## 5. Field-name checklist

Names the Swift decoder reads verbatim. A typo here is a field that silently
renders as its worst-case default.

`ready`: `daa`, `dryRun`, `alwaysOn`, `jevLive`, `providers`, `tools[].name`,
`tools[].floor`, `missing`
`state`: `phase`, `detail`, `since`, `tasks`
`audit`: `kind`, `id`, `at`, `payload`
`audit.payload`: `text`, `tool`, `summary`, `undo_id`, `dry_run`, `synthetic`,
`reason`, `where`, `error`
`speak`: `text`
`confirm.request`: `tool`, `tier`, `reason`, `phrase`, `verb`, `explicit`,
`dryRun`, `targets`, `args[].key`, `args[].isProgram`, `args[].value`,
`consequences`, `assessment.blastRadius`, `assessment.unrecoverable`,
`assessment.explicitlyRequested`, `assessment.targetConfidence`,
`assessment.confidence`, `assessment.synthetic`, `expiresInMs`
`task.update`: `id`, `title`, `progress`, `cancellable`, `startedAt`, `tier`

Defaults when a field is missing, so you know what silence costs:

| missing | dock assumes |
|---|---|
| `dryRun` (ready) | `true` — pill shown |
| `explicit` | `false` — "I inferred this" flag shown |
| `assessment` | blast radius 3, confidence 0, **synthetic true** |
| `phase` | `degraded` |
| `expiresInMs` | 90 s, and any value is clamped to 5–300 s |
| `isProgram` | falls back to `key in _SCRIPT_KEYS` |

---

## 6. Things the dock does NOT want

- **Do not send raw utterance text in `heard`.** The dock will not display it,
  and sending it would move content across a boundary `_shape` exists to hold.
- **Do not `setsid`, double-fork, daemonize, or call
  `responsibility_spawnattrs_setdisclaim`.** The bridge must stay an ordinary
  child of the app or every TCC grant moves back onto the interpreter, which
  is the entire reason this architecture exists.
- **Do not open an audio device.** Swift owns the microphone, the VAD and the
  local STT. `providers.mic` should report what the *bridge* would use, which
  is the `BridgeMic`.
- **Do not put policy in the protocol.** The dock renders `tier` and `reason`
  as strings; it never compares them, never orders them, and must never be
  given a field it is expected to reason about. `make -C ui lint` greps the
  Swift sources for that vocabulary and fails if it appears.
