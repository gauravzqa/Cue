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
666-test suite needs no network, no audio hardware and no permissions.

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
                          run ─► UndoJournal.record ─► TTS
```

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
are tunable against real outcomes instead of guessed. See `evals/`.

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
voice/         mic, STT, TTS, the loop. The ONLY module that may import all three.
evals/         labelled address-gate dataset + scoring harness
```

Layer isolation is mechanically verified — `import daa.tools` pulls in zero
sibling subsystems, and `import daa.jev` no longer drags in the HTTP stack.

---

## State of things

**Done and verified:** the whole pipeline end to end with fakes; 666 tests; the
safety model above; 13 macOS tools; `doctor` / `say` / `listen` / `undo`.

**Needs keys to be real:** every Jev judgment is currently `FakeJev`, which
answers at maximum uncertainty — so with no `TYPESAFE_API_KEY` the gate never
wakes and the confirm parser says "unclear". That is fail-closed working
correctly, and it means **no gate quality number in this repo is real yet.**
Run `evals/run_address_gate.py --live` once you have a key.

**Not built:** local STT is a stub with a seam for whisper.cpp/Parakeet; the
`CONFIRM_VISUAL` approval is a terminal prompt, not a GUI card; no journal
pruning, so a consumed `set_clipboard` undo keeps the previous clipboard in the
0600 journal indefinitely.

**TCC permissions on this machine are all denied**, so `focus_window` raising a
specific window, window titles, AppleScript driving an app, and real shortcut
execution have never run live. Their degradation paths are tested; their success
paths are not.
