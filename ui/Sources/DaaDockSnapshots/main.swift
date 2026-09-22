import AppKit
import SwiftUI
import DaaDockCore
@testable import DaaDockUI

// `make snapshots` -- every screen of the dock, rendered offscreen to PNG, in
// light and dark. See Renderer.swift for why this needs no permission at all.
//
//   swift run daadock-snapshots [output-dir] [--only <substring>]

struct Shot {
    var name: String
    var group: String
    var note: String
    var scroll: Renderer.Scroll = .top
    var backend: Renderer.Backend = .hosting
    var view: @MainActor () -> AnyView
}

@MainActor
enum Scenes {

    // MARK: - the approval card

    static let presentedAt = Fixtures.t0
    /// Well past the 400 ms inert fade, 1:25 left on the clock.
    static let settled = presentedAt + 5

    static func card(_ payload: [String: Any], id: String = "cf_5a1c9e02",
                     seen: Bool = false, now: Double = settled,
                     holdFor: Double? = nil, axSize: DynamicTypeSize? = nil) -> AnyView {
        let c = Fixtures.admittedCard(payload, id: id)
        var gate = ApprovalGate(card: c, presentedAt: presentedAt)
        if seen { gate = gate.scrolledToEnd() }
        if let holdFor { gate = gate.beginningHold(at: now - holdFor) }
        let model = AppModel(repoRoot: nil)
        let v = ApprovalCardView(gate: gate, now: now).environment(model)
        if let axSize { return AnyView(v.dynamicTypeSize(axSize)) }
        return AnyView(v)
    }

    static var cardShots: [Shot] {
        let g = "Approval card"
        return [
            Shot(name: "card-script", group: g,
                 note: "fake-bridge `script`: run_applescript, THIS RUNS block, dry run, inferred, one ⚑ consequence. Settled (1:25 left).") {
                card(Fixtures.fakeBridgeCard("script"), seen: true)
            },
            Shot(name: "card-long-top", group: g,
                 note: "fake-bridge `long`: 400-line script, as it first appears. Scroll gate closed: Approve disabled, 'Scroll to the end' hint.") {
                card(Fixtures.fakeBridgeCard("long"), seen: false)
            },
            Shot(name: "card-long-bottom", group: g, note: "fake-bridge `long`, scrolled to the bottom. Gate open: Approve enabled.",
                 scroll: .bottom) {
                card(Fixtures.fakeBridgeCard("long"), seen: true)
            },
            Shot(name: "card-synthetic", group: g,
                 note: "fake-bridge `synthetic`: the 'daa could not get a real judgment' disclosure.") {
                card(Fixtures.fakeBridgeCard("synthetic"), seen: true)
            },
            Shot(name: "card-live", group: g,
                 note: "fake-bridge `live`: dryRun false. The red \"This will really happen.\" banner and the red, heavier \"Approve for real · hold\" control. Compare card-script, the dry run of the same payload.") {
                card(Fixtures.fakeBridgeCard("live"), seen: true)
            },
            Shot(name: "card-destructive", group: g,
                 note: "variant: four ⚑ destructive consequences, live, a script that deletes, 2 targets, 2 extra args.") {
                card(Fixtures.destructiveCard(), seen: true)
            },
            Shot(name: "card-explicit", group: g,
                 note: "variant: explicit=true, so no 'I inferred this' flag. Compare with card-script.") {
                card(Fixtures.explicitCard(), seen: true)
            },
            Shot(name: "card-inert", group: g,
                 note: "fake-bridge `script`, 150 ms after it appeared: inside the 400 ms inert window (faded to 55%, Approve disabled).") {
                card(Fixtures.fakeBridgeCard("script"), seen: true, now: presentedAt + 0.15)
            },
            Shot(name: "card-hold-mid", group: g,
                 note: "fake-bridge `script`, Approve held for 300 of 600 ms: fill at 50%, 'Keep holding…'.") {
                card(Fixtures.fakeBridgeCard("script"), seen: true, holdFor: 0.3)
            },
            Shot(name: "card-expiring", group: g,
                 note: "fake-bridge `script` with 7 s left: countdown turns orange under 15 s.") {
                card(Fixtures.fakeBridgeCard("script"), seen: true, now: presentedAt + 83)
            },
        ]
    }

    /// A 1024×665 pt screen with a 24 pt menu bar, and the card placed on it
    /// exactly where `ApprovalPanel.centreOnActiveScreen` would put it.
    static func largerTextDisplay(_ payload: [String: Any], seen: Bool) -> AnyView {
        let screen = CGSize(width: 1024, height: 665)
        let menuBar: CGFloat = 24
        // AppKit coordinates, origin bottom-left: visibleFrame stops under the menu bar.
        let visible = CGRect(x: 0, y: 0, width: screen.width, height: screen.height - menuBar)
        let wanted = NSHostingView(rootView: card(payload)).fittingSize
        let f = PanelPlacement.clampedFrame(content: wanted, visible: visible)
        return AnyView(ZStack(alignment: .topLeading) {
            Color(white: 0.45)
            VStack(spacing: 0) {
                Color(white: 0.92).frame(height: menuBar)
                Spacer(minLength: 0)
            }
            card(payload, seen: seen)
                .frame(width: f.width, height: f.height)
                .offset(x: f.minX, y: screen.height - f.maxY)
        }
        .frame(width: screen.width, height: screen.height, alignment: .topLeading)
        .clipped())
    }

    static var axShots: [Shot] {
        let g = "Approval card at the largest accessibility text size (.accessibility5)"
        return [
            Shot(name: "card-script-ax5", group: g, note: "card-script with dynamicTypeSize = .accessibility5.") {
                card(Fixtures.fakeBridgeCard("script"), seen: true, axSize: .accessibility5)
            },
            Shot(name: "card-destructive-ax5", group: g, note: "card-destructive with dynamicTypeSize = .accessibility5.") {
                card(Fixtures.destructiveCard(), seen: true, axSize: .accessibility5)
            },
            Shot(name: "card-long-bottom-ax5", group: g, note: "card-long-bottom with dynamicTypeSize = .accessibility5.",
                 scroll: .bottom) {
                card(Fixtures.fakeBridgeCard("long"), seen: true, axSize: .accessibility5)
            },
            Shot(name: "card-long-on-larger-text-display", group: g,
                 note: "SIMULATION of the real placement: macOS users who need large text mostly use Display › \"Larger Text\" (a scaled resolution) because SwiftUI on macOS ignores dynamicTypeSize. On a 13-inch MacBook Air that is 1024×665 pt, visibleFrame 1024×641 under a 24 pt menu bar. The long card wants 720 pt. The frame is the one ApprovalPanel.centreOnActiveScreen computes -- PanelPlacement.clampedFrame(content:visible:) -- and the card is drawn at that height, so the scroll region shrinks and header, pinned claim and footer stay on screen. Grey = desktop, strip = menu bar.") {
                largerTextDisplay(Fixtures.fakeBridgeCard("long"), seen: false)
            },
            Shot(name: "card-long-on-larger-text-display-bottom", group: g,
                 note: "The same, scrolled to the end of the script: the gate is open, and the claim (\"run a script that sends a message to Alex\" / 'on my way') is still on screen next to `echo step 400`.",
                 scroll: .bottom) {
                largerTextDisplay(Fixtures.fakeBridgeCard("long"), seen: true)
            },
            Shot(name: "card-destructive-on-larger-text-display", group: g,
                 note: "The live destructive card (four flags pinned) in the same clamped 1024×665 placement.") {
                largerTextDisplay(Fixtures.destructiveCard(), seen: true)
            },
            Shot(name: "panel-transcript-ax5", group: g, note: "The dock panel with the transcript mix at .accessibility5.",
                 scroll: .bottom) {
                AnyView(DockView().environment(panelModel(transcript: true)).dynamicTypeSize(.accessibility5))
            },
        ]
    }

    // MARK: - the dock panel

    /// A model driven through the app's own frame handler, the path every
    /// frame from Python takes.
    static func panelModel(_ frames: [Frame] = [], transcript: Bool = false,
                           configure: (AppModel) -> Void = { _ in }) -> AppModel {
        let m = AppModel(repoRoot: nil)
        let feed: (Frame) -> Void = { m.supervisor.onFrame?($0) }
        feed(Fixtures.ready)
        if transcript { Fixtures.transcriptMix.forEach(feed) }
        frames.forEach(feed)
        configure(m)
        return m
    }

    static func panel(_ m: AppModel) -> AnyView { AnyView(DockView().environment(m)) }

    static var panelShots: [Shot] {
        let g = "Dock panel — every phase"
        return [
            Shot(name: "panel-idle", group: g, note: "fake-bridge `ready` then `state idle`. Empty transcript.") {
                panel(panelModel([Fixtures.state("idle")]))
            },
            Shot(name: "panel-listening-ptt", group: g,
                 note: "listening under push-to-talk: live partial shown (the user already addressed daa by holding ⌥Space). Set the way AppDelegate sets it on hotkey-down.") {
                panel(panelModel(transcript: true) { m in
                    m.dock.pushToTalkHeld = true; m.dock.phase = .listening
                    m.dock.micLevel = 0.7; m.dock.partial = "move those screenshots to"
                })
            },
            Shot(name: "panel-listening-alwayson", group: g,
                 note: "listening, always on, not yet woken: level meter only, never text. The privacy boundary.") {
                panel(panelModel { m in
                    m.dock.alwaysOn = true; m.dock.phase = .listening; m.dock.micLevel = 0.45
                })
            },
            Shot(name: "panel-thinking", group: g, note: "fake-bridge `state thinking \"looking at what you said\"`.") {
                panel(panelModel([Fixtures.audit("woke", ["text": "move those screenshots to Archive"]),
                                  Fixtures.state("thinking", "looking at what you said")]))
            },
            Shot(name: "panel-answering", group: g,
                 note: "fake-bridge `speak \"Moved 3 files to Archive.\"` then `state idle`. daa's answer is a `daa` transcript line and nothing else — no spoken phase, no audio. The matching `spoke` audit record arrives too and is collapsed into the same line.",
                 scroll: .bottom) {
                panel(panelModel([Fixtures.audit("woke", ["text": "move those screenshots to Archive"]),
                                  Fixtures.speak("Moved 3 files to Archive."),
                                  Fixtures.audit("spoke", ["text": "Moved 3 files to Archive."]),
                                  Fixtures.state("idle")]))
            },
            Shot(name: "panel-awaiting", group: g, note: "fake-bridge `state awaiting \"waiting for approval\"`: amber header dot.") {
                panel(panelModel([Fixtures.audit("woke", ["text": "script"]),
                                  Fixtures.state("awaiting", "waiting for approval")]))
            },
            Shot(name: "panel-working", group: g, note: "fake-bridge `task`: one background job at 40%.") {
                panel(panelModel([Fixtures.task("t1a2b3c", "indexing the Downloads folder", progress: 0.4),
                                  Fixtures.state("working", "indexing",
                                                 tasks: [["id": "t1a2b3c", "title": "indexing the Downloads folder",
                                                          "progress": 0.4, "cancellable": true]])]))
            },
            Shot(name: "panel-degraded-restarting", group: g,
                 note: "The child exited (fake-bridge `crash`): supervisor status restarting in 2 s, attempt 2.") {
                panel(panelModel(transcript: true) { m in
                    m.supervisor.onStatus?(.restarting(inSeconds: 2, attempt: 2))
                })
            },
            Shot(name: "panel-degraded-gaveup", group: g, note: "The supervisor gave up restarting.") {
                panel(panelModel { m in m.supervisor.onStatus?(.gaveUp("exited 5 times in 60 s")) })
            },
            Shot(name: "panel-degraded-nopython", group: g, note: "No interpreter found at all.") {
                panel(panelModel { m in
                    m.supervisor.onStatus?(.noInterpreter(["$DAA_PYTHON", "~/daa/.venv/bin/python"]))
                })
            },
            Shot(name: "panel-live-mode", group: g,
                 note: "variant: `ready` with dryRun=false. No DRY RUN pill: this is what 'actions really run' looks like.") {
                panel(panelModel([Fixtures.ev("ready", ["daa": "0.1.0", "dryRun": false, "alwaysOn": true,
                                                         "jevLive": true, "providers": ["llm": "live"]]),
                                  Fixtures.state("idle")], transcript: true))
            },
        ]
    }

    static var partShots: [Shot] {
        let g = "Transcript, tasks and notices"
        return [
            Shot(name: "panel-transcript", group: g,
                 note: "Transcript mix: you / daa / dry-run (italic) / refusal (orange) / error / a live line with ↩︎ and the Undo button. Scrolled to the newest line, as the app does.",
                 scroll: .bottom) {
                panel(panelModel(transcript: true))
            },
            Shot(name: "panel-transcript-top", group: g, note: "The same transcript, scrolled to the top.") {
                panel(panelModel(transcript: true))
            },
            Shot(name: "panel-tasks-one", group: g, note: "Background-task strip, one task.") {
                panel(panelModel([Fixtures.task("t1", "indexing the Downloads folder", progress: 0.4),
                                  Fixtures.state("working", "indexing",
                                                 tasks: [["id": "t1", "title": "indexing the Downloads folder",
                                                          "progress": 0.4, "cancellable": true]])]))
            },
            Shot(name: "panel-tasks-several", group: g,
                 note: "Several tasks: determinate, indeterminate, not cancellable, a very long title, and one done.") {
                panel(panelModel([
                    Fixtures.task("t1", "indexing the Downloads folder", progress: 0.7),
                    Fixtures.task("t2", "transcribing “Standup 2026-09-21.m4a”", progress: nil),
                    Fixtures.task("t3", "summarising 42 unread messages from the #launch channel into one note", progress: 0.15),
                    Fixtures.task("t4", "backing up Notes", progress: 0.3, cancellable: false),
                    Fixtures.task("t5", "resizing 12 photos", progress: 1),
                    Fixtures.ev("task.done", ["id": "t5", "outcome": "resized 12 photos"]),
                ]))
            },
            Shot(name: "panel-cdhash-notice", group: g,
                 note: "'daa was rebuilt. macOS has forgotten its permissions' (rebuiltAdHoc), collapsed, at the top of the panel.") {
                panel(panelModel(transcript: true) { m in m.identityNotice = Fixtures.rebuiltAdHoc })
            },
            Shot(name: "cdhash-notice-expanded", group: g, note: "The same notice, expanded ('Why?').") {
                AnyView(IdentityBanner(notice: Fixtures.rebuiltAdHoc, expanded: true).frame(width: 340).background(.regularMaterial))
            },
            Shot(name: "cdhash-notice-firstrun", group: g, note: "First run of an ad-hoc build (the 'warning' severity), expanded.") {
                AnyView(IdentityBanner(notice: Fixtures.firstRunAdHoc, expanded: true).frame(width: 340).background(.regularMaterial))
            },
            Shot(name: "always-on-explainer", group: g, note: "The one-time sheet shown the first time Always on is switched on (jevLive false).") {
                AnyView(AlwaysOnExplainer().environment(panelModel()).background(windowBackground))
            },
        ]
    }

    /// What an ordinary NSWindow paints behind its content view. The offscreen
    /// window is clear, so views that rely on it get it explicitly.
    static var windowBackground: Color { Color(nsColor: .windowBackgroundColor) }

    // MARK: - history

    static var historyModel: AppModel {
        panelModel(transcript: true) { m in
            m.identityNotice = Fixtures.rebuiltAdHoc
            // variant: a synthetic judgment row, so the SYNTHETIC badge shows.
            m.supervisor.onFrame?(Fixtures.audit("disposition", at: 60,
                ["tool": "run_applescript", "tier": "CONFIRM_VISUAL", "synthetic": true,
                 "reason": "unrecoverable and not explicitly requested"]))
        }
    }

    static var historyShots: [Shot] {
        let g = "History window"
        return [
            Shot(name: "history", group: g, note: "History tab, 'Decisions only', driven by the same audit frames as the transcript.") {
                AnyView(HistoryView().environment(historyModel).frame(width: 760, height: 560).background(windowBackground))
            },
            Shot(name: "history-setup", group: g, note: "Set-up tab: code signature, providers (all fake), mode, missing, tools.") {
                AnyView(HistoryView(tab: .setup).environment(historyModel).frame(width: 760, height: 560).background(windowBackground))
            },
            Shot(name: "history-row-expanded", group: g, note: "One audit row expanded (a synthetic refusal).") {
                AnyView(AuditRow(record: AuditRecord(id: "a1", kind: "refused", at: Fixtures.t0 + 41, payload: [
                    "tool": .string("move_file"), "synthetic": .bool(true),
                    "reason": .string("daa could not get a real judgment, so it assumed the worst and did not act"),
                ]), expanded: true).padding(10).frame(width: 560).background(windowBackground))
            },
        ]
    }

    // MARK: - ImageRenderer, for the record

    static var imageRendererShots: [Shot] {
        let g = "ImageRenderer backend (for comparison — shows what it cannot draw)"
        return [
            Shot(name: "imagerenderer-card-script", group: g,
                 note: "card-script through SwiftUI ImageRenderer. The card body lives in a ScrollView, which ImageRenderer cannot draw.",
                 backend: .imageRenderer) { card(Fixtures.fakeBridgeCard("script"), seen: true) },
            Shot(name: "imagerenderer-panel-transcript", group: g,
                 note: "panel-transcript through ImageRenderer: transcript ScrollView, TextField and switch are placeholders.",
                 backend: .imageRenderer) { panel(panelModel(transcript: true)) },
        ]
    }

    static var all: [Shot] {
        cardShots + axShots + panelShots + partShots + historyShots + imageRendererShots
    }
}

// MARK: - the menu-bar glyph

@MainActor
enum MenuBarShots {
    struct State { var name: String; var note: String; var dock: DockState; var level: Double = 0 }

    static var states: [State] {
        func s(_ phase: Phase, alwaysOn: Bool = false, tasks: [TaskRow] = [], approval: Bool = false) -> DockState {
            var d = DockState(); d.phase = phase; d.alwaysOn = alwaysOn; d.tasks = tasks; d.approvalOpen = approval
            return d
        }
        return [
            State(name: "idle", note: "idle: still glyph, 55% opacity", dock: s(.idle)),
            State(name: "idle-alwayson", note: "idle, always on: 85% opacity + hot-mic ring", dock: s(.idle, alwaysOn: true)),
            State(name: "listening", note: "listening: live 3-bar level (frames at level 0.1, 0.4, 0.7, 1.0)", dock: s(.listening)),
            State(name: "thinking", note: "thinking: three dots, one lit in turn (4 animation phases). Not bars — thinking is not a microphone state and must not look like one", dock: s(.thinking)),
            State(name: "awaiting", note: "awaiting: amber — the only colour the icon ever takes", dock: s(.awaiting)),
            State(name: "awaiting-alwayson", note: "awaiting while always on", dock: s(.awaiting, alwaysOn: true)),
            State(name: "working", note: "working: rotating arc (4 animation phases)",
                  dock: s(.working, tasks: [TaskRow(id: "t", title: "indexing")])),
            State(name: "degraded", note: "degraded: slashed", dock: s(.degraded)),
        ]
    }

    /// Four frames side by side, each the 22×22 glyph on a menu-bar-coloured
    /// tile, drawn by the very function the status item uses.
    static func render(_ st: State, scheme: Renderer.Scheme) -> NSBitmapImageRep {
        let a = st.dock.menuBar
        let tile: CGFloat = 30, gap: CGFloat = 6, frames = 4
        let size = NSSize(width: CGFloat(frames) * tile + CGFloat(frames - 1) * gap, height: tile)
        return Renderer.draw(size: size, scale: 6, scheme: scheme) { rect in
            for i in 0..<frames {
                let x = CGFloat(i) * (tile + gap)
                let bg = NSRect(x: x, y: 0, width: tile, height: tile)
                (scheme == .light ? NSColor(white: 0.93, alpha: 1) : NSColor(white: 0.17, alpha: 1)).setFill()
                NSBezierPath(roundedRect: bg, xRadius: 5, yRadius: 5).fill()
                let glyph = NSRect(x: x + 4, y: 4, width: 22, height: 22)
                let level = [0.1, 0.4, 0.7, 1.0][i]
                let phase = Double(i) * 0.4
                // The status item applies `opacity` as the button's alphaValue.
                let ctx = NSGraphicsContext.current!.cgContext
                ctx.saveGState()
                ctx.setAlpha(a.opacity)
                ctx.beginTransparencyLayer(auxiliaryInfo: nil)
                NSGraphicsContext.saveGraphicsState()
                let t = NSAffineTransform(); t.translateX(by: glyph.minX, yBy: glyph.minY); t.concat()
                MenuBarGlyph.draw(a, in: NSRect(origin: .zero, size: glyph.size), level: level, phase: phase)
                NSGraphicsContext.restoreGraphicsState()
                ctx.endTransparencyLayer()
                ctx.restoreGState()
            }
        }
    }
}

// MARK: - run

struct Entry { var name: String; var group: String; var note: String; var files: [String: String] }

@MainActor
func run() throws {
    var args = Array(CommandLine.arguments.dropFirst())
    var only: String?
    if let i = args.firstIndex(of: "--only"), i + 1 < args.count { only = args[i + 1]; args.removeSubrange(i...i + 1) }
    let out = URL(fileURLWithPath: args.first ?? "build/snapshots", isDirectory: true)
    try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

    var entries: [Entry] = []
    var failures: [String] = []

    for shot in Scenes.all where only.map({ shot.name.contains($0) }) ?? true {
        var files: [String: String] = [:]
        for scheme in Renderer.Scheme.allCases {
            let file = "\(shot.name)-\(scheme.rawValue).png"
            guard let rep = Renderer.render(shot.view(), scheme: scheme, scroll: shot.scroll, backend: shot.backend) else {
                failures.append(file); continue
            }
            try Renderer.write(rep, to: out.appendingPathComponent(file))
            files[scheme.rawValue] = file
            print("  \(file)  \(rep.pixelsWide)×\(rep.pixelsHigh)")
        }
        entries.append(Entry(name: shot.name, group: shot.group, note: shot.note, files: files))
    }

    for st in MenuBarShots.states where only.map({ "menubar-\(st.name)".contains($0) }) ?? true {
        var files: [String: String] = [:]
        for scheme in Renderer.Scheme.allCases {
            let file = "menubar-\(st.name)-\(scheme.rawValue).png"
            let rep = MenuBarShots.render(st, scheme: scheme)
            try Renderer.write(rep, to: out.appendingPathComponent(file))
            files[scheme.rawValue] = file
            print("  \(file)")
        }
        let label = st.dock.menuBar.accessibilityLabel
        entries.append(Entry(name: "menubar-\(st.name)", group: "Menu-bar icon (22×22, drawn by MenuBarGlyph, shown at 6×)",
                             note: "\(st.note). VoiceOver: “\(label)”", files: files))
    }

    // A subset run must not replace the full index with a partial one.
    if only == nil { try IndexPage.write(entries, to: out.appendingPathComponent("index.html")) }
    print("\(entries.reduce(0) { $0 + $1.files.count }) images, index at \(out.appendingPathComponent("index.html").path)")
    if !failures.isEmpty {
        FileHandle.standardError.write(Data("failed: \(failures.joined(separator: ", "))\n".utf8))
        exit(1)
    }
}

let app = NSApplication.shared
// No Dock icon, no menu bar, no activation: this process never shows anything.
app.setActivationPolicy(.prohibited)
MainActor.assumeIsolated {
    do { try run() } catch {
        FileHandle.standardError.write(Data("snapshots: \(error)\n".utf8)); exit(1)
    }
}
