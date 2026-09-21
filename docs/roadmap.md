# daa roadmap — computer use, browser use, dock UI

Synthesis of four planning passes (`plan-async-and-consent.md`,
`plan-browser-use.md`, `plan-computer-use.md`, `plan-dock-ui.md`).
This document is the sequence; the four plans are the detail.

---

## The shape all four converged on

None of the planners talked to each other, and all four landed on the same
principle from different directions:

> **Static, human-authored guarantees are the floor. Computed refinements may
> only raise them. Never the reverse.**

- `spec.floor` over `floor_hint` over model judgment (safety)
- a named AX element over a coordinate (computer use)
- a statically-floored `submit_form` over a resolver-detected payment (browser)
- a `RiskTier` ceiling on a grant, which can only *answer* a confirmation
  policy already demanded, never lower one (consent)

That convergence is the main reason to trust the plan. It is the same rule the
existing 799-test safety model already enforces, arrived at independently four
more times.

---

## Stage 0 — before any code (BLOCKING)

Two items, neither of which is programming, both of which get more expensive
the longer they wait.

### 0a. Code signing — do this first or throw away every permission twice

Verified experimentally: an ad-hoc signature's designated requirement **is**
the cdhash, so it changes on every rebuild (`f0b85f…` → `dcf628…` after a
one-string edit). TCC keys grants to the DR. This machine has **zero**
codesigning identities.

Consequence: every TCC grant made before we have a stable signing identity is
thrown away on the next rebuild. The Screen Recording grant currently sitting
on `.venv/bin/python` is already in this category.

**Action:** Apple Developer Program, then sign local builds `Apple Development`
with a frozen bundle id. A purchase and a flag, not code.

### 0b. Two experiments that change the design

1. **Does `kAXTitleAttribute` give window titles with Accessibility alone?**
   If yes, we design Screen Recording *out of the product entirely* — no
   screenshots, no vision grounding, no monthly re-prompt, and the
   "screenshot contains a TCC Allow button" escalation becomes unreachable
   rather than mitigated.
2. **Is macOS 26's `SpeechTranscriber` good enough?** Score it on
   `evals/address_gate_cases.jsonl`. If yes it replaces the `LocalTranscriber`
   stub *and* deletes `silero-vad` + `torch` + `sounddevice` — the difference
   between a ~100 MB bundle and a multi-GB one. Fallback: FluidAudio (Apache-2.0).

---

## Stage 1 — the dock (~6 days to something you use daily)

Native SwiftUI `.app`: `NSStatusItem` + non-activating `NSPanel`, spawning the
existing Python as a child over newline-delimited JSON on stdin/stdout. Swift
owns mic, VAD, STT, hotkey and pixels. Python keeps the entire brain, still
synchronous, still testable with zero hardware.

**Why this is first, and it is not because it is the fun part:**

- It relocates every TCC grant onto a signed `.app`. Do it later and we
  re-grant everything.
- Swift's Speech framework replaces the local-STT stub, which is the single
  thing currently making `daa listen` non-functional. No key fixes that; this
  does.
- Background jobs need somewhere to report to. Building them first would mean
  building a notification channel twice.
- It makes `CONFIRM_VISUAL` a real surface instead of a terminal prompt.

**Blast radius on existing code: ~8 lines** in `_confirm_visual`, preferring
`console.present(...)` when the console advertises it. `_VISUAL_OK` stays the
sole product of that one function. `AudioChunk.pcm` already documents carrying
non-PCM payloads for fakes, so a `BridgeMic` yielding Swift transcripts is the
sanctioned pattern and leaves `run()`, the gate, barge-in and `_next_reply()`
untouched.

**The approval card is the most safety-critical screen in the product.**
Verb-first, script-first, never truncated, hold-to-approve, no default button,
400 ms inert, scroll-gated, visible countdown, dry-run banner, `synthetic`
disclosure. It must be readable, not dismissible by reflex.

---

## Stage 2 — async and consent (no user-visible feature, everything depends on it)

Threads, not asyncio. **The agent thread proposes, the loop thread disposes:**
the worker yields a `ResolvedAction` and blocks; the loop thread computes the
disposition, checks the grant, prompts if needed, and returns a single-use
`Warrant` bound by sha256 to that exact action. Execution runs on the worker so
the loop stays free to hear "stop".

`LongRunningTool` is a **separate** protocol shaped as a generator, so an agent
structurally *cannot* call a tool — only request one. Adding a method to the
existing `Tool` protocol would break all 13 registrations (verified).

Prerequisites inside this stage:
- `UndoJournal` needs an internal `RLock` — 10 mutating methods, no lock, about
  to be touched from two threads.
- `_next_reply()` re-entrancy: a background job's consent prompt must not steal
  a scripted reply meant for the foreground turn.

Checkpoints replace stacked undo: a window of journal entry ids, rolled back in
reverse through the *existing* validation, stopping at the first stale row and
saying so honestly ("I put back four of the six").

---

## Stage 3 — browser, read-only (~4 days)

Playwright 1.63.0 driving a dedicated daa-owned Chrome profile at
`~/.daa/browser-profile`. The user logs in by hand, once; that enrolled set
*is* the grant — enumerable, speakable, revocable with one `rm -rf`.

Attaching to the user's real Chrome is not a tradeoff we get to weigh: Chrome
has required a non-default data directory since 136 and still does in 153, and
it **fails silently**.

Ships "read me this page" with no clicking at all. Text extraction behind one
swappable function; 15,000-char cap (the accessibility tree is 10× the page
text on Hacker News and 731,503 chars on one Wikipedia article — it is an
*acting* representation, not a reading one).

**New exposure this introduces:** summarising a logged-in page sends private
page content to DeepSeek. Separate `summarise_page` tool at `ANNOUNCE` so the
egress is announced *before* it happens; setting defaults to off.

---

## Stage 4 — browser, acting (~6 days)

`PageFacts` lands before any clicking, or `click_element` ships with a
selector-derived readback that is a lie and immediately becomes load-bearing.

- Readback composed from the live DOM: accessible name, form `method`/`action`
  host, field types (**never values**), origin, whether the profile holds a
  session for that eTLD+1.
- **The problem no file tool has:** the DOM moves between `resolve()` and
  "yes". Two seconds of spoken readback is enough for an SPA to replace the
  subtree. `resolve()` hashes a `dom_fingerprint`; `run()` recomputes it and
  aborts on mismatch.
- `click_element` (CONFIRM_VOICE) is split from `submit_form`
  (CONFIRM_VISUAL, `irreversible=True`), and `click_element.resolve()` refuses
  and routes when it finds a submitting control.
- **Log scheme + host + path, never query or fragment.** Query strings
  routinely carry magic-link tokens and OAuth `code`/`state` — a logged URL is
  frequently a working credential.

---

## Stage 5 — computer use, AX-first (~3 weeks)

pyobjc, zero new runtime dependencies. The unit of action is a **named
element, never a coordinate**.

`ui_click(target="the Save button", app="TextEdit")`. The spoken string is
composed from AX attributes only, so a click the model calls "the OK button"
reads back as *"press Delete Account in Account Settings in Safari"*.

If a coordinate is ever needed, `AXUIElementCopyElementAtPosition` hit-tests
it: **a coordinate that does not hit-test to a nameable element is a coordinate
you may not click.** That demotes any future vision model from authority to
suggester.

Non-negotiable: capture (if we keep any) gates on
`CGPreflightScreenCaptureAccess()`. Without permission
`CGWindowListCreateImage` **succeeds** and returns wallpaper plus live menu bar
with all window content silently removed — a forgery the agent cannot detect.

All `ui_*` tools take `inverses=()` with explicit irreversibility. ⌘Z is not an
inverse; it is another click whose meaning the app defines, and it can undo
something the user did by hand.

---

## Cut, or deferred

| | Why |
|---|---|
| Attach to the user's real Chrome | Chrome forbids it; the sanctioned path needs an Accessibility grant and leaves a *standing* grant in `Local State` |
| Vision/screenshot grounding | Design it out via `kAXTitleAttribute`. Removes a TCC grant, a monthly re-prompt, and a privilege-escalation path |
| Autonomous browse/click loops | Cannot be confirmed action-by-action; the whole safety model is per-action |
| Electron | 307 MB measured, wrong order of magnitude |
| Tauri | Good stack, but no Rust installed, `wry` #1848 freezes WKWebView frames on macOS 26, and tauri #11992 breaks notarization with the Python-sidecar path |
| `browser-use` | 61 `==`-pinned deps incl. `openai==2.26.0` vs our `>=2.36`; unsatisfiable, and architecturally the autonomous agent we reject |
| hermes `computer_use/` | 7,122 lines with no macOS code — an MCP client for a `curl\|bash` Rust binary using private SPIs. Its own prompt says "click element #7" |

---

## The risk that no test will catch

**Consent fatigue turning the grant into blanket permission — through
habituation, not through a bug.**

Every invariant holds under adversarial analysis, but invariants bound the
worst case. The typical case is a user hearing a structurally similar
four-clause sentence twenty times and saying "yeah" at clause two. Formally
correct, substantively blanket, and the suite stays green.

The current per-action model is immune to this in a way the grant model is
not: a readback naming the wrong *file* is jarring, one naming a slightly
too-wide *scope* is not.

**This gets measured in `evals/` before the grant ships:** can the user state,
60 seconds later, what they authorised? If that number is bad, the readback is
decoration and the grant model does not ship as designed.

---

## Honest totals

| Stage | Effort |
|---|---|
| 0 — signing + two experiments | ~1 day + a purchase |
| 1 — dock | ~6 days to daily-usable, ~15 to polished |
| 2 — async + consent | ~1 week |
| 3 — browser read-only | ~4 days |
| 4 — browser acting | ~6 days |
| 5 — computer use | ~3 weeks + 1–2 for packaging |

**Recommended v1 = stages 0–4.** That is the polished dock, a working `listen`,
and real browser capability — roughly 4 weeks. Computer use is the longest,
riskiest and least reversible stage, and stages 0–4 make it cheaper by closing
the capability gaps that currently push the model toward the shell escape.
