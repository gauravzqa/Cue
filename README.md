# daa

A voice-driven macOS assistant. **DeepSeek converses, Jev judges, typed tools act.**

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
cp .env.example .env          # nothing here is required to run the tests
.venv/bin/python -m daa.cli doctor
.venv/bin/python -m daa.cli say "open safari"
```

`daa doctor` prints what is wired, what is live and what is fake. Everything
runs with no API keys at all — every provider has an offline fake, and the
1,700-test suite needs no network, no audio hardware and no permissions.

---

## The pipeline

```
mic ─► VAD ─► local STT ─► Jev address gate ─┬─ not addressed ─► dropped, never leaves the laptop
                                             │
                                          addressed
                                             ▼
                       cloud STT rescore ─► Jev tool router ─► DeepSeek (non-thinking)
                                             ▼
                              tool.resolve() ─► Jev risk gate ─► policy.decide()
                                             ▼
        SILENT: run  │  ANNOUNCE: run + say  │  CONFIRM_VOICE: read back, wait for yes
                     │  CONFIRM_VISUAL: print everything, require a TYPED yes
                                             ▼
                          run ─► UndoJournal.record ─► the reply, as text
```

daa listens but does not talk back. Every line it produces — readbacks,
refusals, results, notices — is written: `daa> …` in the CLI, and a `speak`
frame the dock renders in its transcript. There is no audio out and no TTS
provider to configure.

### Why Jev and not just an LLM

[Jev](https://typesafe.ai) is a System One model: unstructured state plus typed
questions in, typed probabilistic decisions out, in one parallel pass, ~70–500ms,
and it cannot hallucinate or emit a type error. A voice assistant is drowning in
exactly its workload — high-volume repeated decisions over a shared state where
the answer space is known up front. Five of them here:

| Decision | Primitive |
|---|---|
| Is this addressed to me? | `Noul` |
| Has the user finished their thought? | `Noul` |
| Which tools does this need? | `Choice` |
| How dangerous is this *resolved* action? | `Score` + `Noul` ×2 + `Choice` |
| Did they actually say yes? | `Noul` |

The address gate is what makes an always-on mic viable: at ~100ms and ~$0.00004
per utterance, you can afford to ask on every utterance. An LLM round trip per
utterance is not affordable in either latency or money.

Because Jev's probabilities are **calibrated**, the thresholds in `config.py`
are tunable against real outcomes instead of guessed. See `evals/` — and tune
them, because a guess here is expensive in a direction nobody notices.
`confirm_yes` was 0.90 and sat *above* where Jev puts an ordinary spoken "yes"
(0.79–0.95), so it rejected 23 of 60 clear consents while catching no refusal
at all; `evals/run_consent.py` measures the gap and it is now 0.72.

---

## The security model

This is the part to read before changing anything.

### 1. Two-phase tools

Every tool implements `resolve()` then `run()`. `resolve()` is **always safe** —
it turns "the screenshots from today" into concrete targets and mutates nothing.
The risk gate and the spoken confirmation both run on its output. Confirming
against the raw utterance instead of the resolved targets is how you delete the
wrong folder.

### 2. The one-way tier rule

`tier = max(spec.floor, derived_from_judgment)`. Policy may only ever **raise**
a tier, never lower it. A confident Jev answer saying "this is harmless" cannot
turn `move_to_trash` into a silent execution — the floor is about the tool's
reach, the judgment is about this invocation. Verified across 52,800
combinations including NaN, out-of-range and relabelled inputs.

Policy also **never invents `REFUSE`**. "Never automate this" is a human's
authored property of a tool, not something a runtime score may assert.

### 3. Everything fails closed

Jev unavailable → maximally pessimistic assessment, not a permissive one. Gate
error → no wake. Confirm error → unclear → abandon. Unknown tool → refuse and
keep the session alive. `assessment is None` never means "proceed".

### 4. The confirmation must not be able to lie

`ResolvedAction` carries a `verb` and a `consequences` map, and `describe()` is
verb-first. Before this, "delete report.pdf" and "reveal report.pdf" read back
**identically** as *"report.pdf — should I go ahead?"*. A destructive modifier
that is known at resolve time and not spoken makes the confirmation a lie, so
the type makes it hard to drop:

```
"move to the Trash myapp, including 401 items inside myapp"
"move a.txt to dest, replacing 1 file already there, which I will put in the
 Trash first so you can get it back"
"run the shortcut Text My Partner, sending this text: '...'"
```

`run_applescript` derives its readback from **what the script actually does**,
never from the model's stated purpose — otherwise the sentence meant to let you
check the model is written by the model.

### 5. The undo journal is untrusted input

`~/.daa/undo.jsonl` is a file on disk. Anything running as you can append to it.
It is treated exactly like LLM output:

- `0o700` dir, `0o600` files, tightened in place on load (not grandfathered)
- every row records `produced_by`, and `entry.tool` must appear in that tool's
  declared `ToolSpec.inverses` or the row is refused
- undo **never** executes below `CONFIRM_VOICE`, whatever the inverse tool's
  floor — the instruction came from a file, not from your mouth
- `peek → run → commit`: a dry-run, refused or failed undo keeps its entry

### 6. Speech is not written down until it is addressed to you

The `heard` event carries a shape (`chars`, `words`, `sha256_8`) and no text.
The transcript is emitted only after the gate wakes. Redaction in the audit sink
is the *second* line of defence, not the only one — by key, by scope (element-wise
into containers) and by shape (Luhn-checked card numbers, `sk-`/`ghp_`/`AKIA`
keys, PEM headers).

---

## Layout

```
contracts.py   the keystone: every type crossing a subsystem boundary
config.py      frozen Settings; all Jev thresholds are named fields
jev/           judgment. Imports nothing from daa except contracts/config.
tools/         macOS actions. Never imports jev/ or voice/.
safety/        policy, undo journal, audit. Pure; imports no sibling.
voice/         mic, STT, the agent loop. The ONLY module that may import all three.
ui/            the dock protocol + `daa bridge` (Swift app lives in ../ui)
evals/         labelled address-gate dataset + scoring harness
```

Layer isolation is mechanically verified — `import daa.tools` pulls in zero
sibling subsystems, and `import daa.jev` no longer drags in the HTTP stack.

---

## State of things

**Works end to end, with fakes:** the whole pipeline; the safety model above;
29 tools (13 macOS + 11 browser + 5 computer use); `doctor` / `say` / `listen`
/ `undo` / `bridge`; the SwiftUI dock (`cd ui && make run`).

**The brain is multi-step.** The model calls one tool, sees what happened, and
calls the next — which is what lets it recover from "I could not find the ok
button" by looking first. Bounded by steps (8), wall clock (45s), and a stop
when the same call repeats. The router runs again before each step and is asked
a *different* question from step two onward — "which tool finishes what is
left", not "which tool does this utterance need" — because a compound
instruction is dominated by its first clause and re-asking it returns the same
answer forever.

A tool result may carry only daa's own sentence about the step, the SHAPE of
the data (counts, flags, numbers), and the model's own arguments echoed back.
Never the data. Page text, control labels and clipboard contents do not reach
the model — and the result SAYS SO, because `ok` plus a word count reads as "I
read the page" and a model that is not told otherwise will state what a page
does not contain. `summarise_page` is the one tool that sends text: ANNOUNCE,
refused on a logged-in page, never answerable by a scoped grant. That exception
is keyed on the tool's spec tag, so no tool can elect itself into it.

**Capabilities are off by default.** `DAA_ENABLE_BROWSER` needs
`uv pip install -e ".[browser]"` plus `playwright install chromium`;
`DAA_ENABLE_COMPUTER_USE` needs Accessibility granted. Each adds a class of
action the model can reach for, so turning one on is a decision.

**Needs keys to be real:** with no `TYPESAFE_API_KEY` every Jev judgment is
`FakeJev` answering at maximum uncertainty, so the gate never wakes and every
confirmation reads "unclear". That is fail-closed working, and it means no gate
quality number is real until you run `evals/run_address_gate.py --live`.

**Not verified on a real screen:** the computer-use success paths pass against
a real AppKit window, but only with the screen UNLOCKED — macOS hides windows
from Accessibility otherwise, and that suite skips. Nobody has measured what
fraction of real apps expose a nameable tree (`evals/ax_coverage.py`, needs the
grant). The approval card has been rendered and reviewed as images but never
seen on a live display.

**Known gaps:** ad-hoc signing means macOS forgets the app's permissions on
every rebuild (the app says so at launch); no journal pruning; Chromium
detection still goes by app name in one place; a button press reports
"possibly did nothing" even when it worked.
