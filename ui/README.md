# The dock

A native SwiftUI menu-bar app that supervises the existing Python as an
ordinary child process over newline-delimited JSON on stdin/stdout. Swift owns
the microphone, the VAD, the local STT, the global hotkey and every pixel.
Python keeps the entire brain.

Stage 0b + Stage 1 of `docs/roadmap.md`. Follows `docs/plan-dock-ui.md`;
§"Where the plan was wrong" below records the four places it was not right.

**Nothing under `src/` or `tests/` was touched.** The one Python-side change
this needs is written out as an exact diff in [`INTEGRATION.md`](INTEGRATION.md).

---

## Stage 0b: is macOS 26's `SpeechTranscriber` good enough?

**Yes. It replaces the `LocalTranscriber` stub and deletes `silero-vad`,
`torch` and `sounddevice` from the shipping story.**

Method: synthesise all 40 utterances in `evals/address_gate_cases.jsonl` with
`say`, transcribe each through `SpeechAnalyzer` + `SpeechTranscriber` on
macOS 26.5.2, score against the labels. Reproduce with
`./experiments/speech/run.sh [voice] [rate]`.

| | Samantha @175 wpm | Alex @195 wpm |
|---|---|---|
| **WER** | **9.0%** (20/222 words) | **8.1%** (18/222) |
| exact match | 29/40 (72.5%) | 28/40 (70.0%) |
| latency, median | **100 ms** | 95 ms |
| latency, p90 / max | 115 / 130 ms | 107 / 115 ms |

Also verified: `SpeechTranscriber.isAvailable == true`; nine English locales
installed; **transcribing a file raises no TCC prompt at all**; and the
on-device model assets downloaded and installed themselves on first run via
`AssetInventory.assetInstallationRequest` in a few seconds. Per-category WER,
the full hypothesis list and the raw result JSON are in
`experiments/speech/`.

### The one finding that changes the product

**It never once transcribed the word "daa".** Across both voices, all five
utterances containing it: `hey daa` → "KDA" / "hey that"; `daa is the name…`
→ "DAA" / "dar"; `telling daa to open it` → "DAA" / "that"; and the two
deliberately-misheard eval cases (`day a`, `the a`) came back as "it opened" /
"they are" / "" / "d".

This is survivable only because daa's address gate is **semantic, not a wake
word** — `AddressGate` asks a judgment model whether an utterance was addressed
to the assistant, and every one of those hypotheses still reads as a command.
But it forecloses two things:

- a cheap wake-word fast path is not available; every always-on utterance costs
  a gate call;
- `AnalysisContext.contextualStrings` **did not help at all.** Biasing with
  `["daa", "hey daa", "undo", "undo that", "standup"]` produced a *byte-identical*
  result file — not one token moved. Either the API is not honoured for this
  module on 26.5, or its weight is far too low to rescue an out-of-vocabulary
  two-syllable name. Do not plan around it.

### The one finding that matters for safety

Short negations and short commands are the fragile class, and two errors
inverted meaning:

- `"undo that"` → **"don't do that"** (Samantha). A command to reverse became
  a refusal. Fails *safe* here — it will not trigger an undo — but it means
  spoken undo is unreliable, which is an argument for the dock's `↩︎` button
  rather than against it.
- `"alexa turn off the lights"` → **"lakes are turn off the lights"** (Alex).
  This one fails *open*: an utterance addressed to a different assistant lost
  its wake word and now reads as a bare imperative. It is the clearest
  label-flip risk in the set and it argues for keeping always-on off by default
  until the gate's numbers are real.

Everything else was cosmetic: dropped fillers (`ugh`, `uh`, `the`),
`standup`/`stand up`, `we're`/`we are`, `dot map`/`dotmap`.

**Verdict: adopt Apple Speech. FluidAudio is not needed.** Retest before
shipping always-on, on real speech rather than `say`, and specifically retest
the short-negation class.

### Caveats on these numbers, stated plainly

`say` produces clean, studio-quality, accent-neutral audio with no room, no
overlap and no disfluency. **These are a ceiling, not a forecast.** What they
establish is that the model is in the right class and that the API works
end-to-end on this machine; they do not establish field accuracy. The
interesting number — how the gate's `address_gate = 0.42` threshold behaves
when fed this transcriber's output instead of the stub's — cannot be measured
until Jev is live, and the README is explicit that no gate-quality number in
this repo is real yet.

---

## What got built

```
ui/
  Package.swift              three products: the app, the core, the tests
  Makefile                   build / test / app / demo / dr / lint
  INTEGRATION.md             the exact Python-side diff
  bundle/                    Info.plist, daa.entitlements
  tools/
    make-app.sh              assemble + sign + verify the DR
    fake-bridge              a complete fake Python peer (no keys, no network)
    gen-tests.py             regenerates the test registry
  experiments/speech/        Stage 0b, reproducible
  Sources/
    DaaDockCore/             every decision, no AppKit, fully tested
      WireProtocol.swift     frames, codec, byte-level line framing
      JSONValue.swift
      Payloads.swift         ready / state / audit / ApprovalCard / tasks
      ApprovalGate.swift     the card's rules + single-use tokens
      ConfirmRouter.swift    the wire→screen boundary
      DockState.swift        phases, menu-bar derivation, transcript projection
      CodeIdentity.swift     the cdhash check
      PythonLocator.swift    interpreter search + restart backoff
    DaaDock/                 the app: AppKit + SwiftUI, no decisions
      main.swift             LSUIElement bootstrap
      AppModel.swift         single source of truth
      PythonSupervisor.swift spawn, watch, restart, frame both ways
      StatusItemController.swift
      DockView.swift  Panels.swift  ApprovalCardView.swift
      HotkeyManager.swift    Carbon push-to-talk + Esc
      SpeechEngine.swift     mic, VAD, on-device STT
      HistoryWindow.swift    history + the Set-up tab
    MicroTest/               a ~120-line XCTest stand-in (see below)
    DaaDockTests/            70 tests, 632 assertions
```

### States

`idle · listening · thinking · speaking · awaiting · working · degraded`,
derived in `DockState.menuBar` and tested:

- **amber is the only colour the icon ever takes.** If the icon has colour,
  something needs a human.
- **approval always outranks progress.** A card open during background work
  still shows amber.
- **a hot mic never looks like a cold one** — always-on at idle draws a
  hairline ring.
- **an unrecognised phase renders as `degraded`, not `idle`.** A dock that
  looks calm for a state it does not understand is lying about the machine
  behind it.

### The privacy boundary is visible

*"Speech is not written down until it is addressed to you."* Live partial text
is shown **only** under push-to-talk, where holding a key is itself an
unambiguous act of address. In always-on mode the dock shows a level meter and
the words *"listening — nothing written down yet"*, and the switch is gated
behind a one-time explainer whose every sentence is enforced in `handle_chunk`.

### The approval card

`ApprovalCardView.swift`, rules in `ApprovalGate.swift`:

- **verb first**, largest type on the card, straight from `_phrase(action)` —
  because "delete report.pdf" and "reveal report.pdf" read back identically if
  you lead with the filename;
- **script args first**, labelled `THIS RUNS`, monospaced, **never truncated**,
  never behind a disclosure triangle. A 400-line script makes the card scroll;
- **every argument in full**, sorted, script keys first;
- **consequences ⚑-flagged** at the top, in prose, **most destructive first**
  (`ConsequenceOrder`; an explicit `consequenceOrder` array from Python wins);
- **the claim is pinned**: header, verb phrase, message and flags sit outside the
  scroll view, so at the bottom of a 400-line script you are still looking at
  what daa says it will do. Only the script and the detail below it scroll;
- **what is sent, and to whom, is shown large** on a send-message card (from a
  `message: {to, body}` field, or the "sending this text:" consequence);
- **"I inferred this — you didn't ask for it"** when `explicit == false`;
- **synthetic judgment disclosed in plain words**: *"daa could not get a real
  judgment, so it is assuming the worst"*;
- **press-and-hold 600 ms** with a fill. **No default button. No keyboard
  shortcut on Approve. Esc = Cancel**, and it is the only shortcut on the card;
- **inert for the first 400 ms** — kills the card-under-a-descending-cursor case;
- **Approve disabled until the script has been scrolled to the end**;
- **visible countdown**, and the card says *"doing nothing is a no"*;
- **a stakes banner in BOTH states**: dry run says *"approving this will not
  actually run it"*; live says *"This will really happen."* in red, and the live
  Approve control is red, heavier and reads *"Approve for real · hold"*. A live
  card is never the calmer of the two;
- **the panel is clamped to the screen's `visibleFrame`** (`PanelPlacement`):
  on a 1024×665 "Larger Text" display the scroll region shrinks and Cancel /
  Approve stay on screen, below the menu bar;
- **exactly one response per request**, guaranteed by `PendingApprovals`;
- **a frame that cannot be rendered in full is refused and never shown.**

### The cdhash feature

Ad-hoc signing was chosen deliberately, so the failure mode is built as a
feature rather than left as a surprise. At launch — **before anything can ask
for a permission** — the app reads its own cdhash via
`SecCodeCopySigningInformation`, compares it with the last-seen value persisted
in `~/Library/Application Support/ai.daa.dock/identity.json` (outside the
bundle, so nothing breaks the seal), and shows one of five notices.

Verified on this machine: ad-hoc, one string changed in `Info.plist`, rebuilt.

```
before  CDHash=7adad5b451550945926d4b2ed8b0952d2999b060
after   CDHash=3b359eae1d3ed9be2cdbeace95bee7bc1833a39d
```

and `codesign --display -r -` prints `designated => cdhash H"7adad5b4…"` — the
DR *is* the hash. So the app says, in the banner and in the Set-up tab:

> **daa was rebuilt. macOS has forgotten its permissions.**
> … You will be asked for the microphone again. Accessibility and Automation
> have no prompt at all — they will simply not work until you re-add daa in
> System Settings, and daa cannot tell you which one failed until it tries.

The five cases (first-run ad-hoc, unchanged, rebuilt ad-hoc, rebuilt with a
stable certificate, downgraded from certificate to ad-hoc) are covered by
`CodeIdentityTests`.

---

## Running it

```bash
cd ui
make test         # 70 tests, no Xcode needed
make app          # build/daa.app, ad-hoc signed, entitlements verified
make demo         # run it against the fake bridge: no keys, no network
make dr           # the designated requirement — run it twice across builds
make lint         # fail if policy vocabulary appears in Swift
```

`make demo` is the useful one. It launches the real app against
`tools/fake-bridge`, a ~280-line dependency-free Python peer that speaks the
whole protocol and touches nothing. Click the menu-bar item, type into the
dock:

| type | you get |
|---|---|
| `script` | a real CONFIRM_VISUAL card with a 3-line AppleScript |
| `long` | the same with a **400-line** script — exercises the scroll gate |
| `synthetic` | a card whose judgment is synthetic (i.e. FakeJev) |
| `live` | a card with `dryRun:false` — red "This will really happen." banner |
| `withdraw` | a card Python takes back after 3 s (`confirm.cancel`) |
| `task` | a background job reporting progress into the task strip |
| `crash` | the child exits — watch the backoff and the degraded state |
| anything else | an ordinary turn: thinking → speaking → execution with an ↩︎ |

Against the real thing, once `daa bridge` exists, it finds
`<repo>/.venv/bin/python3` on its own; `DAA_PYTHON` and `DAA_REPO_ROOT`
override.

### Signing

```bash
DAA_SIGN_IDENTITY="Apple Development: you@example.com (TEAMID)" make app
```

Until that exists, every grant is thrown away on the next build and the app
says so.

---

## Looking at it without running it: `make snapshots`

Renders every screen -- all approval-card states, every panel phase, the
menu-bar glyph, the cdhash notice, tasks, transcript, History -- in light and
dark, to `build/snapshots/*.png`, with `build/snapshots/index.html` laying
them out side by side. `ONLY=card make snapshots` renders a subset (and leaves
the index alone).

Each view draws itself into a bitmap from an `NSWindow` that is never ordered
on screen (`NSView.cacheDisplay`). That is not a screenshot: no Screen
Recording, no TCC prompt, no running app, no Python. The card payloads are
transcribed from `tools/fake-bridge` and go through `FrameCodec` +
`ConfirmRouter` exactly as in the app (`Sources/DaaDockSnapshots/Fixtures.swift`;
keep it in sync).

SwiftUI `ImageRenderer` was tried first and works under Command Line Tools, but
draws every AppKit-backed control -- including the `ScrollView` that holds the
whole card body -- as a yellow placeholder. Two `imagerenderer-*` images are
kept to show that.

Offscreen artefacts, so nobody files them as design bugs: materials
(`.regularMaterial`) render as flat grey with nothing behind them; the
`borderedProminent` button and an ON switch draw in their inactive-window
(grey) style; animation is frozen; `dynamicTypeSize` has no effect on macOS.

To make this possible the views moved from the `DaaDock` executable into a
`DaaDockUI` library (`main.swift` is now only the entry point), the status-item
drawing was lifted into `MenuBarGlyph`, and `IdentityBanner`, `AuditRow` and
`HistoryView` gained initialisers for their initial disclosure/tab state.
Nothing renders differently.

## Where the plan was wrong

Four things `docs/plan-dock-ui.md` got wrong, found by building it.

### 1. `swift test` does not work with Command Line Tools

The plan verified that `swiftc` and `swift build -c release` work without
Xcode, and concluded Xcode was not a blocker. It is not — for *building*. But
**CLT ships no XCTest and no swift-testing**, so `swift test` fails at
`import XCTest`, and there is no runtime test discovery either.

Rather than ship an untestable UI, `Sources/MicroTest/` is a ~120-line
stand-in providing the slice of the XCTest API the suite uses, and the suite
is an ordinary executable (`swift run daadock-tests`) with an explicitly
generated registration list (`tools/gen-tests.py`). The assertion names are
XCTest's on purpose: when Xcode is installed, converting to a real
`.testTarget` is deleting one file and changing one import.

### 2. The `_confirm_visual` diff in the plan no longer applies

`loop.py` has moved since the plan was written: the block now has
`self._confirming += 1` and a `finally:` around it. The diff in
`INTEGRATION.md` is against the real current code.

The plan's version also wrote `typed = "yes" if present(...) else ""`. That is
a truthiness test, so a `present` that returns a non-empty string, a `Mock`, or
any other truthy object would approve. The corrected diff uses
`if present(...) is True`, because the safe reading of a bug on this path is
"not approved".

### 3. The scroll gate cannot be built with `onAppear`

The obvious implementation — a sentinel view at the bottom of the scroll
content with `.onAppear { markSeen() }` — silently deletes the rule. A plain
`VStack` inside a `ScrollView` renders every child eagerly, so `onAppear` fires
for content that has never been on screen. The gate is measured against real
scroll geometry (`onScrollGeometryChange`) instead. This is exactly the class
of bug the plan's Risk 3 warns about, arriving through a different door: the
card looks right and the friction is gone.

### 4. Stacking approval cards is not safe, and the plan did not say what to do

The plan specified one card's behaviour and never said what happens when a
second `confirm.request` arrives while one is up — which stage 2's background
jobs make routine. Stacking lets a click aimed at one card land on the other
(the same click-through the 400 ms inert window exists to prevent, arriving by
a different route), and queueing would run the queued card's countdown while it
was invisible, so it could expire without ever having been seen. The dock
**refuses the second card** and lets Python ask again. `ConfirmRouter` owns
that decision and it is tested.

### Smaller notes

- The plan put the protocol's `expiresInMs` entirely in Python's hands. The
  dock clamps it to 5–300 s: a peer claiming a ten-hour window does not get to
  leave an approval card open overnight.
- The plan did not mention that a `res` frame might arrive without `ok`. The
  codec decodes that as an error, never as success.
- `HotkeyManager` cannot clean up in `deinit` — Carbon's `EventHotKeyRef` is
  not `Sendable` and a nonisolated `deinit` may not touch it under Swift 6
  concurrency. Cleanup is explicit in `applicationWillTerminate`.

---

## Not verified

Honest list of what could not be checked on this machine.

- **Anything requiring a signing identity.** There are zero on this machine, so
  the `rebuiltStable` and `downgraded` paths are covered only by unit tests
  against synthetic identities, and no claim that grants survive a rebuild has
  been demonstrated end-to-end.
- **The microphone path.** `SpeechEngine` compiles and its APIs are verified
  present in the 26.5 SDK, but it was never run: doing so would raise a
  microphone TCC prompt, which is outside what this work should trigger. File
  transcription was exercised heavily (§Stage 0b) and uses the same
  `SpeechAnalyzer` + `SpeechTranscriber` path; the live parts —
  `AVAudioEngine`, the format conversion, `SpeechDetector` onset, and the
  volatile/final result split in `isVolatile` — are **untested**.
- **TCC attribution.** `sudo launchctl procinfo <pid> | grep -i responsible`
  is the check, and it needs root. What *was* verified is the process shape it
  depends on: the bridge is a plain child (`ppid` == the app's pid), there is
  no `setsid`, no double-fork and no disclaim anywhere in the source, and the
  child does not outlive the parent.
- **Pixels.** No Xcode means no SwiftUI previews, and screenshotting needs a
  Screen Recording grant this work will not take. Layout, spacing, Liquid
  Glass materials, Reduce Motion, Reduce Transparency and the VoiceOver pass
  on the approval card have **not been looked at**. `make demo` is how to do
  that; expect to spend time on it.
- **The real `daa bridge`.** It does not exist yet. Everything here was
  exercised against `tools/fake-bridge`.
- **macOS 27.** This machine is 26.5.2. `LSMinimumSystemVersion` is 26.0.
