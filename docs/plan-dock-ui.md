# The dock: a plan

Research + recommendation, 2026-09-21. **Nothing here is built.** No file under
`src/` or `tests/` was touched.

Every claim is marked **VERIFIED** (I ran it on this machine, or read it out of
an SDK / registry / Apple doc), **VERIFIED-secondary** (credible source, no
primary), or **INFERRED** (my reasoning). Two research passes backed this;
where they disagreed I say so.

---

## Recommendation, in one paragraph

Build the dock as a **native SwiftUI `.app`** — an `NSStatusItem` in the menu
bar plus a non-activating floating `NSPanel` — that **supervises the existing
Python as an ordinary child process and talks to it over newline-delimited
JSON on stdin/stdout**. The Swift app owns the microphone, the VAD, the local
STT (macOS 26's `SpeechAnalyzer`/`SpeechDetector`/`SpeechTranscriber` are
present in the SDK on this machine — VERIFIED), the global push-to-talk
hotkey, and every pixel; Python keeps the entire brain exactly as it is, still
synchronous, still testable with zero hardware. The integration needs **two
seams that already exist** — the injectable `audit` sink becomes the UI's event
stream, and the `console` protocol becomes the approval card — plus one
~8-line addition to `_confirm_visual` so the card gets structured data instead
of a pre-rendered text blob. This shape is chosen primarily for **TCC**: macOS
attributes privacy grants to the *responsible process*, a signed `.app` is the
responsible process for its non-disclaimed children, and so the microphone,
Accessibility and Automation grants land on **one stable bundle identifier**
that survives rebuilds — which `.venv/bin/python` never can. Do not build the
UI from scratch: fork **yap** (MIT) for the panel/hotkey skeleton, lift
**Hex's** push-to-talk state machine (MIT), and lift **AudioWhisper's**
stdio-JSON-RPC-to-Python layer (MIT). Reject Electron and Hermes' desktop app
outright — Hermes' own `release/mac-arm64` is **307 MB on disk** (VERIFIED) and
its Electron shell has no tray, no panel and no hotkey, so there is nothing in
it to copy but the backend supervisor and the notarization config.

---

## 1. Environment, as actually measured

All VERIFIED on this machine on 2026-09-21:

| Thing | Value | How |
|---|---|---|
| OS | macOS **26.5.2** (25F84), arm64 | `sw_vers`, `uname -m` |
| Xcode | **not installed** — Command Line Tools only at `/Library/Developer/CommandLineTools` | `xcode-select -p`; `xcodebuild` errors |
| Swift | **6.3.3**, target `arm64-apple-macosx26.0` | `swift --version` |
| macOS SDK | **26.5** | `xcrun --show-sdk-version` |
| SwiftPM | present (`swift package`, `swift build`) | `swift package --version` |
| Node | v22.23.2 / npm 10.9.8 | `node --version` |
| Rust / cargo | **not installed** | `rustc: command not found` |
| Python | 3.12.13 in `.venv`; venv is **92 MB** (no voice extras) | `du -sh .venv` |
| Codesigning identities | **zero valid identities** | `security find-identity -v -p codesigning` |
| daa TCC state | accessibility **denied**, FDA **denied**, mic unknown (no `pyobjc-framework-AVFoundation`) | `daa doctor` |
| daa providers | mic **fake**, local STT **fake**; cloud STT / TTS / LLM / Jev **live** | `daa doctor` |

The two research passes disagreed on the current macOS release (one said 26.6.2
current with 27 due this fall; the other said 27 "Golden Gate" shipped
2026-09-14 with 26.x now at 26.7). Both are secondary. **This machine is on
26.5.2 and that is what matters**; plan for a macOS 27 retest.

### Can we build a SwiftUI menu-bar app without Xcode? Yes. VERIFIED.

I compiled and ran one:

```
swiftc -sdk $(xcrun --sdk macosx --show-sdk-path) -target arm64-apple-macos26.0 \
       -parse-as-library main.swift -o daadock        # 63,624-byte binary
swift build -c release                                # SwiftPM: "Build complete! (36.18s)"
```

`MenuBarExtra(...).menuBarExtraStyle(.window)` compiles clean against the
26.5 SDK with CLT only, and the hand-assembled `.app` bundle was **68 KB** and
ran, showing a real menu-bar item. Measured RSS while running: **77 MB**
(`ps -o rss`) — note that macOS RSS counts shared AppKit/SwiftUI framework
pages, so the app's private footprint is far smaller; treat 77 MB as a ceiling,
not a cost.

For comparison, a `rumps` menu-bar app on this machine ran at **93 MB RSS** —
*and its host binary was `/opt/homebrew/.../Python.app/Contents/MacOS/Python`*,
which is the entire TCC problem in one line (see §4).

### macOS 26 APIs that are present in this SDK. All VERIFIED by reading the `.swiftinterface`.

- `Speech.SpeechAnalyzer` (a Swift `actor`), `Speech.SpeechTranscriber`,
  **`Speech.SpeechDetector`** (VAD, with `SensitivityLevel.low/.medium/.high`),
  and `Speech.AssetInventory` (on-device model install, `status(forModules:)`,
  `assetInstallationRequest(supporting:)`).
- Liquid Glass: `SwiftUICore.glassEffect`, `GlassEffectContainer`, `Glass`,
  `glassEffectID`, `glassEffectTransition`, `glassEffectUnion`; SwiftUI's
  `GlassButtonStyle` / `GlassProminentButtonStyle`.
- Carbon `RegisterEventHotKey` still compiles against the 26.5 SDK (global
  hotkey **without** an Accessibility grant — see §4).

`SpeechDetector` + `SpeechTranscriber` is the single biggest finding in this
document. It means the Swift shell can replace **`silero-vad` + `torch` +
`sounddevice` + the `LocalTranscriber` stub** — the `voice` extra, which would
otherwise push a shipped bundle from ~100 MB to multiple GB — with framework
calls, at zero bundle cost, and put the microphone grant on the `.app`.

---

## 2. Chosen framework: native SwiftUI (`NSStatusItem` + `NSPanel`)

**What we build:** an `LSUIElement` app (no Dock icon) that owns

- an `NSStatusItem` with a template image that animates through the states,
- a borderless, `.nonactivatingPanel` `NSPanel` at `.statusBar` window level
  with `canJoinAllSpaces` + `isFloatingPanel` — this is the dock proper,
- a second, larger panel for the approval card,
- a Carbon/`CGEventTap` hotkey manager for push-to-talk,
- `SpeechDetector` + `SpeechTranscriber` for VAD + on-device transcription,
- a `PythonSupervisor` that spawns, watches and restarts the Python child.

### Why

1. **TCC is the whole argument.** A signed `.app` with a frozen
   `CFBundleIdentifier` is the only artifact whose privacy grants survive a
   rebuild. Everything else in the candidate list either *is* a bare binary
   (rumps, pywebview, Textual) or adds a second runtime the grants still have
   to be attributed through. Getting this wrong means re-granting Microphone,
   Accessibility and every Automation target every time you `make`.
2. **It is the only stack with real Liquid Glass**, verified present in this
   SDK. "A very polished and great experience" on macOS 26 means the system
   material, the system spring animations, the system focus ring and SF
   Symbols' built-in symbol effects. A webview approximates these; it never
   matches them, and the mismatch is most visible exactly where this product
   lives — a small always-on-screen widget sitting next to Control Center.
3. **It deletes the heaviest dependency in the project.** On-device Speech
   removes `torch` from the shipping story entirely.
4. **Footprint.** 68 KB bundle / 77 MB RSS ceiling, measured, versus Hermes'
   307 MB Electron release, measured.
5. **It is assembly, not invention** — see §7.
6. **Xcode is not required.** VERIFIED above. (Xcode *is* required for
   Instruments, SwiftUI previews and the `.icon` Icon Composer format; install
   it when polish starts, but it is not a blocker to day one.)

### Known costs, honestly

- **`MenuBarExtra` is still gap-ridden in Xcode 26** (VERIFIED-secondary): no
  API to read or set the popup's presentation state, reach the underlying
  `NSStatusItem`, or reach the popup's `NSWindow`; no right-click menu; and
  `openSettings` from a `MenuBarExtra` is **broken on macOS 26** (works on 15)
  unless you declare a hidden window scene *before* the Settings scene. The
  `MenuBarExtraAccess` package (MIT, v1.3.1, 2026-08-05) exists solely to patch
  these and is still shipping releases — which is itself the evidence the gaps
  persist. **Decision: do not use `MenuBarExtra` for the real panel.** Use it
  only if you want a throwaway; drive `NSStatusItem` + `NSPanel` directly, which
  is what every high-quality app in this category actually does.
- Swift is a second language in the repo. Mitigated by the fact that the Swift
  side holds **no policy**: it renders state and forwards clicks. Every rule
  stays in Python, under the 666-test suite.
- No cross-platform story. daa is a macOS assistant; this is not a cost.

---

## 3. Rejected options

| Option | Version (VERIFIED) | Verdict |
|---|---|---|
| **Electron** | 44.4.3, 2026-09-18, MIT (Chromium 152, Node 24.21) | **Reject.** Hermes' own macOS release is **307 MB** measured on this disk. ~130–160 MB idle RAM for a tray app. Electron 44 raised the floor to macOS 13 and its release notes never mention Tahoe at all. For a widget that must be running 100% of the time, this is the wrong order of magnitude, and it buys nothing here: the Tray API is fine, but the mic still has to be TCC-attributed and you now have a second runtime in the chain. |
| **Tauri v2** | CLI 2.11.5 (2026-09-20) / crate 2.11.6 (2026-09-19), MIT/Apache-2.0 | **Reject, reluctantly.** It is genuinely good: ~8.6 MB bundles, a *documented* PyInstaller sidecar path, tray + global-shortcut plugins, notarization in the bundler. Three blockers. (a) **Rust is not installed here** — a full toolchain to add. (b) `wry` **#1848**, opened **2026-09-16, still open**: on **macOS 26.0**, WKWebView "stops presenting frames (stale/partial frames) while DOM and accessibility tree stay live." A compositor that freezes while JS keeps running is disqualifying for a status indicator whose entire job is to be truthful about state. (c) tauri **#11992**, open since 2024-12-17: codesigning + notarization breaks when using `externalBin` — i.e. exactly the Python-sidecar path. Revisit if #1848 closes. **Tauri v3 exists only as `alpha.1` (2026-09-15) — do not touch.** |
| **`rumps`** | 0.4.0, **uploaded 2022-10-15**, BSD-3 | **Reject as a foundation.** It does still work — I ran a rumps menu bar app on macOS 26.5.2 (VERIFIED) — and it is the fastest possible path to a status item. But: no PyPI release in **four years** (master has 2026 commits, 82 open issues, 14 stale PRs; what you `pip install` is 2022 code), no floating-panel story, no Liquid Glass, and fatally, **its TCC subject is Homebrew's shared `Python.app`** — every Python GUI on the machine is the same TCC principal, and a `brew upgrade python` re-rolls it. Fine for a one-afternoon spike; not the face of the product. |
| **`pywebview`** | 6.2.1 (2026-04-15), BSD-3, healthy (15 open issues on 6k stars) | **Reject.** It is a *good* frameless panel — `frameless`, `on_top`, `transparent`, **`vibrancy`**, `easy_drag`, `shadow` are all real parameters on `create_window`. But **it has no tray / status-item API at all**. The documented workaround is `pystray`, and both libraries demand the main thread, so you end up running pywebview in a separate process via `multiprocessing`. Adding a process-boundary bug surface at the exact centre of an always-available app, to get a worse-looking panel, is a bad trade. |
| **Textual** | 8.2.8 (2026-06-30), MIT | **Reject for this job — keep for another.** No macOS GUI target of any kind: no tray, no panel, no window, no hotkey. Its two web bridges are dead or dying (`textual-serve` last release 2025-04-16; **`textual-web` last pushed 2024-08-30**). *But*: a Textual TUI over the same JSON stream would be an excellent developer/debug console and a real answer for headless/SSH use, where the dock cannot exist. Park it as a stage-6 nice-to-have. |
| **PyObjC-direct (hand-rolled `NSStatusItem` in Python)** | pyobjc 12.2.2 (2026-08-11), MIT, tracks the 26.5 SDK | **Reject for the UI, keep for tools.** This is the healthiest piece of the Python-on-macOS stack and `daa/tools/` should keep using it. But making Python the UI host re-creates the rumps TCC problem with more code. |

---

## 4. TCC: the part that is easy to get wrong

`src/daa/tools/permissions.py` already opens with the right sentence —
*"macOS attaches privacy grants to the host binary, not to this package"* — and
lists four different subjects (`.venv/bin/python`, `/usr/bin/python3`, a
packaged `.app`, pytest). The plan below is the answer to the question that
docstring raises.

### 4.1 The mechanism: responsible process, not "the process"

**VERIFIED** (Apple TN3179, plus Apple DTS on the forums): TCC does not blame
the process that made the call. It tracks down the **responsible code** —
"the nearest 'parent' of the process that the user knows about" — and uses it
for the alert text, for the Info.plist usage string, for the entitlement check,
and for where the decision is stored. Apple's own example is exactly ours:
*"if your app spawns a helper tool and the helper tool performs a local network
operation, macOS considers **the app** to be the responsible code."*

The algorithm is **undocumented and changes between macOS versions**
(Apple DTS, verbatim). So the rule to build on is not "here is the algorithm"
but "**do not do the things known to break it**."

Apple DTS's own matrix:

| How the code runs | Responsible process |
|---|---|
| From Terminal | **Terminal.app** ← this is today's daa |
| From Xcode | the tool itself (chain broken by `debugserver`) |
| Over SSH | the SSH server |
| **As a child of an app** | **the app** ← this is the plan |
| From `launchd` | the app *only* if installed via `SMAppService`, else needs `AssociatedBundleIdentifiers` |

Breaks attribution (VERIFIED): `responsibility_spawnattrs_setdisclaim` (private
SPI, used by LLDB/Firefox/Chromium), double-forking, `setsid`, daemonizing, and
a bare `launchd` agent. **The dock must do none of these.**

You can check your work:
```
sudo launchctl procinfo $(pgrep -n daa) | grep -i responsible
```

### 4.2 Who holds what, under the recommended architecture

| Permission | Held by | Notes |
|---|---|---|
| **Microphone** | **the Swift `.app`** | Needs `NSMicrophoneUsageDescription` **and** the `com.apple.security.device.audio-input` entitlement. Apple's AVFoundation doc: without both, *"the system terminates your app."* Under the plan the Python side never opens an audio device at all — Swift does VAD + STT and sends text. |
| **Speech recognition** | **the Swift `.app`** | `NSSpeechRecognitionUsageDescription` for `SpeechAnalyzer`. On-device; `AssetInventory` may need a first-run model download. |
| **Accessibility** | **the Swift `.app`**, inherited by the Python child | For `focus_window`. No entitlement exists — pure TCC toggle, user-set, polled via `AXIsProcessTrusted()`. The Python child calling `AXUIElementSetAttributeValue` resolves against the app. |
| **Automation / Apple Events** | **the Swift `.app`**, per target app | `NSAppleEventsUsageDescription` + `com.apple.security.automation.apple-events`. Under hardened runtime `tccd` hard-requires the entitlement; the log line is literally `... requires entitlement com.apple.security.automation.apple-events but it is missing`. Keyed **per (client, target)** pair via `indirect_object_identifier`, so "may control Finder" and "may control Safari" are two separate prompts. |
| **Screen Recording** | **ideally nobody — design it out** | See below. |
| **Full Disk Access** | **nobody** | `daa doctor` probes it only as a readability proxy; don't request it. |

### 4.3 Design Screen Recording out of the product

`CGWindowListCopyWindowInfo` **never prompts**; without the Screen Recording
grant it simply omits `kCGWindowName`, which is why `tools/windows.py` degrades
the way it does. The cost of *keeping* that dependency is high: macOS 15
introduced periodic re-consent for Screen Recording (weekly in the betas,
changed to **monthly** after developer backlash), it persists in 26, there is
**no off switch**, and the escape-hatch entitlement
`com.apple.developer.persistent-content-capture` is, per Apple, "intended for
Virtual Network Computing (VNC) apps." A monthly system modal is a fatal
papercut in a product whose value is being unobtrusive.

**Proposal (INFERRED — mechanism is sound, not yet tested here):** get window
titles from `AXUIElementCopyAttributeValue(window, kAXTitleAttribute)` under the
**Accessibility** grant you already need for `focus_window`, and drop Screen
Recording entirely. **This is test #1 in stage 0** — it is cheap to check and it
changes the permission story materially. Accessibility is currently `denied` on
this machine, so it has not been run.

### 4.4 Signing: the finding that decides local development

**VERIFIED by experiment on this machine.** I ad-hoc signed a test bundle with
hardened runtime, read its designated requirement, changed one string in the
source, rebuilt, re-signed, and read it again:

```
before:  # designated => cdhash H"f0b85fa55d2f9f21975bc5bfe869bd8666916870"
after:   # designated => cdhash H"dcf6284398200938e937c93cbf1655da310229a1"
```

An ad-hoc signature's designated requirement **is the code-directory hash**.
TCC keys grants on the designated requirement. Therefore:

> **Every rebuild of an ad-hoc-signed app is a brand-new app to TCC, and every
> permission must be granted again.**

Apple DTS says this in so many words: *"Ad hoc signed code does not include a
stable DR, and thus macOS is unable to tell that version N+1 of your app is the
'same code' as version N"* — and TN3127 notes it "leads to repeated
authorization prompts for resources like the microphone."

A certificate-based DR instead pins `identifier` + `anchor apple generic` +
`subject.OU` (Team ID) — **none of which change when you rebuild**.

| Signing | Grants survive a rebuild? | Needs $99/yr? |
|---|---|---|
| Unsigned | No (undefined behaviour) | — |
| **Ad-hoc (`codesign -s -`)** — *what this machine can do today* | **No** | No |
| Self-signed cert in the login keychain | Probably yes locally (stable leaf ⇒ stable DR) — **INFERRED**, not Apple-anchored, cannot notarize | No |
| **Apple Development** | **Yes** — Apple's recommendation for daily work | Yes |
| **Developer ID Application** | **Yes** — required to distribute | Yes |

**There are zero codesigning identities on this machine (VERIFIED).** So:

> **Action item, and the earliest one: join the Apple Developer Program and
> sign local builds with an Apple Development certificate before granting the
> dock a single permission.** Every grant made before that is thrown away on
> the next build. This is the "getting this wrong means re-granting everything
> later" failure the brief warned about, and it is avoided by one purchase and
> one `codesign -s` flag, not by any code.

Note: the Apple Development DR and the Developer ID DR are **not** mutually
compatible, so grants do not carry from a dev build to a release build. That is
fine and expected; just don't be surprised by it.

### 4.5 Bundle shape

```
daa.app/
  Contents/
    Info.plist                       CFBundleIdentifier=ai.daa.dock  (NEVER changes)
                                     LSUIElement=true
                                     NSMicrophoneUsageDescription
                                     NSSpeechRecognitionUsageDescription
                                     NSAppleEventsUsageDescription
    MacOS/daa                        the Swift shell — the TCC principal
    Resources/
      python/                        onedir CPython + site-packages (stage 4+)
        bin/python3
        lib/python3.12/...
    _CodeSignature/
```

`daa.entitlements` — **no XML comments** (comments have been observed to make
`codesign` silently drop entitlements) — and verify after every build with
`codesign -d --entitlements - daa.app`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.security.device.audio-input</key><true/>
    <key>com.apple.security.automation.apple-events</key><true/>
    <key>com.apple.security.cs.disable-library-validation</key><true/>
</dict>
</plist>
```

There is **no entitlement for Accessibility and none for Screen Recording** —
both are pure TCC toggles.

`disable-library-validation` is needed only once CPython is embedded, because
library validation otherwise restricts the process to code signed by Apple or by
*your* Team ID, and pip wheels are neither. The strictly better alternative is
to **re-sign every `.so` and `.dylib` in `site-packages` with your Developer
ID** — which notarization requires anyway — and keep library validation on.
Start strict, add entitlements only when something actually fails.

Add `com.apple.security.cs.allow-unsigned-executable-memory` only if a native
wheel demands it. Avoid `allow-dyld-environment-variables` by using
`@executable_path`-relative install names instead of `DYLD_*`.

### 4.6 Signing the Python payload

- **Sign inside-out with a script. Never `--deep`.** Apple DTS's "`--deep`
  Considered Harmful" gives two reasons and both bite here: it applies one set
  of entitlements to *everything* (your app and the interpreter need different
  ones), and it only finds code in *nested code sites* — `.so` files sitting in
  `Resources/python/lib/.../site-packages/` are in a **data** location, so
  `--deep` silently skips them and notarization rejects the bundle.
- **Never `pip install` into a signed bundle at runtime** — any write breaks the
  seal.
- **Never PyInstaller `--onefile`**: it unpacks to a fresh random
  `/var/folders/.../_MEIxxxxx` on every launch, which is the worst possible
  shape for both library validation and TCC identity. `--onedir` or nothing.

### 4.7 Notarization

**Required** for a Developer-ID-signed app that someone downloads (the
quarantine bit). **Not required** for anything you build and run locally, and
**notarization has no effect on TCC whatsoever** — stable grants come from the
*certificate*, not from the ticket. Requirements: Developer ID Application cert,
hardened runtime on the app *and every nested Mach-O*, `--timestamp`,
well-formed entitlements, then `xcrun notarytool submit --wait` →
`xcrun stapler staple`. `altool` is retired.

So the staging is clean: **stage 1–3 need no notarization at all**; they need a
$99 certificate for grant stability and nothing else.

### 4.8 Resets, and a macOS 26 bug to know about

```
tccutil reset Microphone    ai.daa.dock
tccutil reset Accessibility ai.daa.dock
tccutil reset AppleEvents   ai.daa.dock
```
`tccutil` can only *reset*, never grant. The system TCC database is
SIP-protected.

**VERIFIED, and it is an argument for the whole plan:** on macOS **26.0–26.2**,
plain non-bundled executables **cannot be added to Accessibility / Full Disk
Access / Screen & System Audio Recording in System Settings at all** — the file
picker accepts the selection and nothing appears, with no error. Apple DTS:
*"IMO this is a bug and I encourage you to file it as such."* Reportedly fixed
in 26.3 beta. This machine is on 26.5.2 so it should be past it, but it is a
clear statement that **non-bundled binaries are second-class in macOS 26's
privacy UI** — which is where `.venv/bin/python` lives today.

Also: test first-run permission behaviour in **revertible VM snapshots**. It is
the only way to see the true clean state, and it is Apple DTS's explicit advice
for 26.x.

---

## 5. Process and IPC architecture

### 5.1 Shape: UI-parent, Python-child, stdio

**The Swift `.app` spawns Python as a plain, non-disclaimed child and speaks
newline-delimited JSON over its stdin/stdout.**

Assessment of the alternatives:

| Option | Verdict |
|---|---|
| **UI spawns a Python subprocess per utterance** | **No.** Cold import of daa's subsystems is only 0.05 s (VERIFIED — the lazy-import discipline in `build_loop` pays off), so startup is not the problem. The problem is that the loop is *stateful*: `Transcript` (12 turns), `self._pending` mid-utterance buffering, `_scripted_replies`, the barge-in listener and the `_segments` iterator that `_next_reply()` pulls confirmation answers from all die with the process. A per-utterance process cannot answer "yes" to its own question. |
| **Python daemon + thin UI** | **Yes — this, with the UI as the parent.** The distinction that matters is not daemon-vs-not, it is *who is the parent*. A `launchd`-managed daemon breaks TCC attribution unless you add `SMAppService` or `AssociatedBundleIdentifiers`; an ordinary child of the `.app` inherits responsibility for free. |
| **Local HTTP / WebSocket** | **No.** It is what Hermes does (`tui_gateway/ws.py` over FastAPI) and it is right *for Hermes*, which serves iOS and web clients too. For a single local UI it adds a listening socket, a port, an auth token, a macOS Local Network privacy prompt on 26, and a whole class of "another process connected to my assistant" bugs — for zero benefit. |
| **Unix domain socket in `~/.daa/`** | **Good fallback, not the default.** `~/.daa` is already `0700` with `0600` files (`safety/store.py`), so the permission story is sound, and it survives the UI restarting while Python keeps running. But it needs discovery, stale-socket cleanup, liveness checks and an orphan-reaping story. Adopt it in stage 6 *only* if a long-running background task genuinely needs to outlive the dock. |
| **stdio JSON-RPC** | **Chosen.** No port, no socket file, no auth, no discovery, no Local Network prompt. The child's lifetime is bound to the parent by the OS. It is the same transport Hermes' gateway supports (`tui_gateway/transport.py` → `StdioTransport`) and the same one **AudioWhisper** (MIT) already uses for a Swift↔Python daemon. |

**Surviving restarts.** The brief asks that the UI survive Python restarting.
It does, because the UI is the supervisor:

- `PythonSupervisor` watches the child; on exit it moves the dock to a visible
  **`degraded`** state ("daa's brain stopped — restarting"), backs off
  exponentially (1s, 2s, 4s, capped at 30s), and respawns.
- **Any in-flight approval is cancelled and refused** on child death. This is
  the fail-closed rule from the README applied to the new boundary.
- Conversation state is intentionally *not* restored. `Transcript` is capped at
  12 turns and 2000 chars by design ("a voice conversation has a short
  horizon"); pretending otherwise across a crash would be a lie about what the
  model can see.
- The *audit* and *undo* journals are files and are untouched by any of this,
  so `undo` still works across a restart — and still refuses to run below
  `CONFIRM_VOICE`, because the instruction came from a file.

### 5.2 Threading on the Python side

Python stays **fully synchronous**. The bridge adds exactly three threads:

1. **reader** — owns `sys.stdin.buffer`, parses one JSON object per line,
   dispatches into queues. Never runs handler code.
2. **turn** — runs `VoiceLoop.run()` exactly as `daa listen` does today, pulling
   from a `BridgeMic`. Blocking, single-threaded, unchanged semantics.
3. **writer** — a lock-guarded `sys.stdout.buffer` with a bounded queue.

**`sys.stdout` must be stolen at startup.** The protocol owns fd 1; every
`print()` in the tree must be redirected to stderr, which the Swift side tees
to `~/Library/Logs/daa/python.log`. Hermes does exactly this (`_real_stdout` +
`_stdout_lock` in `tui_gateway/transport.py`). Launch with `-u` or
`PYTHONUNBUFFERED=1`: Python's stdout is *fully buffered* on a pipe, and
forgetting this is the classic "the UI hangs waiting for ready" bug — Hermes
has a comment warning about precisely it.

### 5.3 The mic seam is already the right shape

`daa/voice/mic.py` documents `AudioChunk.pcm` as:

> *"The fakes put UTF-8 text in here instead, which is not a hack so much as the
> point: the loop must never be able to read the mic without going through a
> Transcriber, and the only way to guarantee that is for the mic's payload to be
> opaque bytes."*

So the bridge needs two tiny classes and **no change to the loop**:

- **`BridgeMic(AudioSource)`** — `segments()` yields `AudioChunk`s off a queue
  fed by the reader thread, with `pcm = text.encode()`, `started_at` from
  Swift's monotonic clock, and `complete` set from Swift's VAD (silence-cut vs
  timeout-cut), which is what feeds the gate's honest `end_of_turn`.
  `set_speech_listener` forwards Swift's speech-onset notification, so
  **barge-in keeps working**.
- **`BridgeTranscriber(Transcriber)`** — decodes the chunk and returns
  `Transcription(text=..., source="local", confidence=..., partial=not complete)`.

Because `run()` is untouched, `self._segments` still exists, so
**`_next_reply()` still works** and spoken "yes"/"no" answers to `CONFIRM_VOICE`
arrive through the same path as any other utterance. That is the detail that
makes stdio + `BridgeMic` clearly better than calling `handle_text()`.

Push-to-talk vs always-on maps onto the existing gate exactly:

- **Push-to-talk** — holding the hotkey *is* an unambiguous act of address, the
  same argument `handle_text`'s docstring makes for typed text. Swift sets
  `addressed: true` on the utterance and the bridge skips the gate. Saves a Jev
  round trip at the most latency-sensitive moment.
- **Always-on** — every utterance goes through `AddressGate`. Unchanged.

### 5.4 Wire protocol

**Framing:** one UTF-8 JSON object per line, `\n`-terminated, no embedded raw
newlines, both directions. No Content-Length headers. Unparseable line → log to
stderr and skip; never crash the reader.

**Envelope.** Three message kinds, JSON-RPC-shaped but deliberately not the full
spec:

```json
{"t":"req","id":"r7","m":"method.name","p":{}}     // expects exactly one res
{"t":"res","id":"r7","ok":true,"p":{}}             // or "ok":false,"err":{"code","message"}
{"t":"ev","m":"event.name","p":{}}                 // fire and forget
```

Ids are opaque strings, unique per sender. Requests flow **both** ways.

#### Swift → Python

| Method | Kind | Payload |
|---|---|---|
| `session.hello` | req | `{proto:1, app:"0.1.0", caps:["stt.local","tts","hotkey"]}` → res `{proto:1, daa:"0.1.0"}` |
| `mic.utterance` | ev | `{text, confidence, startedAt, complete, addressed}` — `addressed:true` for push-to-talk |
| `mic.onset` | ev | `{}` — speech started; drives barge-in, must be cheap |
| `confirm.result` | res | response to `confirm.request` (below) |
| `control.alwaysOn` | req | `{on:bool}` |
| `control.cancel` | ev | `{}` — user hit Esc; abandon the current turn |
| `undo.last` | req | `{}` → res `{ok, summary}` |
| `doctor` | req | `{}` → res: the full `daa doctor` payload, structured |
| `session.shutdown` | req | `{}` → res, then the child exits |

#### Python → Swift

| Method | Kind | Payload |
|---|---|---|
| `ready` | ev | `{daa, dryRun, alwaysOn, jevLive, providers:{mic,stt,tts,llm,jev}, tools:[{name,floor}], missing:[...]}` — the dock's whole boot state in one frame |
| `state` | ev | `{phase, detail?, since}` — `phase ∈ idle \| listening \| thinking \| speaking \| awaiting \| working \| degraded` |
| `audit` | ev | `{kind, id, at, payload}` — a verbatim redacted `AuditEvent` |
| `speak` | ev | `{text}` — mirrors what TTS is saying, for the transcript |
| **`confirm.request`** | **req** | the approval card — see below |
| `confirm.cancel` | ev | `{id, reason}` — withdraw a pending card (timeout, child shutting down) |

#### `audit`: the event stream already exists

`VoiceLoop.audit` is an injected callable taking one `AuditEvent`. It is already
**structured** (`kind`, `id`, `at`, `payload`), already **redacted** (by key, by
scope, and by shape — Luhn-checked card numbers, `sk-`/`ghp_`/`AKIA`, PEM
headers), and already carries `synthetic`. The event kinds it emits today —
`heard`, `dropped`, `buffered`, `woke`, `rescored`, `router_miss`, `judgment`,
`disposition`, `confirm`, `confirmation`, `visual_confirm`, `abandoned`,
`refused`, `dry_run`, `execution`, `undo`, `undo_rejected`, `undo_retained`,
`spoke`, `barge_in`, `error` — are, read in order, *precisely* the story the
dock needs to tell.

> **So the read path needs no new instrumentation at all.** Wrap the existing
> audit sink in a tee: one branch to `~/.daa/audit.jsonl` as today, one to the
> writer thread. `daa listen` without a dock is unchanged.

Rules on the tee, all of them because "an audit sink that throws must never take
the loop down with it":
- bounded queue (2048); on overflow **drop the chatty display-only kinds first**
  (`heard`, `spoke`, `barge_in`), never `disposition` / `confirmation` /
  `execution` / `undo` / `refused`;
- never block the loop thread;
- swallow every exception.

#### `confirm.request`: the payload that matters

```json
{"t":"req","id":"cf_3b91c0ad","m":"confirm.request","p":{
  "tool":"run_applescript",
  "tier":"CONFIRM_VISUAL",
  "reason":"unrecoverable and not explicitly requested",
  "phrase":"run a script that sends a message to Alex",
  "verb":"run",
  "explicit":false,
  "dryRun":true,
  "targets":["a script of 14 lines"],
  "args":[
    {"key":"script","isProgram":true,
     "value":"tell application \"Messages\"\n  send \"on my way\" to buddy \"Alex\"\nend tell"}
  ],
  "consequences":{"send":"sending this text: 'on my way'"},
  "assessment":{"blastRadius":2.4,"unrecoverable":0.81,"explicitlyRequested":0.19,
                "targetConfidence":"probable","confidence":0.77,"synthetic":false},
  "expiresInMs":90000
}}
```

Response: `{"t":"res","id":"cf_3b91c0ad","ok":true,"p":{"granted":true}}`.

**Security properties of this channel, and why it is stronger than the terminal
it replaces:**

- `id` is a fresh random token minted by Python per card, **single-use**. A
  `confirm.result` with an unknown, already-consumed or stale id is **dropped
  and audited as an error**. The model cannot mint one; a forged `undo.jsonl`
  row cannot mint one.
- The only writer to the child's stdin is the parent `.app`, holding a pipe fd —
  a strictly narrower channel than a tty, which any process sharing the terminal
  session could theoretically drive.
- **`_VISUAL_OK` stays exactly where it is.** `_confirm_visual` remains the only
  function in the codebase that can produce it. The dock returns a boolean; it
  never sees or names the token.
- **`expiresInMs` is a new, strictly-safer behaviour.** `console.ask()` on a
  terminal blocks forever. The dock times out at 90 s and **fails closed**:
  card dismissed, `granted:false`, `_emit("visual_confirm", granted=False)`,
  `_speak("Okay, leaving it.")`. An approval you walked away from is not an
  approval.
- Child death, UI death, or `confirm.cancel` all resolve to `granted:false`.

### 5.5 The one change to `src/`

Everything above is additive — new files under `src/daa/ui/`, a new
`daa bridge` subcommand — **except** one ~8-line edit inside `_confirm_visual`,
which exists so the card receives structured data instead of a pre-rendered
68-column text blob. Proposed, not applied:

```python
        typed = ""
        try:
            present = getattr(console, "present", None)
            if present is not None:
                # A console that can render the action itself gets the objects.
                # Same contract: it returns True only for a deliberate approval.
                typed = "yes" if present(action, disposition) else ""
            else:
                console.write(_visual_detail(action, disposition))
                typed = console.ask("type yes to approve, anything else to cancel: ")
        except Exception as exc:  # noqa: BLE001 -- isolation boundary, see comment
            self._emit("error", where="console", error=str(exc))
            typed = ""
```

Nothing else moves: the `== "yes"` comparison, the `visual_confirm` emit, the
`_confirmation_logged` call, the `_NO_SCREEN` branch and the `_VISUAL_OK`
gating are all untouched. `TerminalConsole` has no `present`, so every existing
test in `tests/test_voice_loop.py` (`FakeConsole`, the `"y"`-is-not-`"yes"`
test, the EOF test, the `Exploding` console test) passes unchanged. New tests
to add alongside: `present` returning `False` refuses; `present` raising is a
refusal; a console with `present` is **never** asked to `write`/`ask`.

**If you would rather touch nothing at all**, stage 1 can ship a `DockConsole`
that implements only `available`/`write`/`ask` and renders `_visual_detail`'s
text in a monospaced block. It works, it is honest, and it is ugly — which is
the wrong answer for the most safety-critical screen in the product. Do it as a
half-day spike to prove the transport, then take the 8 lines.

---

## 6. The UI, screen by screen

### 6.1 Principles

1. **State is legible from the menu bar alone.** The panel is for detail; the
   22×22 template image must answer "is it listening? is it doing something? does
   it need me?" from across the room.
2. **Nothing is dismissible by reflex.** The approval card is the one surface
   where the design deliberately fights muscle memory.
3. **The privacy boundary is visible.** *"Speech is not written down until it is
   addressed to you."* In always-on mode the dock shows a waveform, **never live
   text**, until the `woke` event arrives. Under push-to-talk the user has
   already addressed daa, so live partials are fine and should be shown — the
   difference is itself a piece of UI that teaches the model.
4. **Dry-run is never subtle.** It changes what the product *is*.

### 6.2 States

| State | Menu bar | Panel | Sound/haptic |
|---|---|---|---|
| **idle** | static `waveform` glyph, 55% opacity | collapsed, or hidden | — |
| **listening** | live 3-bar level meter driven by Swift's RMS, full opacity | waveform; live partial text **only if push-to-talk**; "listening…" otherwise | soft tick on hotkey down |
| **thinking** | slow indeterminate pulse (SF Symbol `.variableColor` effect) | "thinking…" + the activated tool chips from `router_miss`/`disposition` events | — |
| **speaking** | gentle bounce synced to TTS; **tap to interrupt** (barge-in, already implemented) | the spoken line, streaming | — |
| **awaiting** | **amber dot badge**, the only colour the icon ever takes; plus a Dock-bounce-equivalent (`NSApp.requestUserAttention(.criticalRequest)`) | **approval card**, focused, front | one firm system alert |
| **working** (§6.7) | small rotating arc | task rows with progress | — |
| **degraded** | glyph with a hairline slash | "daa's brain stopped — restarting (2s)" + a `Show log` button | — |

The amber badge must be the **only** non-monochrome state. If the icon is ever
coloured, something needs a human. That single rule is worth more than any
amount of animation.

### 6.3 The dock panel

~320 pt wide, height driven by content, `glassEffect` material, `.statusBar`
level, non-activating (so the frontmost app never loses focus — it is the app
daa is about to act on), anchored under the status item, `canJoinAllSpaces`.

```
┌──────────────────────────────────────┐
│ ●  daa            ⌥Space   DRY RUN   │  ← header: state dot, hotkey hint, mode pill
├──────────────────────────────────────┤
│  ▁▂▅▇▅▂▁   listening…                │  ← live zone
├──────────────────────────────────────┤
│  you   move those screenshots to…    │  ← transcript, newest at bottom
│  daa   Moved 3 files to Archive.  ↩︎  │  ← ↩︎ = undo affordance, present only
│  you   thanks                        │     when this turn produced an undo entry
│  daa   Sure.                         │
├──────────────────────────────────────┤
│  ⏻ Always on    ⚙︎    ⓘ               │
└──────────────────────────────────────┘
```

- **DRY RUN pill** — persistent, high-contrast, in the header, whenever
  `settings.dry_run` is true. It also changes the transcript's verb tense:
  dry-run results already read *"Dry run: I would move 3 files…"* and the dock
  should render those in a distinct, quieter style so a dry run can never be
  mistaken for a real one. Turning it off should require a Settings toggle with
  a one-line confirmation, not a menu item — the repo's own regression test
  `test_environment_cannot_silently_make_a_loop_live` exists because this
  exact flag once did the wrong thing.
- **Undo** — show `↩︎` on a turn only when the `execution` audit event carried an
  `undo_id`. Clicking sends `undo.last`. The dock must render the result
  faithfully, including the refusal path: undo never runs below
  `CONFIRM_VOICE`, so clicking `↩︎` produces a **spoken** confirmation, not a
  silent reversal. Show that as a distinct "confirming…" state so the click
  doesn't look broken.
- **Always on** — toggles the address gate. When it goes on, show a one-time
  explainer stating plainly that the mic is open and that utterances judged not
  addressed to daa are dropped without being written down. That sentence is
  true (it is enforced in `handle_chunk`) and saying it is the difference
  between a feature and a creep.
- **Transcript** — mirrors the in-memory `Transcript` (12 turns / 2000 chars).
  Deeper history comes from `~/.daa/audit.jsonl` in a separate History window,
  opened on demand, never streamed into the dock.

### 6.4 The approval card — the most important screen

This is what `CONFIRM_VISUAL` exists for. `_visual_detail`'s docstring sets the
bar: *"The whole action, in full, on screen. Nothing elided and nothing
summarised… if a script is about to run, the script is here, not a description
of it."*

Separate panel, ~520 pt wide, **activating** (unlike the dock), centred on the
active screen, `.modalPanel` level, with the dock panel dimmed behind it.

```
┌────────────────────────────────────────────────────────────────┐
│  ⚠︎  daa needs your approval                                    │
│      This one can't be approved by voice.                      │
├────────────────────────────────────────────────────────────────┤
│  I would  run a script that sends a message to Alex            │  ← _phrase(), verb-first, large
│                                                                │
│  ⚑ I inferred this — you didn't ask for it                     │  ← only when explicit == false
│  ⚑ sending this text: 'on my way'                              │  ← every consequences entry
├────────────────────────────────────────────────────────────────┤
│  THIS RUNS                                                     │  ← script args first, always
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ tell application "Messages"                              │  │  monospaced, full text,
│  │   send "on my way" to buddy "Alex"                       │  │  syntax-tinted, scrollable,
│  │ end tell                                                 │  │  NEVER truncated
│  └──────────────────────────────────────────────────────────┘  │
│                                                                │
│  TARGETS (1)                                                   │
│    a script of 14 lines                                        │
│                                                                │
│  ▸ Why daa is asking                                           │  ← collapsed by default
│      tool         run_applescript                              │
│      tier         CONFIRM_VISUAL  (floor: CONFIRM_VISUAL)      │
│      reason       unrecoverable and not explicitly requested   │
│      blast radius ▓▓▓▓▓▓▓░░  2.4 / 3                           │
│      unrecoverable 0.81      confidence 0.77                   │
│      targets       probable                                    │
├────────────────────────────────────────────────────────────────┤
│                              [ Cancel ]   [ Approve · hold ]   │
│  Auto-cancels in 1:28                                          │
└────────────────────────────────────────────────────────────────┘
```

Design rules, each with a reason:

- **Verb first, largest type on the card.** Straight from
  `ResolvedAction.describe()`. The README's own example — *"delete report.pdf"
  and "reveal report.pdf" read back identically"* — is the bug this prevents;
  the card must not undo it by leading with the filename.
- **Script args go first and are labelled `THIS RUNS`.** `_SCRIPT_KEYS` already
  encodes which arguments are programs rather than references to them. Never
  truncate, never scroll-lock, never put it behind a disclosure triangle. If it
  is 400 lines, the card grows and scrolls.
- **Every argument is shown.** Non-script args follow, sorted, in full. "Nothing
  elided and nothing summarised."
- **Consequences are ⚑-flagged at the top, in prose.** These are the fields the
  type system makes hard to drop precisely because omitting them makes the
  confirmation a lie.
- **"I inferred this"** when `explicit == false`. The most dangerous actions are
  the ones nobody asked for.
- **The judgment is collapsed but present.** The user should be able to see
  *why* Jev escalated, and the calibrated numbers are honest enough to show. If
  `assessment.synthetic` is true (FakeJev / fail-closed), say so in plain words:
  *"daa could not get a real judgment, so it is assuming the worst."* That is
  the current state of this repo with no key, and hiding it would be dishonest.
- **Approve is a press-and-hold (~600 ms) with a fill animation.** This is the
  replacement for `"yes"`-not-`"y"` — *"the friction is the feature."* A hold
  cannot be produced by a stray Return or a double-click landing on a card that
  appeared mid-click.
- **No default button. No keyboard shortcut on Approve. Esc = Cancel.** The
  card must never be dismissible by reflex, and Return must never approve
  anything.
- **The card is inert for its first 400 ms** (buttons disabled, faded in). Kills
  the click-through case where a card appears under a cursor already descending.
- **Approve is disabled while the script area has unseen content below the
  fold** — you cannot approve what you have not scrolled past. Strong, and
  correct for this tier.
- **Countdown is visible.** Silence is a refusal, and the user should know that.
- **Dry-run banner when `dryRun` is true**: *"Dry run — approving this will not
  actually run it."* Otherwise the card teaches people to approve reflexively
  during testing and they carry the habit into production.

### 6.5 Push-to-talk and always-on

- **Default hotkey: hold `⌥Space`.** Hold-to-talk, release to send. Register via
  Carbon `RegisterEventHotKey` (VERIFIED to still compile against the 26.5 SDK)
  — **it needs no Accessibility grant**, unlike an `NSEvent` global monitor or a
  `CGEventTap`. That matters: push-to-talk must work on first launch, before the
  user has been asked for anything but the microphone.
- **Bare-modifier PTT** (hold right-`⌘`, or double-tap-and-hold `fn`) is the
  nicer gesture and is what yap's `ModifierHotkeyMonitor` and MiniWhisper's
  `FnStateMachine` implement. It **does** need an event tap, hence Accessibility.
  Offer it as an opt-in in Settings, degrade to the Carbon hotkey when
  Accessibility is absent, and say why.
- **Tap vs hold.** Tap < 300 ms → latch (toggle-to-talk, tap again to send).
  Hold ≥ 300 ms → push-to-talk. Lift Hex's `docs/hotkey-semantics.md` state
  machine wholesale; it already handles the 0.3 s guard, Esc-cancel and silent
  discard.
- **Esc always cancels** the in-flight turn: `control.cancel`, drop the
  utterance, return to idle, speak nothing.
- **Always-on** is a toggle in the dock footer and mirrors `settings.always_on`.
  In always-on mode the mic level meter runs continuously and **the menu bar
  icon is visibly different at idle** — a hot mic must never look like a cold one.

### 6.6 History, undo, dry-run — the second window

A normal, resizable window (⌘-clickable from the dock's ⓘ), backed by
`~/.daa/audit.jsonl`:

- one row per turn, expandable into the full decision path (gate → router →
  judgment → disposition → confirmation → execution), which is what the audit
  log already records in order;
- filter by tier, by tool, by `dry_run`, by `granted`;
- the undo journal as its own tab, showing each entry's `produced_by` and
  whether it is still reversible;
- `synthetic` judgments badged, so "this decision came from FakeJev" is visible.

This window is also the honest home for the `daa doctor` output — providers
live/fake, TCC grants, missing modules — surfaced as a **"Set-up"** tab with a
row per permission, its live status, and a button that opens the exact System
Settings pane (`permissions.py` already carries `SETTINGS_URL` and a
human-readable `REMEDY` string for each). Route the dock's own permission
prompts through the same copy.

### 6.7 Long-running / background tasks (room left)

Reserve it now, build it later:

- `state.phase = "working"` plus a `tasks` array in the `state` event:
  `{id, title, progress?, cancellable, startedAt, tier}`.
- The dock grows a **task strip** above the footer: one row per task, title,
  determinate or indeterminate progress, a cancel affordance.
- The menu bar shows a rotating arc; if **any** task is `awaiting`, amber wins —
  approval always outranks progress.
- A background task must be able to raise an approval card *while the dock is
  closed*. That already works: the card is a separate activating panel and
  `requestUserAttention` fires regardless.
- The protocol addition is two events — `task.update` and `task.done` — and
  nothing about the transport or the approval path changes. That is the point
  of leaving room.

---

## 7. Prior art: take, adapt, reject

### Hermes (`~/.hermes/hermes-agent`, MIT)

| | |
|---|---|
| **Take** | The **backend-supervisor discipline** in `apps/desktop/electron/` — `backend-child.ts`, `backend-health.ts`, `backend-ready.ts`, `backend-start-failure.ts`, `crash-forensics.ts`, `backend-probes.ts`. Someone has already thought through "the Python died, now what," and each concern is a separately testable module. Port the *shape*, not the TypeScript. |
| **Take** | `tui_gateway/transport.py`'s transport abstraction, and specifically its two hard-won comments: peer-gone errnos (`EPIPE`/`ECONNRESET`/`EBADF`/`ESHUTDOWN`) are a clean disconnect and everything else re-raises; and **Python stdout is fully buffered on a pipe**, so `-u`/`PYTHONUNBUFFERED=1` is mandatory. Both are bugs you would otherwise find at 2 a.m. |
| **Take** | The `set_emitter` pattern in `tools/desktop_ui.py`: a module-level optional sink, `available()` returning False when unset, and tools that degrade to "desktop only" rather than failing. daa's `console` seam is already this shape; the parallel is a good sanity check. |
| **Adapt** | The `electron-builder` `mac` block — `hardenedRuntime: true`, `entitlements` + `entitlementsInherit`, `afterSign: scripts/notarize.mjs`, `gatekeeperAssess: false`, `extendInfo` carrying `NSMicrophoneUsageDescription`. It is a working, shipped configuration. Note its entitlements grant `audio-input`, `camera`, `allow-jit`, `allow-unsigned-executable-memory`, `disable-library-validation` — and **not** `com.apple.security.automation.apple-events`. daa needs that one; Hermes doesn't. |
| **Adapt** | The `ws.py` **coalescing** idea: buffer high-frequency display-only frames and flush on a short timer rather than waking the peer per token. Apply it to `heard` / mic-level events, not to `disposition` / `confirmation`. |
| **Reject** | **The entire UI.** `apps/desktop/` has **no tray, no menu-bar item, no floating panel and no global hotkey** — I grepped `electron/` for all four and found only `titlebar-overlay-width.ts`. It is a full-window IDE-style chat app with xterm, CodeMirror, Mermaid, KaTeX, react-arborist and `@assistant-ui/react`. There is nothing dock-shaped in it. |
| **Reject** | **Electron itself**, for this product. `release/mac-arm64` is **307 MB** measured. Hermes also pins Electron **40.10.2** while 44.4.3 is current — four majors of drift is the maintenance tax that comes with the stack. |
| **Reject** | The scale. `tui_gateway/` alone is **25,369 lines** of Python (`server.py` is 13,905). daa's bridge should be ~400 lines. Copy the lessons, not the layers. |

### The open-source menu-bar voice apps (all licences read from the repo's LICENSE blob)

| Repo | Licence | Take / adapt / reject |
|---|---|---|
| **[FrigadeHQ/yap](https://github.com/FrigadeHQ/yap)** — MIT, Swift/SwiftUI, ~40 files, v0.1.12 (2026-09-15) | MIT | **Take — the skeleton.** `HUDPanel.swift` is a textbook floating panel (`.borderless`, `.nonactivatingPanel`, `isFloatingPanel`, `canJoinAllSpaces`, repositions to the focused app's screen); plus `MenuBarIcon`, `HotkeyManager`, `ModifierHotkeyMonitor`, `EscapeMonitor`, `FocusedWindow`, `SecureInput`. Small enough to read in an afternoon. **This is the starting point.** |
| **[kitlangton/Hex](https://github.com/kitlangton/Hex)** — MIT, Swift + TCA, v0.8.5 | MIT | **Take — the push-to-talk semantics.** `docs/hotkey-semantics.md` documents a real PTT state machine (modifier-hold with a 0.3 s guard vs. regular hotkey, Esc cancel, silent discard) and `InvisibleWindow.swift` is a full-screen borderless nonactivating panel at `.statusBar` level. Repo now self-labels "Legacy Swift" (upstream moved to a Rust/GPUI rewrite), so lift the design and the file, don't take a dependency. |
| **[mazdak/AudioWhisper](https://github.com/mazdak/AudioWhisper)** — MIT, Swift SPM | MIT | **Take — the Python link.** It already ships `Sources/ml_daemon.py` + `Sources/ml/rpc.py`: a **JSON-RPC-over-stdin/stdout daemon**, plus `PythonDetector.swift` which hunts `.venv`/pyenv/Homebrew/`which` for an interpreter — exactly what stage 1 needs, where the dock must find `/Users/sanjay/daa/.venv/bin/python` before anything is bundled. Least active of the three; treat as reference code. |
| **[watzon/pindrop](https://github.com/watzon/pindrop)** — MIT, Swift/SwiftUI, v1.22.5 | MIT | **Adapt.** The most feature-complete MIT Swift app in the category: menu-bar-only, documented global hotkeys including push-to-talk and a dedicated cancel shortcut, overlay sink, Sparkle updates, settings. Read its settings and onboarding; don't fork the whole thing. |
| **[tornikegomareli/Talkify](https://github.com/tornikegomareli/Talkify)** — MIT, Swift + Metal | MIT | **Adapt — the polish reference.** `CoreHUD/HUDPanel.swift`: one shared panel, `HUDStage` owning the shape, a Metal compute renderer with an ADR. If "polished" means anything visually, read this first. |
| **[Muesli-HQ/muesli](https://github.com/Muesli-HQ/muesli)** — MIT, Swift | MIT | **Adapt.** `FloatingMeetingTranscriptPanel.swift`, `HotkeyMonitor`, and notably `PushToTalkEnablementPolicy.swift`. Good second opinion on yap. |
| **[andyhtran/MiniWhisper](https://github.com/andyhtran/MiniWhisper)** — MIT, Swift | MIT | **Adapt — hotkey robustness.** `Services/Hotkeys/`: `FnStateMachine`, `ModifierTapMonitor`, `EventTapRunLoop`, `CarbonHotKeyCenter`, **`TapHealthPolicy`**. Event taps get silently disabled by the system; `TapHealthPolicy` is the answer. |
| **[cjpais/Handy](https://github.com/cjpais/Handy)** — MIT, Tauri v2, 31.9k★ | MIT | **Reference only** (we rejected Tauri). `src-tauri/src/tray.rs` has exactly the idle/recording/transcribing tray-state model proposed in §6.2. |
| **[Beingpax/VoiceInk](https://github.com/Beingpax/VoiceInk)** | **GPL-3.0** | **Read, do not fork.** `PersistentQuickPanel.swift` and its `ShortcutMonitor` / `RecordingShortcutManager` split are the best-organised version of this problem I found — and copyleft. |
| **[synth-inc/onit](https://github.com/synth-inc/onit)** | **CC BY-NC 4.0** | **Reject as code** (non-commercial, not OSI). Excellent *design* reference: `MenuBarController`, `PanelStateManager`, the "Tether" panel that attaches to the focused window. |
| **CapSoftware/Cap** (AGPL-3.0), **FluidVoice / typewhisper-mac / TypeNo** (GPL-3.0), **epicenter** (unclear) | copyleft / unclear | **Reject.** |
| **MacWhisper, superwhisper, Willow Voice, Highlight AI** | closed | **Reject.** (Note: `toverainc/willow` is now `HeyWillow/willow` and is Apache-2.0 **ESP32 firmware**, not a Mac app.) |
| **[FluidInference/FluidAudio](https://github.com/FluidInference/FluidAudio)** — Apache-2.0, Swift CoreML | Apache-2.0 | **Hold in reserve.** Parakeet/VAD/diarization in Swift, used by Hex, Pindrop, MiniWhisper and VoiceInk. It is the fallback if Apple's `SpeechTranscriber` turns out to be worse than whisper on this workload — which is stage 0, test #2. |

**Net: the dock is assembly, not invention.** yap for the panel, Hex for the
hotkey state machine, AudioWhisper for the stdio bridge, Hermes for the
supervisor discipline and the notarization config. Estimate the hand-written
Swift at ~1,500–2,500 lines.

---

## 8. Risks

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| 1 | **No codesigning identity on this machine.** Ad-hoc signing invalidates every TCC grant on every rebuild (VERIFIED by experiment, §4.4). | **High** | Buy the Apple Developer Program **before stage 2**. Sign every local build Apple Development with a frozen `CFBundleIdentifier`. Assert in CI that `codesign --display -r -` prints a byte-identical DR across builds. |
| 2 | **Swift becomes a place where policy hides.** A UI that decides anything is a second, untested safety layer. | **High** | Hard rule: the Swift side holds no tier, no threshold, no allowlist. It renders `state`/`audit`/`confirm.request` and forwards clicks. Enforce with a review checklist and a grep in CI for tier/risk vocabulary in Swift sources. |
| 3 | **The approval card becomes prettier and less complete.** Design pressure pushes toward summarising the script. | **High** | Write the test first: given a `ResolvedAction` with an N-line script and M args, the card's accessibility tree must contain **every** line and **every** arg. Snapshot it. `_visual_detail`'s docstring is the spec. |
| 4 | **`SpeechTranscriber` quality is unknown** for this workload, and the address gate's calibration was tuned against a different (stub) transcriber. | Medium | Stage 0 test #2: transcribe the `evals/address_gate_cases.jsonl` utterances through `SpeechTranscriber` and diff against the labels. `FluidAudio` (Apache-2.0) is the drop-in fallback. |
| 5 | **Moving STT to Swift changes what the gate sees**, so `address_gate = 0.42` and `end_of_turn = 0.75` may need retuning — and the README is explicit that **no gate-quality number in this repo is real yet** (everything is `FakeJev`). | Medium | Run `evals/run_address_gate.py --live` with a real key **before** the dock exists, to get a baseline; re-run after. Do not ship always-on until both numbers are real. |
| 6 | Apple **Speech model assets** may need a first-run download (`AssetInventory.assetInstallationRequest`). | Medium | Treat as an onboarding step with real UI; fall back to push-to-talk-only until assets are installed; never fail silently. |
| 7 | **`MenuBarExtra` limitations** and the macOS 26 `openSettings` bug. | Medium | Already mitigated: use `NSStatusItem` + `NSPanel` directly. |
| 8 | **macOS 27 shipped or is imminent** (research passes disagreed; this machine is 26.5.2). Liquid Glass and Speech are both young APIs. | Medium | Set `minimumSystemVersion` to 26.0. Retest on 27 before any release. Budget a rendering-regression pass. |
| 9 | **Embedding CPython** — library validation vs unsigned wheels, `--deep` skipping `.so` files in `Resources/`, notarization rejecting one unsigned dylib. | Medium | Stage 4 problem, not stage 1. Ship stage 1–3 pointing at the developer's own `.venv`. When bundling: `--onedir`, inside-out signing script, re-sign every `.so` with the Team ID, verify with `codesign -d --entitlements -`. |
| 10 | **Two stdout owners.** Any stray `print()` in the Python tree corrupts the protocol. | Medium | Steal `sys.stdout` at bridge start, redirect to stderr, keep the real fd private (Hermes' `_real_stdout`). Add a test that imports the whole tree under the bridge and asserts fd 1 saw nothing but valid frames. |
| 11 | **The dock makes always-on feel free**, and always-on with an unreal gate is a hot mic. | Medium | Always-on stays off by default (`Settings.always_on = False` today). Gate the toggle behind a one-time explainer and behind risk #5 being closed. |
| 12 | A **confirmation timeout** is new behaviour: today `console.ask` blocks forever. | Low | It fails closed, which is strictly safer. Audit it as a distinct outcome (`visual_confirm granted=False reason="timeout"`) so it is visible in `daa doctor`/History and not mistaken for a user refusal. |
| 13 | Screen Recording's **monthly re-prompt** if `kCGWindowName` stays a dependency. | Low–Medium | Stage 0 test #1: prove `kAXTitleAttribute` works with only Accessibility, then drop Screen Recording. |
| 14 | Restart loops — a crashing Python child respawned forever. | Low | Exponential backoff to 30 s; after 5 consecutive failures within 2 minutes, stop, sit in `degraded`, and show the log. |

---

## 9. Build order

Effort is one focused engineer-day unless stated. Each stage is shippable.

### Stage 0 — Decide the two open questions (0.5 day)
- **Test #1:** does `AXUIElementCopyAttributeValue(window, kAXTitleAttribute)`
  return other apps' window titles with **only** Accessibility granted and no
  Screen Recording? If yes, delete Screen Recording from the product.
- **Test #2:** run `evals/address_gate_cases.jsonl` through `SpeechTranscriber`
  and diff against the labels. Decide Apple Speech vs FluidAudio.
- **Buy the Apple Developer Program.** Nothing downstream is safe without it.
- Install Xcode (for Instruments, previews, and Icon Composer) — optional for
  building, useful for polishing.

### Stage 1 — The bridge, headless (2 days)
New files only; `src/` otherwise untouched.
- `src/daa/ui/protocol.py` — frame encode/decode, the envelope, the event names.
- `src/daa/ui/bridge.py` — reader/writer/turn threads, `BridgeMic`,
  `BridgeTranscriber`, `DockConsole`, the audit tee, stdout theft.
- `daa bridge` subcommand in `cli.py`.
- Tests: a fake peer drives the whole protocol from `pytest` with no Swift at
  all — utterance in, `audit` frames out, a `confirm.request` answered both
  ways, a malformed frame ignored, a timeout failing closed, stdout never
  polluted. **This stage is fully testable without a line of Swift**, which is
  the point of doing it first.
- **Exit:** `daa bridge` is drivable from a Python test peer and `daa listen`,
  `daa say`, `daa undo` are bit-for-bit unchanged.

### Stage 2 — The shell (2 days)
- Fork yap. Strip to: `NSStatusItem`, one `NSPanel`, `PythonSupervisor`
  (spawn `/Users/sanjay/daa/.venv/bin/python -u -m daa.cli bridge`, lift
  AudioWhisper's `PythonDetector`), and the frame codec.
- Hand-rolled `.app` bundle + `Info.plist` + entitlements + an
  `Apple Development` signing step in a `make dock` target.
- Transcript view driven by real `audit` frames. No mic yet — type into the
  panel, which exercises `handle_text`.
- **Exit:** clicking the menu bar shows real daa output from a real child, and
  `codesign --display -r -` is stable across two consecutive builds.

### Stage 3 — The approval card (2 days) ← the point of the project
- The 8-line `present` addition to `_confirm_visual`, plus its tests.
- `ApprovalPanel` per §6.4: verb-first, script-first, hold-to-approve, no
  default button, 400 ms inert, scroll-gated approval, visible countdown,
  dry-run banner, `synthetic` disclosure.
- Timeout → `granted:false`. Child death → `granted:false`.
- Accessibility-tree snapshot test asserting nothing is elided.
- **Exit:** `daa say "run a script that ..."` raises a real card, and the only
  way to approve is a deliberate 600 ms hold.

### Stage 4 — Voice (3 days)
- `SpeechDetector` + `SpeechTranscriber` in Swift; utterances over `mic.utterance`.
- Carbon `RegisterEventHotKey` push-to-talk; Esc cancel; tap-to-latch.
- `mic.onset` → barge-in (the Python side already has `_on_speech_start`).
- The idle/listening/thinking/speaking/awaiting state machine and the menu bar
  animation.
- **Microphone TCC prompt now appears against `ai.daa.dock`** — the moment the
  architecture pays for itself.
- **Exit:** hold `⌥Space`, speak, watch the dock move through every state, and
  get a real approval card at the end.

### Stage 5 — Polish and completeness (3 days)
- History window over `audit.jsonl`; undo affordances; the Set-up tab wrapping
  `daa doctor` + `permissions.REMEDY` + `SETTINGS_URL`.
- Dry-run pill, always-on toggle + explainer, degraded state, restart backoff.
- Liquid Glass materials, SF Symbol effects, spring timings, Reduce Motion and
  Reduce Transparency honoured, full VoiceOver pass on the approval card.
- **Exit:** it looks and feels like an Apple app, and a screen reader can read
  the approval card completely.

### Stage 6 — Shipping and the next thing (3 days)
- Embed CPython (`--onedir`), inside-out signing script, re-sign every `.so`,
  Developer ID, hardened runtime, `notarytool`, `stapler`, DMG.
- Reserve `task.update` / `task.done` and build the task strip stub.
- Optional: the Unix-socket transport, if background tasks must outlive the dock.
- Optional: a Textual TUI over the same protocol for headless/SSH use.

**Total: ~15–16 focused days.** Stages 1–3 (~6 days) already deliver the thing
the brief actually asks for — a clicky dock with a real, readable, deliberate
approval card — with the voice path still going through `daa say`.

---

## 10. Appendix: verified vs inferred

**VERIFIED by running it on this machine (2026-09-21):** macOS 26.5.2 / arm64;
Xcode absent, CLT present, Swift 6.3.3, SDK 26.5; SwiftUI `MenuBarExtra`
compiles and runs with CLT only (63 KB binary, 68 KB bundle, 77 MB RSS);
`swift build -c release` works; zero codesigning identities; **ad-hoc DR is a
cdhash and changes on every rebuild**; hardened runtime + entitlements apply
correctly under ad-hoc signing; `Speech.SpeechAnalyzer` / `SpeechTranscriber` /
`SpeechDetector` / `AssetInventory` present in the 26.5 SDK; SwiftUICore
`glassEffect` / `GlassEffectContainer` / `Glass` present; Carbon
`RegisterEventHotKey` compiles; `rumps` 0.4.0 + `pywebview` 6.2.1 +
`pyobjc` 12.2.2 install and import, and a rumps app runs with Homebrew's
`Python.app` as its host binary; Hermes' `release/mac-arm64` is 307 MB and its
`electron/` has no tray/panel/hotkey source; daa's venv is 92 MB without voice
extras; daa cold subsystem import is 0.05 s; `daa doctor` reports accessibility
denied, mic/local-STT fake, cloud STT/TTS/LLM/Jev live; `~/.daa/audit.jsonl`
already contains every event kind the dock needs.

**VERIFIED from registries:** Electron 44.4.3 (2026-09-18, MIT);
`@tauri-apps/cli` 2.11.5 (2026-09-20, MIT/Apache-2.0); electron-builder 26.15.3;
rumps 0.4.0 **uploaded 2022-10-15**; pywebview 6.2.1 (2026-04-15);
pyobjc-core 12.2.2 (2026-08-11); py2app 0.28.10 (2026-02-13);
PyInstaller 6.22.3 (2026-09-12, GPL-2.0-or-later with exception);
briefcase 0.4.5 (2026-09-08); textual 8.2.8 (2026-06-30).

**VERIFIED from Apple documentation / Apple DTS:** the responsible-code model
and its breakage modes; ad-hoc signing has no stable DR (TN3127 + DTS);
microphone requires both the usage string and `com.apple.security.device.audio-input`
or the app is terminated; Apple Events requires
`com.apple.security.automation.apple-events` under hardened runtime and is keyed
per (client, target); there is no entitlement for Accessibility or Screen
Recording; `--deep` is deprecated and skips code in data locations;
notarization does not affect TCC; the macOS 26.0–26.2 Privacy-UI bug for
non-bundled executables.

**INFERRED (my reasoning, not sourced):** that `kAXTitleAttribute` under
Accessibility alone replaces the Screen Recording dependency (test it);
that a self-signed certificate would give stable local grants; the effort
estimates in §9; the specific wire-protocol method names; every pixel of §6.

**NOT VERIFIED, flagged by the research passes:** SwiftUI bundle/RAM figures
have no authoritative public benchmark (mine above are measurements of a
*trivial* app, not of the finished dock); rumps on Python 3.12/3.13 and macOS 26
has no maintainer statement either way (it did run here); py2app and PyInstaller
have no positive macOS 26 support statement; Electron 44's release notes never
mention Tahoe; Tauri/Electron size and RAM comparisons rest on one N=1
benchmark; the current macOS release number (26.6.2 vs 26.7 vs 27) — the two
research passes disagreed, and only this machine's 26.5.2 was checked directly.
