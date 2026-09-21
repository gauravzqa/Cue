import AppKit
import SwiftUI
import DaaDockCore

/// The dock: an `LSUIElement` app that owns a status item, two panels, a
/// Carbon hotkey, the microphone and every pixel — and spawns the existing
/// Python as an ordinary child over newline-delimited JSON.
///
/// The Swift side holds **no policy**. There is no tier here, no threshold, no
/// allowlist, no "this one looks fine". Everything that decides whether an
/// action may run lives in Python under its existing test suite, and the dock
/// renders `state` / `audit` / `confirm.request` and forwards clicks.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var model: AppModel!
    private var statusItem: StatusItemController!
    private var dockPanel: DockPanel!
    private var approvals: ApprovalWindowController!
    private var hotkey = HotkeyManager()
    private var escape = EscapeMonitor()
    private var speech = SpeechEngine()
    private var dockVisible = false
    private var outsideClickMonitor: Any?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)

        model = AppModel(repoRoot: Self.repoRoot())
        // Before anything can ask for a permission: tell the user whether the
        // permissions they are about to grant will survive the next build.
        model.checkCodeIdentity()

        statusItem = StatusItemController()
        statusItem.onLeftClick = { [weak self] in self?.toggleDock() }
        statusItem.onRightClick = { [weak self] in self?.showMenu() }

        dockPanel = DockPanel(view: DockView().environment(model))
        approvals = ApprovalWindowController(model: model)

        model.setApprovalObserver { [weak self] gate in
            guard let self else { return }
            if let gate, gate.resolution == nil {
                if !self.approvals.isOpen { self.approvals.show() }
            } else if self.approvals.isOpen {
                self.approvals.hide()
            }
            self.refreshStatusItem()
        }

        wireHotkey()
        wireSpeech()

        escape.onEscape = { [weak self] in self?.model.cancelTurn() }
        escape.start()

        model.start()
        startStatusRefresh()
    }

    func applicationWillTerminate(_ notification: Notification) {
        // A card on screen at quit is a refusal. Fail closed.
        model.stop()
        hotkey.unregister()
        escape.stop()
        Task { await speech.stop() }
    }

    // MARK: - the dock panel

    private func toggleDock() {
        dockVisible ? hideDock() : showDock()
    }

    private func showDock() {
        dockPanel.position(under: statusItem.buttonFrameOnScreen)
        // orderFront, never activate: the frontmost app keeps focus, because
        // it is usually the app daa is about to act on.
        dockPanel.orderFrontRegardless()
        dockVisible = true
        outsideClickMonitor = NSEvent.addGlobalMonitorForEvents(
            matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
            Task { @MainActor in self?.hideDock() }
        }
    }

    private func hideDock() {
        dockPanel.orderOut(nil)
        dockVisible = false
        if let m = outsideClickMonitor { NSEvent.removeMonitor(m) }
        outsideClickMonitor = nil
    }

    private func showMenu() {
        let menu = NSMenu()
        menu.addItem(withTitle: "History and set-up…", action: #selector(openHistory), keyEquivalent: "")
            .target = self
        menu.addItem(withTitle: "Open log", action: #selector(openLog), keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit daa", action: #selector(quit), keyEquivalent: "q").target = self
        // A one-shot popup rather than `statusItem.menu`, which would steal
        // the left click that opens the dock.
        menu.popUp(positioning: nil,
                   at: NSPoint(x: 0, y: statusItem.buttonFrameOnScreen?.minY ?? 0),
                   in: nil)
    }

    @objc private func openHistory() { HistoryWindowController.shared.show(model: model) }
    @objc private func openLog() {
        NSWorkspace.shared.selectFile(model.supervisor.logPath, inFileViewerRootedAtPath: "")
    }
    @objc private func quit() { NSApp.terminate(nil) }

    // MARK: - hotkey

    private func wireHotkey() {
        hotkey.onGesture = { [weak self] gesture in
            guard let self else { return }
            switch gesture {
            case .pressStart, .latchOn:
                // Holding the hotkey IS an unambiguous act of address, so the
                // utterance is marked `addressed` and skips the gate — and a
                // live partial is the user's own words, shown back to them.
                self.model.dock.pushToTalkHeld = true
                self.model.dock.phase = .listening
                Task { await self.speech.start() }
            case .pushToTalkEnd, .latchOff:
                self.model.dock.pushToTalkHeld = false
                self.speech.endUtteranceFromRelease()
                if !self.model.dock.alwaysOn { Task { await self.speech.stop() } }
            }
            self.refreshStatusItem()
        }
        hotkey.register()
        if let err = hotkey.registrationError {
            model.supervisor.note("hotkey: \(err)")
            model.transcript.append(TranscriptLine(
                speaker: .system,
                text: "Push-to-talk is not available: \(err). You can still type.",
                refused: true))
        }
    }

    // MARK: - speech

    private func wireSpeech() {
        speech.onLevel = { [weak self] level in
            self?.model.dock.micLevel = level
            self?.refreshStatusItem()
        }
        speech.onPartial = { [weak self] text in
            self?.model.dock.partial = text
        }
        speech.onSpeechOnset = { [weak self] in self?.model.sendOnset() }
        speech.onUtterance = { [weak self] text, confidence, complete, startedAt in
            guard let self else { return }
            self.model.sendUtterance(text, confidence: confidence, complete: complete,
                                     addressed: self.model.dock.pushToTalkHeld || self.hotkey.isLatched,
                                     startedAt: startedAt)
            self.model.dock.partial = ""
        }
        speech.onModelDownload = { [weak self] progress in
            self?.model.transcript.append(TranscriptLine(
                speaker: .system,
                text: progress >= 1 ? "Speech model installed."
                                    : "Downloading the on-device speech model…"))
        }
        speech.onFailure = { [weak self] failure in
            guard let self else { return }
            let message: String
            switch failure {
            case .micDenied:
                message = """
                    daa cannot use the microphone. Grant it in System Settings › Privacy \
                    & Security › Microphone. If this app is ad-hoc signed, that grant will \
                    be forgotten on the next rebuild.
                    """
            case .micRestricted:
                message = "The microphone is restricted by a policy on this Mac."
            case .modelUnavailable(let why):
                message = "The on-device speech model is not available: \(why)"
            case .engineFailed(let why):
                message = "The audio engine failed: \(why)"
            case .noLocale:
                message = "No supported speech locale is installed for your language."
            }
            self.model.supervisor.note("speech: \(message)")
            self.model.transcript.append(TranscriptLine(speaker: .system, text: message, refused: true))
        }
    }

    // MARK: - status item

    private func startStatusRefresh() {
        let t = Timer(timeInterval: 0.25, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refreshStatusItem() }
        }
        RunLoop.main.add(t, forMode: .common)
    }

    private func refreshStatusItem() {
        statusItem.update(model.dock.menuBar, level: model.dock.micLevel)
    }

    // MARK: - where is the repo

    /// Stages 1–3 point at the developer's own checkout. `DAA_REPO_ROOT` wins;
    /// otherwise walk up from the bundle looking for a `.venv` next to `src/daa`.
    static func repoRoot() -> String? {
        let env = ProcessInfo.processInfo.environment
        if let explicit = env["DAA_REPO_ROOT"], !explicit.isEmpty { return explicit }
        var dir = Bundle.main.bundleURL
        for _ in 0..<8 {
            dir = dir.deletingLastPathComponent()
            let marker = dir.appendingPathComponent("src/daa/cli.py")
            if FileManager.default.fileExists(atPath: marker.path) { return dir.path }
        }
        let home = NSHomeDirectory() + "/daa"
        return FileManager.default.fileExists(atPath: home + "/src/daa/cli.py") ? home : nil
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
