# Computer use for daa — recommendation

Researched 2026-09-21 on macOS **26.5.2 Tahoe** (Darwin 25.5.0, arm64, build 25F84),
Python 3.12.13, against the repo at `/Users/sanjay/daa`. Claims are tagged
**[verified]** (run on this machine), **[sourced]** (primary doc, header, PyPI/GitHub
metadata, or bug tracker read this session) or **[inferred]** (reasoning). Nothing
under `src/` or `tests/` was modified.

> **Scope note.** macOS **27 "Golden Gate" shipped 2026-09-14** (Apple Silicon only,
> Darwin 27). This machine is on N-1. Everything below is scoped for 26 *and* 27;
> the two places where 27 changes the answer are flagged. **[sourced]**

---

## Recommendation

**Build it yourself, on pyobjc, accessibility-first, with no new runtime
dependency — and make the unit of action a *named element*, never a coordinate.**
There is nothing open-source worth depending on. The nearest prior art
(NousResearch's `hermes-agent`, MIT) contains **zero macOS code** — it is a
3,295-line MCP client for `cua-driver`, a third-party Rust binary installed by
`curl | bash` that holds its own TCC grants and drives the Mac through private
SkyLight SPIs. Everything else is dead (`atomacos` — archived 2024, GPL-2.0, last
release 2021), abandoned-with-known-broken-input (`pyautogui` — unmerged fixes for
double-click and non-US keyboards, and no Pillow on 3.12), or a competing agent
framework that would drag `litellm`, `anthropic`, `openai`, `groq`, `mistralai`,
`google-genai` and PostHog into a project that already has its own LLM layer and
its own safety model. What daa needs is small:
`pyobjc-framework-ApplicationServices` and `-Quartz` are **already in
`pyproject.toml`**, installed at 12.2.2 (2026-08-11, MIT, tracks the macOS 26.5
SDK, already builds under Xcode 27), and **[verified]** expose every AX and CGEvent
symbol required. The real work is not the automation — it is the contract, and the
contract is where every existing implementation fails: hermes's own approval prompt
reads `"click element #7"` (`tools/computer_use/tool.py:569`), which is the exact
lie `README.md §4` exists to forbid. Take from hermes its *interface* ideas
(verify→escalate, background-vs-foreground delivery as a separately approved axis,
the snapshot `element_token`, timeout-is-not-consent, the hard key-combo deny-list);
take from `cua-driver` its *packaging* idea (a code-signed `.app` with a stable
bundle id owns the TCC grant, not your interpreter). Reject both implementations.

---

## 1. The stack, and why

| Layer | Choice | Status |
|---|---|---|
| Element model | `AXUIElement*` via `pyobjc-framework-ApplicationServices` 12.2.2 | already a dependency |
| Press / set | `AXUIElementPerformAction(el, kAXPressAction)`, `AXUIElementSetAttributeValue` | already a dependency |
| Fallback input | Quartz `CGEventCreate{Mouse,Keyboard,ScrollWheel}Event` + `CGEventPost` | already a dependency |
| Hit-test | `AXUIElementCopyElementAtPosition` | already a dependency |
| Capture (confirmation card only) | `pyobjc-framework-ScreenCaptureKit` 12.2.2 | one optional new package |
| Vision grounding | **none** | deliberately |

**[verified]** pyobjc 12.2.2 is installed in `.venv` (core, Cocoa, Quartz,
ApplicationServices, CoreText). Every symbol needed is present — I probed all of
`AXUIElementCreateApplication`, `CreateSystemWide`, `CopyAttributeValue`,
`CopyAttributeNames`, `CopyActionNames`, `PerformAction`, `SetAttributeValue`,
`CopyElementAtPosition`, `CopyMultipleAttributeValues`, `SetMessagingTimeout`,
`GetPid`, the `kAX*` attribute/action constants, and in Quartz
`CGEventCreateMouseEvent`, `CGEventCreateKeyboardEvent`,
`CGEventKeyboardSetUnicodeString`, `CGEventCreateScrollWheelEvent`,
`CGEventSetFlags`, `CGEventPost`, `CGEventPostToPid`, `CGEventSourceCreate`,
`CGWindowListCreateImage`, `CGDisplayCreateImage`. **Missing: none.**

**[verified]** With Accessibility denied, every AX call returns `-25211`
(`kAXErrorAPIDisabled`) — uniform, non-raising, and usefully distinguishable from
`-25205` (`kAXErrorAttributeUnsupported`, "this app has no such attribute") and
from an empty tree. `AXUIElementSetMessagingTimeout` returns `0` (success) *even
without the grant*. pyobjc accepts arbitrary attribute-name strings, so
`AXManualAccessibility` can be set without a bound constant — confirmed the call
signature reaches TCC rather than failing on types.

**[sourced]** `AXUIElement` is undeprecated on macOS 26 **and 27**: Apple's
documentation feeds for `AXUIElementCopyAttributeValue`,
`AXIsProcessTrustedWithOptions` and `AXObserverCreate` carry no deprecation entry,
and no replacement API was announced in either release. It remains the only public
documented cross-app GUI automation surface.

### Why accessibility-first is not a preference — it is the only option that satisfies the contract

`ResolvedAction.targets` is documented as *"Human-readable, speakable
resolutions"*. A screenshot yields `(847, 312)`. There is no honest sentence you
can build from that pair of integers. The AX tree yields `AXButton` /
`AXTitle="Delete Account"` / in window `"Account Settings"` / of `Safari`, which
composes directly into *"press Delete Account in Account Settings in Safari"*.
**Vision-first is not a slower path to the same place; it is a path to a place
where the confirmation cannot exist.** That settles the architecture before any
coverage argument.

Three further arguments point the same way:

1. **AX needs a strictly smaller grant.** The element route needs *Accessibility
   only*. Screen Recording buys window titles and the optional preview;
   Automation (AppleEvents) is not needed at all. Vision-first needs Accessibility
   (to post events) **and** Screen Recording (to see) — and **[sourced]** Screen
   Recording carries a **monthly re-consent prompt** on Sequoia and Tahoe that no
   setting disables, so a vision-first daa silently breaks once a month.
2. **AXPress does not steal the machine.** `AXUIElementPerformAction(el,
   kAXPressAction)` presses a specific element in a specific process without
   moving the cursor, raising a window, or changing key focus. `CGEventPost` moves
   the real pointer. For an always-on assistant that runs *while you work*, this is
   the difference between a usable product and one you switch off. **[inferred]** —
   untestable with the grant denied; it is Apple's documented behaviour and is what
   hermes markets as *"Background computer-use: does NOT steal the user's cursor or
   keyboard focus"* (`computer_use_tool.py:29-31`).
3. **AX is verifiable after the fact.** After an AXPress you can re-read the element
   and say whether anything changed. After a CGEvent click at a coordinate you can
   only take another screenshot and guess. Hermes encodes exactly this as
   `ActionResult.effect ∈ {confirmed, unverifiable, suspected_noop}`
   (`backend.py:88-110`) — the best idea in that codebase, and an interface rather
   than an implementation.

### The screenshot forgery — the most important experimental result here

**[verified, and it got worse when I re-ran it.]** This is a two-part observation
from the same interpreter, twenty minutes apart.

**Part one, Screen Recording *not granted*.** `CGPreflightScreenCaptureAccess()`
returned `False`. `CGWindowListCreateImage(CGDisplayBounds(main), …)` nevertheless
**succeeded**: a 3456×2234 PNG, 8.6 MB, in 29 ms. It did not raise, did not return
`None`, and was not black. I saved it and looked at it. It contained the desktop
wallpaper and the **live menu bar** (`Chrome  File  Edit  View  History
Bookmarks  Profiles  Tab  Window  Help`), and **every one of the sixteen running
applications' window content was silently removed**. A "did we get a real
screenshot?" heuristic — non-null, non-black, plausible dimensions, OCR-able text
in the menu bar — passes on that image.

**Part two, Screen Recording granted.** The identical call on the identical
interpreter returned the real screen: Activity Monitor with its process table,
desktop widgets, the Dock, forty desktop icons. Same API, same arguments, same
25 ms, same success return — **the only synchronous difference is the preflight
boolean.**

Three consequences, all load-bearing:

1. **Any capture path must gate on `CGPreflightScreenCaptureAccess()` first and
   treat an image obtained without it as void, not as data.** A vision-first daa
   in part-one state would ground a click on a fabricated screen and the
   confirmation would faithfully describe a desktop nobody is looking at.
2. **The capture call itself provoked a TCC consent flow out of band**, which is
   why the state changed between the two runs. A capture inside `resolve()` — the
   phase the contract calls *"always safe to call"* — is therefore not
   side-effect-free: it can raise a system permission dialog in front of a user
   who was talking to a voice assistant. `permissions.py` already forbids
   prompting probes for exactly this reason; the rule must extend to capture.
3. **[verified]** The part-two screenshot *itself contains a live TCC consent
   dialog* — *"'Warp' is requesting to bypass the system private window picker and
   directly access your screen and audio. [Allow] [Open System Settings]"*. A
   vision-grounded agent handed that frame can be asked to click **Allow**. That
   is privilege escalation through the confirmation channel, and it is not
   hypothetical — it was in the first real screenshot I took. The AX route is not
   immune, but it *names* the button: *"press Allow in a system dialog"* is a
   sentence a user can refuse.

**[verified]** The same screenshot also shows a notification headed **"Python"
Notifications**. That is the TCC subject the grant just attached to. The row a user
sees in System Settings says **"Python"**. See §5.

### AX coverage on real apps

**[verified]** the running-app set on this machine (`activationPolicy == 0`): Music,
Finder, Activity Monitor, Preview, Google Chrome, Slack, Code, Antigravity IDE,
iTerm2, HeyClicky, Calendar, Warp, Wispr Flow, ChatGPT, Safari, Pen. A
representative, Electron-heavy sample — use it rather than a hypothetical one.

| Class | Apps here | Expected AX quality |
|---|---|---|
| Native AppKit / SwiftUI | Music, Finder, Activity Monitor, Preview, Calendar, Safari chrome | Rich. Roles, titles, `AXPress`, `AXIdentifier`. SwiftUI auto-generates `AXOutline`/`AXRow` via its AppKit internals. The happy path. |
| WebKit content | Safari page content | Good. **[sourced]** Browser-internal AX trees are implementation details, not standardised — the WebDriver "AT-classic" tree-extraction proposal is early-stage and unshipped. |
| Electron / Chromium | Chrome page content, Slack, Code, Antigravity IDE, ChatGPT, Pen | **Off by default.** **[sourced]** Chromium's own design doc: it *"turns on or off accessibility support based on whether it sees a client… has set the `AXEnhancedUserInterface` attribute."* Must be opted into per app (see below). Once on, labels are usable and trees run 500–1000+ nodes. Known gaps persist: VS Code has a long tail of VoiceOver focus bugs; Slack has an Electron-wide AX text-selection bug (electron#36337). |
| Custom-rendered | Warp, iTerm2 grid, HeyClicky | **Poor to absent.** A GPU-drawn terminal grid has no element tree worth the name. |

**Use `AXManualAccessibility`, not `AXEnhancedUserInterface`.** **[sourced]**
`AXManualAccessibility` is the Electron-sanctioned opt-in — it is in Electron's own
docs (`AXUIElementSetAttributeValue(appRef, CFSTR("AXManualAccessibility"),
kCFBooleanTrue)`), and the historical bug where Electron failed to advertise it
(returning `-25205`) was fixed in electron#38102.
`AXEnhancedUserInterface` works too but **carries a real hazard**: setting it makes
Chromium/Electron animate window moves and resizes, so consecutive AX frame-set
calls collide with the unfinished animation and produce windows that move but do
not resize. Rectangle's debug log literally prints *"AXEnhancedUserInterface was
enabled, will disable before resizing"*; Phoenix does the same dance; Mozilla filed
the mirror-image complaint (bugzilla 1664992). daa's `focus_window` already touches
window geometry, so this is not theoretical for this codebase. **Set
`AXManualAccessibility`; never set `AXEnhancedUserInterface`; if an app has it set
by something else and we are about to move a window, disable and restore it.**

So: roughly half these apps are the happy path, a large minority work only after a
flag and produce trees big enough to need aggressive pruning, and a tail has no
usable tree. **The honest design consequence is not "add vision as a fallback" — it
is "refuse".** See §3.

---

## 2. What I evaluated and rejected

Versions, dates and licences are **[verified]** from PyPI JSON / `brew info`, or
**[sourced]** from the project's own repo, on 2026-09-21.

| Option | Version / last release | Licence | Verdict |
|---|---|---|---|
| **pyobjc** (core, Cocoa, Quartz, ApplicationServices) | 12.2.2, **2026-08-11** | MIT | **Adopt.** Already a dependency. cp312 `universal2` wheels, no build step. Changelog: *"Update framework bindings for macOS 26.5 SDK"*, *"can now be built using Xcode 27"*. Last commit 2026-09-05. |
| `pyobjc-framework-ScreenCaptureKit` | 12.2.2, 2026-08-11 | MIT | **Adopt for capture.** Installs; `SCScreenshotManager`, `SCShareableContent`, `SCContentFilter`, `SCStreamConfiguration` all present. See the obsoletion note below. |
| `atomacos` | 3.3.0, **2021-05-24** | **GPL-2.0** | **Reject.** GitHub repo **archived 2024-02-29**; classifiers stop at Python 3.7; all three known forks also dead. GPL-2.0 alone disqualifies it. Drags in nine packages including `pyautogui`. (It does still install on 3.12/26 — verified — which is not a reason.) |
| `atomac`, `PyXA` (`mac-pyxa`) | 2013-02-13 / 2024-01-08 | GPL-2.0 / MIT | **Reject.** atomac ships a py2.7 Intel egg. PyXA has open, unanswered issues titled *"Support recent Python & MacOS releases"* and *"Installation fails due to PyObjC using deprecated Setuptools APIs"*, and pins old pyobjc. |
| `pyautogui` | 0.9.54, 2023-05-24 | BSD-3 | **Reject — and mine it for bug reports.** Abandoned (last commit 2023-06-07, 584 open issues). Two unmerged fixes are *exactly the bugs you will write yourself*: PR #949, macOS multi-click never sets `kCGMouseEventClickState`, so `doubleClick()` does not register as a double-click in native targets; PR #947, `write()` posts virtual key codes instead of `CGEventKeyboardSetUnicodeString`, so a non-US layout types garbage (open since 2017). Also: `pyscreeze`'s Pillow markers stop at `python_version == "3.11"`, so **on 3.12 no Pillow is installed and `screenshot()` fails outright**. |
| `macos-use` (CursorTouch/MacOS-Use) | 0.2.0, 2026-05-13 | MIT | **Reject as a dependency; read for ideas.** It is an *agent*, not a driver: installing it pulls `anthropic`, `openai`, `litellm`, `groq`, `mistralai`, `google-genai`, `cerebras-cloud-sdk`, `ollama`, `posthog`, `pyautogui`, `ipykernel`. daa already has a conversational layer, a router, a risk gate and a policy module. Two releases total. |
| `mlx-use` (browser-use/macOS-use) | 0.0.3, 2025-01-27 | MIT | **Reject — dead, but note the vote.** Last commit 2025-03-05; its own README roadmap still has *"Release the first working version to pypi"* unchecked. It used **pyobjc → the AX API**, i.e. it picked the right architecture and then stopped. |
| `cua-agent` / `cua-computer` / `cua-core` | 0.8.4 / 0.5.19 / 0.3.1, 2026-06 | MIT (repo) | **Reject.** `cua-agent` pins `litellm==1.86.2` exactly; `cua-core` mandates `posthog>=3.20`; optional extras pull `ultralytics` (**AGPL-3.0**). |
| **`cua-driver`** (trycua, Rust binary) | ~0.6.8 | MIT (repo) | **Reject as a runtime dependency; steal its packaging.** Detail below. |
| Anthropic `claude-quickstarts` | pushed 2026-09-18 | MIT | **Partially relevant; do not depend on it.** `computer-use-demo/` is Linux/Docker/X11+VNC. **[sourced]** There *is* now a macOS sibling, `computer-use-best-practices/` — *"It targets macOS only. Key handling, the `pyautogui` backend, and `sandbox-exec` are all Mac-specific."* Worth reading for its `sandbox-exec` confinement idea; its input layer is pyautogui, so it inherits §5's double-click and keyboard-layout bugs. |
| Agent S / S2 / S3 (`gui-agents`) | 0.3.2, 2025-12-16 | Apache-2.0 | **Reject.** macOS backend is pyautogui + vision, not AX. `requires-python <=3.12` (3.12 is the ceiling). No release in 9 months. |
| UI-TARS / UI-TARS-desktop | UI-TARS-2, 2025-09-04; repo pushed 2026-09-11 | Apache-2.0 (code) | **Reject for v1.** Pure vision, absolute coordinates, no AX. Sidesteps every Electron gap and lands squarely on the one thing daa cannot confirm. |
| OmniParser (Microsoft) | V2, 2025-02; last commit 2026-07-20 | CC-BY-4.0 repo, **weights partly AGPL** | **Reject.** **[sourced]** Apple Silicon is not supported out of the box (Florence-2 forces a `flash_attn` import unsupported on MPS — issues #224/#302/#174). Licence contamination on the detectors. |
| Self-Operating Computer | v1.5.8, 2025-02-28 | MIT | **Reject.** Dead. |
| `screenpipe` | pushed 2026-09-21 | **not OSS** | **Reject on licence.** Moved to `screenpipe/screenpipe` (YC S26); GitHub reports NOASSERTION — a source-available commercial licence. Does use AX + OCR, but oriented at *recording* context, not driving apps. |
| `terminator` (mediar-ai) | 2026-06-02 | MIT | **Reject.** **[sourced]** Its own README support table says *"Terminator currently supports Windows only. macOS and Linux are not supported."* Several blog posts claim otherwise; they are wrong. |
| `cliclick` | **5.1, 2022-08-14** | BSD-3 | **Reject as runtime; keep as a dev oracle.** Bottles exist for `arm64_tahoe` **and** `arm64_golden_gate`, so `brew install` works — but **do not build from source**: it fails on the 15+ SDK with `'CGWindowListCreateImage' is unavailable: obsoleted in macOS 15.0`. Fix PR merged 2025-06-20, never released; issue #197 asking for a release is open. Open issue #194 is a missing click-count value — the same bug as pyautogui's. |
| `OpenAdapt` | 1.16.0, 2026-08-26 | MIT | **Not a dependency, but the strongest corroboration in this table.** Alive, `requires_python >=3.10,<3.13`, and its macOS extra depends on **`pyobjc-framework-applicationservices` / `-cocoa` / `-quartz` directly** — no atomacos, no pyautogui. The healthy 2026 projects use raw pyobjc; the dead ones wrap it badly. |
| AppleScript `System Events` UI scripting | alive — **[verified]** `osascript` works on 26.5.2 | n/a | **Already exists** as `run_applescript` at `CONFIRM_VISUAL`. Needs Accessibility *and* Automation, and its readback is the script text, not the element. The escape hatch, not the mechanism. |

### `CGWindowListCreateImage` is *compile-time obsolete*, not merely deprecated

**[sourced]** The macOS 26.5 SDK header declares
`CGWindowListCreateImage(...) SCREEN_CAPTURE_OBSOLETE(10.5, 14.0, 15.0)` —
introduced 10.5, deprecated 14.0, **obsoleted 15.0**. That is why cliclick no
longer compiles. **[verified]** The symbol still *functions* from Python on 26.5.2
(both my captures used it), because obsoletion blocks ObjC/Swift compilation
against the 15+ SDK and pyobjc resolves dynamically. **Do not build on that.** Use
`pyobjc-framework-ScreenCaptureKit` for anything that has to survive macOS 27+; it
is the same MIT release train and it installs cleanly.

### On `cua-driver` specifically — the one that nearly wins

This is what hermes actually uses, and it is good engineering: a Rust binary from
`trycua/cua` (MIT, ~25k stars, pushed daily), installed by
`/bin/bash -c "$(curl -fsSL https://cua.ai/driver/install.sh)"`. **[verified]** by
fetching and reading `install.sh` (122 lines) and `_install-rust.sh` (1,421 lines):
it installs **`/Applications/CuaDriver.app`**, bundle id **`com.trycua.driver`**,
CI-signed, LaunchServices-registered — and, the detail that shows they understood
the problem, it reads the previous bundle's designated code-signing requirement
with `codesign -d -r-`, checks the replacement with `codesign --verify -R`, and
runs `tccutil reset Accessibility com.trycua.driver` when the requirement changed,
because **TCC keys on the designated requirement, not merely on the bundle id**
(`_install-rust.sh:142-241`).

For: it solves packaging outright, does background delivery, draws set-of-mark
overlays, and works on three platforms. Against, decisively for daa:

1. **It drives macOS through private SkyLight SPIs** — `SLEventPostToPid`,
   `SLPSPostEventRecordTo`, `_AXObserverAddNotificationAndCheckRemote`. Hermes's own
   docstring says these *"aren't Apple-public and can break on OS updates."* daa's
   README already carries an honest "these paths have never run live" note; a
   dependency on undocumented SPIs makes that class of unknown permanent — and
   macOS 27 just shipped.
2. **You would hand full Accessibility and Screen Recording to a `curl | bash`
   binary you do not build.** For a project whose thesis is that the confirmation
   cannot lie, an opaque blob at the layer that touches the machine is the wrong
   trade.
3. **Telemetry is on by default.** Hermes injects
   `CUA_DRIVER_RS_TELEMETRY_ENABLED=0` (`cua_backend.py:190, 233-241`) and
   separately sanitises the child env so the driver cannot see API keys (seven call
   sites).
4. **Its element API is `element_index: int`, not a label.** No click-by-name. You
   re-derive the names anyway — paying the whole dependency cost for none of the
   part you care about.
5. **Two uncoordinated approval gates.** cua-driver has its own
   (`CUA_DRIVER_PERMISSION_MODE`, `--dangerously-bypass-approvals`), which hermes
   simply disables (`cua_backend.py:414-415`).
6. **The integration cost is the bulk of the code.** Roughly two thirds of hermes's
   3,295-line `cua_backend.py` is finding, launching, version-checking, babysitting
   and surviving that subprocess.

**Verdict: reject the binary, adopt the bundle-identity pattern (§5).** Leave a
`ComputerUseBackend`-shaped seam so it can be slotted in later. Do not start there.

### What to take, adapt and reject from `~/.hermes/hermes-agent` (MIT, © 2025 Nous Research)

**Take (as ideas — nothing vendors cleanly, because it is an MCP client):**

- `ActionResult`'s verdict fields — `verified`, `effect ∈ {confirmed, unverifiable,
  suspected_noop}`, `path`, `degraded` (`backend.py:71-110`). Transport success is
  not semantic success. Maps straight onto `ToolResult.data`.
- **Background vs foreground as a separately approved axis.** Hermes requires a
  *distinct* approval for `bring_to_front` even when the input was already approved
  (`tool.py:485-493`), because raising a window is a visible side effect nobody
  consented to. In daa this becomes
  `consequences["focus"] = "this will bring Slack to the front"`.
- **`element_token`** — an opaque `s{snapshot}:{index}` handle so a stale reference
  returns an *error* instead of silently resolving to a different element
  (`backend.py:31-38`). §3.3 needs exactly this.
- **Timeout is not consent** — `tool.py:552-558`: *"the user did not respond.
  Silence is not consent; do not retry without the user."*
- **Never screenshot after a failure** — `tool.py:1253-1262`.
- **The hard key-combo deny-list** — `tool.py:96-109`: ⌘⇧⌫ (empty Trash), ⌘⌥⌫,
  ⌘⌃Q (lock), ⌘⇧Q (log out), ⌘⌥⇧Q — plus the bug-fix that earns its comment: the
  canonicaliser splits on `+` **and** `-`, because the backend accepts hyphens and
  `"ctrl-alt-delete"` would otherwise walk straight through the gate.
- **The typed-text deny-list** — `curl|bash`, `wget|sh`, `sudo rm -rf`, `rm -rf /`,
  fork bomb (`tool.py:128-136`), checked *before* the approval prompt.

**Adapt:** the read/write split (`_SAFE_ACTIONS` / `_DESTRUCTIVE_ACTIONS`,
`tool.py:81-94`) — in daa that is not a set literal, it is the `RiskTier` floor on
each `ToolSpec`, which is stronger because policy may only raise it. And
`max_elements` capping — necessary for Electron, but cap at *walk* time, not at
*response* time.

**Reject:**

- **The approval gate defaults to allow.** `tool.py:533-536`: no registered callback
  ⇒ `_request_approval` returns `None`, i.e. permitted. daa fails closed everywhere.
- **`_summarize_action` — the reason this document exists.** `tool.py:565-587`
  produces `"click element #7"`, `"click at (847, 312)"`, `"drag 3 → 9"`. Hermes
  *has* the label — `_format_elements` renders `#3 AXButton 'Save' @ (x,y,w,h)`
  (`tool.py:1298-1307`) — and the approval prompt never resolves the index back to
  it. This is precisely README §4's *"'delete report.pdf' and 'reveal report.pdf'
  read back identically."*
- Session-wide `always_approve` / YOLO mode. daa has no tier below the floor.
- The MCP client, subprocess lifecycle, CLI-transport fallback and update-nudge
  machinery (~2,200 lines that exist only because the work happens elsewhere).
- `vision_routing.py` — it solves "the main model cannot read images", which daa
  does not have, because daa is not sending images to a model.

---

## 3. The readback design — the crux

### 3.0 The rule

> **A computer-use action that cannot be named from the accessibility tree at
> resolve time is not performed.**

Not "performed with a weaker description", not "falls back to a screenshot".
Refused. Everything below follows from that sentence.

### 3.1 The unit of action is an element; the coordinate is never an argument

`click_at(x, y)` does not exist in this design. The signature is:

```python
ui_click(target="the Save button", app="TextEdit", window=None)
```

`resolve()` walks the live AX tree, ranks candidates with the fuzzy matcher that
already exists in `tools/base.py` (`normalize_spoken`, `fuzzy_score`,
`rank_candidates` — written for exactly this, including the `MATCH_FLOOR = 0.55`
noise floor `windows.py` already uses), and returns **one** element. The coordinate
is computed inside `run()` and never leaves it.

The inversion is forced by the contract, not chosen. `resolve()` is defined as the
thing that turns loose arguments into concrete speakable targets. If the argument
is already a coordinate, `resolve()` has no work to do and therefore nothing to
say. If the argument is a description, `resolve()` *must* disambiguate, and the
by-product of disambiguation is the name. Anthropic's computer-use API takes
coordinates; hermes takes an opaque index; both make `resolve()` vacuous.

Ambiguity is a first-class outcome, not a tiebreak: three matches for "save"
produces *"I found three things called Save — the Save button, Save As…, and Save
All. Which?"*, which is the pattern `rank_candidates` was built for.

### 3.2 The readback is derived from the tree, never from the model's phrasing

This is `run_applescript`'s rule applied to a new tool. `applescript.py:113-121` is
explicit about why: *"`purpose` is written by the MODEL. Using it as the readback
asks the user to check the model against a sentence the model wrote: a script that
empties an inbox can introduce itself as 'check what time my next meeting is', and
it did."*

The identical attack exists here. The model asks to click *"the OK button"*. The
tree says `AXButton` with `AXTitle="Delete Account"`, inside `AXWindow "Account
Settings"`, in Safari. **The readback says what the tree says.** The model's word
travels in `args` as `requested_target`, for the log and the card, and is never the
target.

The target string is composed from AX attributes only:

```
{AXRoleDescription} {AXTitle | AXDescription | AXValue | AXHelp}
  in {window AXTitle} in {app localizedName}
→ "the Delete Account button in Account Settings in Safari"
```

`verb = "press"`, so `describe()` yields *"press the Delete Account button in
Account Settings in Safari"* with no new machinery at all.

**`consequences` carries everything `resolve()` can compute that changes what a yes
means** — the discipline `move_to_trash` uses for "including 401 items inside":

| Detectable at resolve time | `consequences` entry |
|---|---|
| Title matches a destructive lexicon (Delete, Remove, Erase, Discard, Don't Save, Send, Pay, Buy, Confirm, Sign Out, Reset, Allow) | `"wording": "the button is labelled Delete Account"` |
| Ancestor is `AXSheet`/`AXAlert`, or element is the window's `AXDefaultButton` | `"modal": "this is the default button of an alert"` |
| **The window belongs to a system process (`com.apple.*`, `UserNotificationCenter`, TCC)** | `"system": "this is a macOS permission dialog"` — see §1, part three |
| Action raises a window or changes focus | `"focus": "this will bring Safari to the front"` |
| Target field is `AXSecureTextField` | **refuse outright** — daa does not type into password fields |
| `AXEnabled == False` | resolve fails: *"the Save button is there but greyed out"* |
| Element's app ≠ the app the user named | `"app": "this is in Safari, not in Mail"` |
| Always, for every mutating UI action | `"undo": "I can't undo this — whether it can be taken back is up to the app"` |

The destructive lexicon is a direct analogue of `applescript.DANGEROUS`. It is
only ever matched against the **tree's** title, never the model's.

**Use `ResolvedAction.floor_hint` for the ones that must not be negotiable.**
(That field is not in the committed `contracts.py` I read at the start of this
research — it appeared in the working tree during the session, added by parallel
work on browser-use. If it lands, this design should use it; if it does not, the
paragraph below is the argument for adding it.) Without it, "this button says
*Delete Account*" can only reach a stronger tier by convincing a judgment model,
and a confidently-wrong Jev answer is exactly what floors exist to defend against.
`floor_hint` is set by deterministic code that read the real AX tree, and policy
takes `max(spec.floor, floor_hint, derived)` — raise-only, so a resolver can never
make anything cheaper. Concretely:

| Resolver finding | `floor_hint` |
|---|---|
| Destructive lexicon hit on the element's own title | `CONFIRM_VISUAL` |
| Element is in a system permission dialog (§1 part three) | `CONFIRM_VISUAL` |
| Element is the default button of an alert | `CONFIRM_VISUAL` |
| Everything else | `None` (the tool floor stands) |

That is the difference between *"we asked the model nicely and it agreed this was
dangerous"* and *"the tree said the word Delete, so this one goes on screen."*

### 3.3 Sequences: a scoped grant bounded by content, not by time

Computer use is sequential — press Save, type a filename, press Return. Confirming
each step by voice is unusable; a blanket "yes, do UI things for five minutes" is
the lie the model forbids. The answer that fits the existing types is a **bound plan
in one `ResolvedAction`**.

`ui_sequence.resolve()` walks the whole plan against the current tree and returns
one action whose `targets` are the ordered step descriptions:

> *"in TextEdit: press Save, then type 'quarterly notes', then press Return — three
> steps. I'll stop if the screen stops matching."*

Two rules make that one sentence honest for all three steps:

**(a) Re-verification is a precondition on every step.** Before step *N*, `run()`
re-reads the tree and checks that the element it is about to touch still matches the
identity that was read back — role, title, window, and a snapshot token of the kind
hermes calls `element_token`. If it does not match, `run()` **stops** and returns
*"the screen changed after step two, so I stopped."* Consent was given to a
specific named chain; the chain is abandoned the moment reality diverges from the
sentence the user approved. This beats a duration- or app-scoped grant because it
is bounded by *content*.

**(b) Deferred steps may type or press keys. They may never click.** You cannot
resolve step 3 against step 1's tree — the Save dialog does not exist until Save is
pressed. So each step is typed `bound` (resolved now against a live element) or
`deferred` (described, not yet resolvable), and the readback says so:

> *"…then, in whatever save dialog appears, type 'quarterly notes' and press
> Return"*, with `consequences["unseen"] = "the last two steps happen in a window I
> can't see yet"`.

A deferred step's action is self-describing — literal text, or a named key — so the
sentence stays complete even though the target is not yet known. **"Click whatever
appears" is precisely the unspeakable action, and it is forbidden.** A plan whose
deferred steps include a click does not resolve. (Note how neatly this also blocks
the §1 escalation: an agent cannot blind-click the Allow button of a dialog it
provoked, because that click would have to be deferred.)

`ui_sequence` sits at `CONFIRM_VISUAL`: a multi-step plan is a program, and the
precedent for "the target IS the program" is `run_applescript`. `_visual_detail()`
already prints every argument in full, so the ordered plan lands on the card with no
loop changes.

### 3.4 If you ever do need a coordinate: hit-test it

I recommend **not** building `ui_click_point` in v1. But the question deserves an
answer, because the Warp/HeyClicky tail is real.

The answer is that the same API that gives you names lets you *check* a coordinate:
`AXUIElementCopyElementAtPosition(systemWide, x, y)` — **[verified]** present in
pyobjc 12.2.2, correct signature, returns `(-25211, None)` while denied.

> **A coordinate that does not hit-test to a nameable element is a coordinate you
> may not click.**

`resolve()` hit-tests `(x, y)`. If it names something, the readback is that name and
the coordinate is redundant — you are back in §3.2 and nothing is lost. If it names
nothing, **there is no honest readback and the tool refuses.** This turns the
coordinate from an unconfirmable primitive into a *proposal* that must survive an
independent check against the same source of truth the readback uses. It also
demotes any future vision model from an authority to a *suggester*: it proposes
coordinates, AX adjudicates and names them, and anything AX cannot name is dropped.
That is the only shape in which vision grounding is compatible with this safety
model — and it is, not coincidentally, the shape UI-TARS and OmniParser do not have.

### 3.5 The rendered preview — why it is not the answer

A screenshot with the target outlined is an appealing readback and is worth having
on the `CONFIRM_VISUAL` card *as corroboration*. It cannot be primary:

1. **[verified]** It requires Screen Recording, and without it the capture is a
   convincing forgery (§1). It is only trustworthy behind a
   `CGPreflightScreenCaptureAccess()` gate — and **[sourced]** that grant re-prompts
   monthly, so the preview will vanish periodically and must degrade, not fail.
2. `CONFIRM_VISUAL` today is `_visual_detail()` — plain text to a `Console`
   (`voice/loop.py:1042-1078`). There is no image channel. iTerm2 shows inline
   images, Terminal.app does not, so an image card either depends on the terminal
   emulator or opens a Preview window — itself a focus-stealing side effect *during
   a confirmation*.
3. A picture is not a *spoken* readback, and `CONFIRM_VOICE` — where most of these
   actions belong — has no screen at all.
4. **[verified]** §1 part three: a screenshot can contain a permission dialog. A
   preview is a channel through which a forged or adversarial frame reaches the
   user's judgement. The named sentence is not.

**Use it as a secondary artifact at `CONFIRM_VISUAL` only, gated on the preflight,
never as a substitute for the named target.**

---

## 4. Undo, and what `inverses=()` should mean here

**There is no honest `UndoAction` for a click, and the types should say so loudly.**

- The state change does not belong to this process. It happened inside the target
  app, and only that app knows what it was.
- **⌘Z is not an inverse.** It is another click whose meaning is defined by the app;
  it can be a no-op, it can be unimplemented, and it can undo something the *user*
  did by hand five minutes ago. Recording it as an `UndoAction` would make the undo
  journal assert a relationship that does not exist — exactly what
  `ToolSpec.inverses` exists to prevent (README §5: the journal is a world-readable
  file and every row is a claim).

**So: every mutating computer-use tool declares `inverses=()`, `irreversible =
True`, `mutates = True`, the `"irreversible"` tag, says so in its
`activation_hint`, and sits at `CONFIRM_VOICE` or above.** That is precisely the
four-part escape hatch `tests/test_undo_coverage.py` already defines, and the same
trade `run_applescript` and `run_shortcut` already made. Add the new names to that
test's `IRREVERSIBLE` set — a one-line diff a reviewer sees, which is the point of
keeping it as data rather than as a predicate.

Two refinements:

**`irreversible` is about to mean two different things, and the user should hear the
difference.** Today it means *"arbitrary generated code, effect unknowable"*
(`run_applescript`). For a click it means *"effect known and named, but owned by
another process"*. I would **not** change `contracts.py` for this — instead always
emit `consequences["undo"] = "I can't undo this — whether it can be taken back is up
to the app"`. `describe()` appends `consequences` verbatim, so it reaches the
sentence the user answers, which is the only place it matters. Zero contract churn.

**Reject the one tempting real undo.** `ui_type` into a plain text field *could*
return an inverse: `resolve()` read the field's prior `AXValue`, so
`ui_set_value(previous)` restores it. Say no. First, the restore is only correct if
nothing else touched the field in between — the general case is a race, and a wrong
restore is worse than none. Second and decisively, it would write **arbitrary
on-screen text** into `~/.daa/undo.jsonl`. The README already flags this class of
problem — *"a consumed `set_clipboard` undo keeps the previous clipboard in the 0600
journal indefinitely"* — and the contents of an arbitrary text field are strictly
worse to persist than a clipboard. **`irreversible = True` and a clean journal beats
a fragile undo and a journal full of the user's screen.**

---

## 5. Permissions: zero to granted, and what packaging implies

### Where it stands, and a warning about measuring it

**[verified]** Accessibility is **denied** (`AXIsProcessTrusted() == False`; every
AX call `-25211`). Screen Recording **flipped from denied to granted during this
research session**, because — see §1 — the capture call provoked the consent flow.
Automation for `com.apple.finder` reports not-granted, while `osascript` driving
System Events works, i.e. **Apple Events and Accessibility are independent grants
and one tells you nothing about the other** — `permissions.py` already models them
separately, which is correct.

The mid-session flip is itself the lesson: **TCC state is not a constant, and a
probe that prompts will change the thing it measures.** `permissions.py`'s
non-prompting discipline is load-bearing, and it must extend to capture.

### The binary problem — now with a screenshot of it

`permissions.py` already says the true thing: *"macOS attaches privacy grants to
the host binary, not to this package."* **[verified]** the grant that appeared this
session attached to a subject macOS displays as **"Python"** — it showed up in a
notification reading *"'Python' Notifications"*, captured in the part-two
screenshot. The row a user will find in System Settings → Privacy & Security says
**Python**.

That is wrong for a shipped product in three ways: **granting Accessibility to
`python` grants it to everything you ever run under that interpreter**; the row is
uninterpretable; and recreating the venv can orphan it.

**[sourced]** It is worse than static misattribution. Tahoe evaluates the
**responsible process**, so a wrapper script that `exec`s a child re-targets TCC at
the child: OpenClaw issue #14138 — *"[macOS Tahoe] screencapture via exec tool
fails — TCC Screen Recording permission not inherited by Gateway LaunchAgent"* —
where `screencapture` returns *"could not create image from display"* despite the
parent holding the grant, and the workaround is granting the specific interpreter
binary path. **If daa ever runs as a LaunchAgent, budget for this.**

### What packaging implies — the `cua-driver` pattern, minus `cua-driver`

**Ship a separate, code-signed `.app` with a stable bundle id whose only job is UI
I/O.** `daa-ui-helper.app` / `ai.daa.uihelper`, speaking a line-delimited JSON
protocol over a unix socket in `~/.daa/` (0700, matching the journal). Only the
helper holds Accessibility and Screen Recording; the Python process holds neither.
The security surface becomes *"daa's UI helper can control your Mac"* — a sentence a
user can evaluate.

**[verified]** from trycua's installer, they got the hard parts right, so copy the
checklist:

1. Stable bundle id, LaunchServices-registered, launched with `open -n -g -a` so the
   TCC dialog is **attributed to the helper**, not to the terminal.
2. **TCC stores the designated code-signing requirement, not just the bundle id.**
   When the identity changes (dev self-signed → Developer ID), the old grant
   silently stops applying and the checkbox keeps looking checked. Detect it
   (`codesign -d -r-` on the old bundle, `codesign --verify -R` on the new) and
   `tccutil reset Accessibility ai.daa.uihelper` **with an explanation**.
3. One helper per signing identity during development, or accept a re-grant per
   re-sign.
4. The helper never inherits the parent environment or API keys. Hermes sanitises at
   seven call sites; do it from day one.

**[sourced] — the macOS 27 constraint.** MDM/PPPC provisioning of **Accessibility is
deprecated in 26.2 and removed in 27.0**; the replacement
(`com.apple.configuration.app.settings` → `Privacy` → `PermissionDefaults`) is
declarative and **still prompts the user**, even on a supervised Mac. Screen
Recording never could be silently granted. **On macOS 27 there is no silent
provisioning path for Accessibility at all — every machine needs a human click.**
That kills any "just ship an MDM profile" answer and makes the onboarding flow below
the product, not a workaround.

This is the single biggest line item in the estimate, which is why §7 stages it. v1
grants to the dev binary and says so loudly — `permissions.describe()` already
prints `TCC subject: <path>`, which is exactly the right honesty.

### The onboarding flow

Add `daa grant [accessibility|screen-recording]` — explicit, user-initiated, never
reachable from `resolve()`:

1. **Say what is about to be granted, in words**, including the actual binary path
   from `_host_binary()`, and say plainly that this is a broad grant when the subject
   is a terminal or a bare interpreter. *"You're about to let anything you run under
   this Python control your Mac"* is the true sentence.
2. **Deep-link, because the prompt no longer blocks.** **[sourced]**
   `AXIsProcessTrustedWithOptions(kAXTrustedCheckOptionPrompt: true)` no longer
   shows a blocking modal on Sequoia/Tahoe — it returns immediately. The
   `SETTINGS_URL` map already in `permissions.py`
   (`x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility`)
   is therefore **mandatory**, not a convenience.
3. **Poll and confirm out loud.** `AXIsProcessTrusted()` every 500 ms for up to 60 s;
   say *"got it"* when it flips. **[verified]** the probe is cheap and never raises.
   **[inferred]** whether the flip is observable without a process restart — fall
   back to *"restart daa and I'll check again"* on timeout rather than assuming.
4. **The prompting form is called only from this command.** `permissions.py` already
   establishes the rule for probes; make it absolute, and extend it to capture (§1).
5. **Ask for less.** The element path needs **Accessibility only**. Screen Recording
   buys window titles and the optional preview — and re-prompts monthly, so treat it
   as a capability that comes and goes. Automation is only for `run_applescript`.
6. **Extend `daa doctor` with a per-app AX readiness probe** — for each running app:
   can we read `kAXWindowsAttribute`, how many elements does a depth-capped walk
   return, how long did it take, with and without `AXManualAccessibility`. That
   table is the ground truth for §1's coverage question and it is the *first* thing
   to run once the grant exists.

---

## 6. Integration sketch

Five tools, plus one that deliberately does not exist yet.

```python
# tools/uielements.py  — the AX walker. No tool; shared machinery.
#   walk(pid, *, max_depth=12, max_nodes=800, timeout_s=1.5) -> list[Element]
#   Element: role, role_description, title, value, help, identifier,
#            enabled, focused, position, size, window_title, app, pid,
#            actions: tuple[str, ...], token: str
#   - AXUIElementSetMessagingTimeout(el, 0.25) per app element     [verified callable]
#   - AXUIElementCopyMultipleAttributeValues to batch node reads   [verified present]
#   - sets AXManualAccessibility on Chromium-family bundle ids;
#     NEVER sets AXEnhancedUserInterface (window-resize hazard, §1)
#   - AXSecureTextField values are never read, only their presence
#   - token = sha256(snapshot_id, path-in-tree, role, title)[:12]
#   - distinguishes -25211 (no grant) from -25205 (no such attribute)
#     from an empty tree: three different spoken remedies

ui_describe = spec(
    "ui_describe",
    "List the named, actionable elements of an app's frontmost window.",
    {"app": param("string", "Application name", required=True),
     "window": param("string", "Restrict to one window title"),
     "kind": param("string", "buttons | fields | menus | all")},
    floor=RiskTier.ANNOUNCE,      # NOT SILENT: this reads screen content
    tags=("ui", "accessibility", "read"),
    inverses=(),
)

ui_click = spec(
    "ui_click",
    "Press one named element — a button, checkbox, menu item or link.",
    {"target": param("string", "What to press, in words", required=True),
     "app": param("string", "Which application", required=True),
     "window": param("string", "Which window")},
    floor=RiskTier.CONFIRM_VOICE,
    tags=("ui", "accessibility", "irreversible"),
    inverses=(),
)

ui_type = spec(
    "ui_type",
    "Type literal text into one named text field.",
    {"text": param("string", "Exactly what to type", required=True),
     "target": param("string", "Which field", required=True),
     "app": param("string", "Which application", required=True)},
    floor=RiskTier.CONFIRM_VOICE,    # the text itself goes in consequences
    tags=("ui", "accessibility", "irreversible"),
    inverses=(),
)

ui_key = spec(
    "ui_key",
    "Press a named key combination in one application.",
    {"keys": param("string", "e.g. 'command s'", required=True),
     "app": param("string", "Which application", required=True)},
    floor=RiskTier.CONFIRM_VOICE,
    tags=("ui", "accessibility", "irreversible"),
    inverses=(),
)

ui_sequence = spec(
    "ui_sequence",
    "Perform a short, bound chain of UI steps in one application.",
    {"app": param("string", "Which application", required=True),
     "steps": param("array", "Ordered steps; each is a click, type or key",
                    required=True, maxItems=8)},
    floor=RiskTier.CONFIRM_VISUAL,   # a plan is a program; cf. run_applescript
    tags=("ui", "accessibility", "irreversible", "escape-hatch"),
    inverses=(),
)

# ui_click_point — NOT REGISTERED in v1. If ever added, it resolves via
# AXUIElementCopyElementAtPosition and REFUSES when the hit-test names
# nothing (§3.4), at floor=CONFIRM_VISUAL.
```

**Floor justification, against the existing calibration** (`move_to_trash`,
`move_files`, `run_shortcut` = `CONFIRM_VOICE`; `run_applescript` =
`CONFIRM_VISUAL`; `focus_window` = `ANNOUNCE`; `list_windows`, `spotlight_search` =
`SILENT`):

- `ui_describe` is **ANNOUNCE, not SILENT**, deliberately. `list_windows` at SILENT
  returns titles; an AX walk returns *contents* — the text of your open email, the
  rows of your password manager. Reading the screen should be a thing the assistant
  says it did. Its audit payload must carry counts and roles only, never values, in
  the spirit of `_shape()` in `voice/loop.py`.
- `ui_click` / `ui_type` / `ui_key` at **CONFIRM_VOICE** because a single named press
  is comparable in blast radius to `move_to_trash` — bounded, named, fully
  describable in one sentence. `consequences` (destructive wording, alert membership,
  system dialog, focus change) push individual invocations up to `CONFIRM_VISUAL`
  through the ordinary policy path, which is what the one-way rule is for.
- `ui_sequence` at **CONFIRM_VISUAL** because the target is a program.

### Two implementation details the abandoned libraries will cost you if you forget

**[sourced]** Both are unmerged pyautogui PRs, i.e. bugs that have bitten everyone:

1. **Set `kCGMouseEventClickState`.** Posting two single-clicks is not a
   double-click; native targets ignore it (pyautogui PR #949, cliclick issue #194 —
   two independent projects, same bug).
2. **Type with `CGEventKeyboardSetUnicodeString`, not virtual key codes.** Posting
   key codes types garbage on any non-US layout — a Russian layout turns `test` into
   `еуіе` (pyautogui PR #947, open since 2017). For daa this is not cosmetic: the
   readback promises *"type 'quarterly notes'"* and the machine must type exactly
   that. **A layout-dependent typing path makes the confirmation a lie.**

### A gap this design creates that nothing else in the repo has

**`resolve()` now performs a screen read, and `resolve()` runs at no tier.** The
contract says *"always safe to call"*, which has meant "mutates nothing" — and an AX
walk mutates nothing. But this is the first resolver whose side effect is *reading
the user's screen*, and it runs before any gate. Two consequences:

1. **It must be fast and bounded.** Electron trees run to 500–1000+ nodes and AX
   calls are synchronous IPC. Arm `AXUIElementSetMessagingTimeout` (**[verified]**
   callable at 0.25 s even with the grant denied), cap depth and node count, walk
   lazily, and stop as soon as the match is unambiguous. A resolver that blocks the
   voice loop for three seconds is a broken resolver.
2. **What it reads must not leak.** Only the chosen target, its role, its window and
   its app reach the `ResolvedAction`, the audit sink or the LLM context. Rejected
   candidates and the rest of the tree are discarded. `ui_describe` returning
   contents is a *tool result* the user was told about; `ui_click.resolve()` reading
   the same tree is not.

### Reuse

`windows.py` already does the degrade-don't-fail dance for these two grants — *"a
revoked grant costs a capability, never the process"* — and `Degraded` produces a
spoken remedy. Every tool above returns `Degraded(ACCESSIBILITY, …)` rather than
raising. That path is already tested.

### The strategic footnote: the best UI action is the one you don't take

**[sourced]** Apple is steering away from UI automation, not towards it. macOS 26
lets Spotlight invoke third-party App Intents; macOS 27 deprecates SiriKit in favour
of App Intents and adds App Intent Schemas, Entity Schemas, a View Annotations API
and an App Intents Testing Framework that validates integration *"through real
system pathways **without UI automation**"*. daa already has the right tool for
that world — `run_shortcut`, at `CONFIRM_VOICE`, whose readback names the shortcut
and its input. **The router should prefer `run_shortcut` over `ui_*` whenever a
shortcut exists**, and every `ui_*` `activation_hint` should say *last resort, only
when no other tool fits* — the same framing `run_applescript` uses. That is both the
safer path and the one Apple is investing in.

---

## 7. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **The core capability is unverified.** Accessibility is still denied, so `ui_*` has the same status the README gives `focus_window`: degradation tested, success path never run. | High | Stage 0 is nothing but granting and measuring. Do not write tool code before the coverage table exists. |
| **AX coverage is worse than hoped** — Electron flags do not take, Warp-class apps have no tree. | High | This is why the rule is *refuse*, not *fall back*. "I can't see anything I can name in Warp" is a correct product. Measure before committing scope. |
| **`AXEnhancedUserInterface` breaks window management.** | Medium | Never set it. Set `AXManualAccessibility`. If another app set it and we are about to move a window, disable and restore. |
| **AX walks are slow enough to break the voice loop.** | Medium | Messaging timeouts, depth/node caps, lazy matching, a hard resolver budget with a spoken "that took too long". |
| **`resolve()` reads the screen with no gate.** (§6) | Medium | Bound it, discard everything but the chosen target, keep values out of the audit sink. |
| **A capture or probe prompts and changes what it measures.** Observed this session. | Medium | Preflight before capture; prompting forms only from `daa grant`. |
| **Screen Recording re-consents monthly.** | Medium | Treat the preview as a capability that comes and goes; never make a confirmation *depend* on it. |
| **Destructive-wording lexicon is English-only and title-based.** A button labelled "Continue" that deletes an account reads back as safe. | Medium | Accept and be honest: the readback is *"press Continue in Delete Account in Safari"* — the **window title** carries the context the button title lost, which is why the window is part of the target string rather than decoration. |
| **`CGWindowListCreateImage` is obsoleted in the 15+ SDK.** | Low now, certain later | **[verified]** still functions at runtime on 26.5.2, but migrate to `pyobjc-framework-ScreenCaptureKit` behind a seam. |
| **Signing identity changes silently invalidate TCC grants; macOS 27 removes MDM provisioning of Accessibility.** | Medium (v2) | Implement the `codesign -d -r-` / `tccutil reset` check; design onboarding for a human click per machine, permanently. |
| **A UI action is genuinely irreversible** and the user says yes to the wrong thing. | High, inherent | What `CONFIRM_VOICE`+, the tree-derived readback, the destructive lexicon, alert detection and the explicit *"I can't undo this"* consequence are all buying. Cannot be eliminated, only made honest. |
| **The model routes around a safer tool** — clicking through Finder instead of calling `move_to_trash`. | Medium | *Last resort* framing in `activation_hint`; prefer `run_shortcut`; evaluate the router for this specific regression. |

---

## 8. Build order and effort

One engineer familiar with this codebase. Assumes the existing discipline (666
tests, offline fakes, no permissions needed in CI).

**Stage 0 — Measure, before writing any tool. ~1 day.**
Grant Accessibility to the dev binary. Throwaway script (not in `src/`) that walks
every running app and prints element count, depth, wall time, fraction of elements
with a non-empty title, fraction exposing `AXPress` — with and without
`AXManualAccessibility` on the Chromium family. **This table decides the scope of
everything below**, and right now nobody — not this document — knows what it says.

**Stage 1 — `tools/uielements.py`, the walker. ~3 days.**
Pure pyobjc, no tool registration. Depth/node caps, messaging timeouts, secure-field
suppression, stable tokens, three-way error discrimination, `Degraded` on a missing
grant. Tested against a recorded fixture tree so CI needs no permissions — the trick
`test_undo_coverage` already uses with `_trash_via_appkit`.

**Stage 2 — `ui_describe` + `ui_click`. ~4 days.**
The §3.2 readback machinery is most of this: target composition, destructive
lexicon, alert / default-button / system-dialog detection, disabled-element refusal,
ambiguity through `rank_candidates`. `AXPress` first; CGEvent at the element centre
(with `kCGMouseEventClickState`) only when `kAXPressAction` is absent. Tests
asserting a `"Delete Account"` button never reads back as anything the model called
it.

**Stage 3 — `ui_type` + `ui_key`. ~2 days.**
`CGEventKeyboardSetUnicodeString` for text. Literal text into `consequences` (the
`run_shortcut` precedent). Secure-field refusal. The key canonicaliser that splits on
`+` **and** `-`, plus the hard deny-list, both from hermes.

**Stage 4 — `daa grant` + `doctor` coverage table. ~2 days.**
The §5 flow. The per-app AX readiness table becomes a shipped diagnostic rather than
a throwaway script.

**Stage 5 — `ui_sequence`. ~4 days.**
Bound/deferred typing, the no-deferred-clicks rule, per-step re-verification against
the token, stop-on-divergence. The hardest correctness work; do not attempt before
2–3 are live on a real machine.

**Stage 6 — `daa-ui-helper.app`. ~1–2 weeks.**
Signed bundle, socket protocol, LaunchServices attribution, designated-requirement
check, env sanitisation. Independent of 1–5 — it replaces the transport under them
without changing a `ToolSpec`.

**Deliberately not scheduled:** `ui_click_point`, any vision model, any `cua-driver`
integration. Each gets a seam, none gets a sprint. Revisit `ui_click_point` only if
the Stage 0 table shows a material fraction of the user's *actual* apps are AX-blind
*and* worth automating — and then build it behind the §3.4 hit-test, never without.

**Total to a usable AX-first computer-use capability: ~3 weeks**, plus 1–2 weeks for
correct packaging, which can land later.

---

## Appendix: verified vs sourced vs inferred

**[verified] — run on this machine, 2026-09-21:**

- macOS 26.5.2 Tahoe / Darwin 25.5.0 / arm64 / build 25F84; Python 3.12.13.
- pyobjc-core, -Cocoa, -Quartz, -ApplicationServices, -CoreText all **12.2.2**
  (PyPI 2026-08-11, MIT). Every AX and CGEvent symbol in §1 present; **none missing**.
- All AX calls return `-25211` with the grant denied; `AXIsProcessTrusted() == False`.
- `AXUIElementSetMessagingTimeout` returns `0` **without** the grant.
- `AXUIElementSetAttributeValue(el, "AXManualAccessibility", True)` callable with a
  raw attribute-name string (reaches TCC, returns `-25211`).
- `AXUIElementCopyElementAtPosition` present, correct signature, `(-25211, None)`.
- **The two-part screenshot experiment.** Denied: `CGWindowListCreateImage` returned
  a 3456×2234 / 8.6 MB PNG in 29 ms containing wallpaper + live menu bar with all 16
  apps' window content removed. Granted (same interpreter, 20 min later, after the
  first call provoked consent): the same call returned the real screen in 25 ms. I
  saved and looked at both.
- The granted screenshot contains a live TCC consent dialog (*"'Warp' is requesting
  to bypass the system private window picker…" [Allow] [Open System Settings]*) and a
  notification headed *"Python" Notifications* — the TCC subject the grant attached to.
- `pyobjc-framework-ScreenCaptureKit` 12.2.2 installs; `SCScreenshotManager`,
  `SCShareableContent`, `SCContentFilter`, `SCStreamConfiguration` present.
- `atomacos` **3.3.0 / 2021-05-24 / GPL-2.0**; installs on 3.12/26, pulling nine
  transitive packages including `pyautogui` 0.9.41.
- `pyautogui` 0.9.54 (2023-05-24, BSD); `macos-use` 0.2.0 (2026-05-13, MIT) with the
  dependency list in §2; `cua-agent` 0.8.4 / `cua-computer` 0.5.19 / `cua-core` 0.3.1
  with `litellm==1.86.2` and `posthog>=3.20`.
- `cliclick` **5.1**, BSD-3, bottled `arm64_tahoe`, not installed here.
- `hermes-agent` is **MIT, © 2025 Nous Research**. It contains **no** Quartz / AX /
  pyobjc / cliclick / osascript / screencapture code. `_summarize_action`
  (`tool.py:565-587`) renders `"click element #7"`; `_request_approval`
  (`tool.py:533-536`) defaults to allow with no callback; `cua_backend.py:414-415`
  sets `CUA_DRIVER_PERMISSION_MODE=unrestricted` and
  `CUA_DRIVER_DANGEROUSLY_BYPASS_APPROVALS=1`.
- `cua-driver`'s `install.sh` (122 lines) and `_install-rust.sh` (1,421 lines), read
  in full: `/Applications/CuaDriver.app`, `com.trycua.driver`,
  `aarch64-apple-darwin` prebuilts, the `codesign -d -r-` / `codesign --verify -R` /
  `tccutil reset` logic, GitHub-releases distribution.
- 16 apps running with `activationPolicy == 0`, listed in §1.

**[sourced] — primary docs, SDK headers, PyPI/GitHub metadata or bug trackers read
this session (not executed here):**

- macOS 27 "Golden Gate" 27.0, released 2026-09-14, Apple Silicon only.
- `AXUIElement` undeprecated on 26 and 27; no replacement announced.
- `SCREEN_CAPTURE_OBSOLETE(10.5, 14.0, 15.0)` on `CGWindowListCreateImage` in the
  26.5 SDK header; cliclick's resulting build failure; fix merged 2025-06-20,
  unreleased.
- Chromium builds no AX tree without a detected client; `AXManualAccessibility` is
  Electron's documented opt-in; the `-25205` non-advertisement bug fixed in
  electron#38102; `AXEnhancedUserInterface`'s window-animation hazard (Rectangle
  #912, Phoenix PR #310, bugzilla 1664992, crbug 40865608).
- pyautogui PR #949 (`kCGMouseEventClickState`), PR #947 (unicode typing),
  `pyscreeze` Pillow markers stopping at 3.11.
- atomacos repo archived 2024-02-29; all three forks dead. PyXA's open
  "support recent Python & macOS" issues.
- Anthropic `claude-quickstarts/computer-use-best-practices` targets macOS, uses
  pyautogui + `sandbox-exec`.
- `terminator` is Windows-only per its own README; `screenpipe` is source-available,
  not OSS; OmniParser lacks out-of-box Apple Silicon support and has AGPL detectors.
- OpenAdapt 1.16.0 (2026-08-26) depends on pyobjc frameworks directly.
- Screen Recording monthly re-consent carried from Sequoia into Tahoe.
- `AXIsProcessTrustedWithOptions(prompt:true)` no longer blocks; deep-link required.
- MDM/PPPC Accessibility deprecated 26.2, **removed 27.0**; the declarative
  replacement still prompts. Screen Recording continues via legacy PPPC.
- OpenClaw issue #14138 — Tahoe responsible-process attribution breaking a
  LaunchAgent-spawned child's Screen Recording grant.
- Apple's App Intents direction (macOS 27 Schemas, View Annotations API, testing
  framework "without UI automation"); Xcode 26.3's MCP server is scoped to Xcode.

**[inferred] — reasoning, not executed or sourced:**

- That `AXPress` does not move the cursor or steal focus, and is available on most
  native controls. Untestable with the grant denied.
- Per-class AX coverage proportions on this machine. **Stage 0 exists to replace
  this with measurement.**
- Whether an Accessibility grant becomes visible to a running process without a
  restart.
- That `CGEventPostToPid` is unreliable for many apps (it exists; the reliability
  claim is reputational).
