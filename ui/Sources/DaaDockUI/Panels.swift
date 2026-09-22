import AppKit
import SwiftUI
import DaaDockCore

/// The dock panel.
///
/// Non-activating on purpose: the frontmost app must never lose focus when the
/// dock appears, because that app is usually the one daa is about to act on —
/// and `focus_window`, the clipboard and AppleScript targeting all depend on
/// it still being frontmost.
///
/// `NSStatusItem` + `NSPanel` rather than `MenuBarExtra`: `MenuBarExtra` still
/// has no API to read or set its presentation state, reach the underlying
/// status item, or reach the popup's window, and `openSettings` from one is
/// broken on macOS 26.
final class DockPanel: NSPanel {
    init(view: some View) {
        super.init(
            contentRect: NSRect(x: 0, y: 0, width: 340, height: 460),
            styleMask: [.borderless, .nonactivatingPanel, .fullSizeContentView],
            backing: .buffered, defer: false)
        isFloatingPanel = true
        level = .statusBar
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        isOpaque = false
        backgroundColor = .clear
        hasShadow = true
        hidesOnDeactivate = false
        isMovable = false
        animationBehavior = .utilityWindow
        contentView = NSHostingView(rootView: view)
    }

    override var canBecomeKey: Bool { true }   // so the text field works
    override var canBecomeMain: Bool { false } // but the app behind stays main

    /// Anchored under the status item, kept on screen.
    func position(under statusItemFrame: NSRect?) {
        guard let screen = NSScreen.screens.first(where: {
            guard let f = statusItemFrame else { return false }
            return $0.frame.intersects(f)
        }) ?? NSScreen.main else { return }

        let size = contentView?.fittingSize ?? NSSize(width: 340, height: 460)
        setContentSize(size)

        let vis = screen.visibleFrame
        var x = (statusItemFrame?.midX ?? vis.midX) - size.width / 2
        x = min(max(vis.minX + 8, x), vis.maxX - size.width - 8)
        let y = (statusItemFrame?.minY ?? vis.maxY) - size.height - 6
        setFrameOrigin(NSPoint(x: x, y: max(vis.minY + 8, y)))
    }
}

/// The approval card's own panel.
///
/// Unlike the dock this one IS activating and sits at `.modalPanel`: a
/// decision that needs a human should not be something you can leave behind a
/// window by accident. It is a separate panel from the dock so a background
/// job can raise a card while the dock is closed.
final class ApprovalPanel: NSPanel {
    init(view: some View) {
        let host = NSHostingView(rootView: AnyView(view))
        super.init(
            contentRect: NSRect(x: 0, y: 0, width: 560, height: 480),
            styleMask: [.titled, .fullSizeContentView, .nonactivatingPanel],
            backing: .buffered, defer: false)
        titleVisibility = .hidden
        titlebarAppearsTransparent = true
        isMovableByWindowBackground = true
        level = .modalPanel
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        isOpaque = false
        backgroundColor = .clear
        hidesOnDeactivate = false
        // There is deliberately no close button. Esc cancels, the Cancel
        // button cancels, and the countdown cancels. A window-chrome X would
        // be a fourth way to dismiss the card that is indistinguishable from
        // a stray click.
        standardWindowButton(.closeButton)?.isHidden = true
        standardWindowButton(.miniaturizeButton)?.isHidden = true
        standardWindowButton(.zoomButton)?.isHidden = true
        contentView = host
    }

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }

    /// Sized to the card, then clamped to the screen's `visibleFrame` (see
    /// `PanelPlacement`), so on a small or "Larger Text" display the header,
    /// the pinned claim and the footer with Cancel / Approve are on screen
    /// and the card's scrolling region is what gives way. `visibleFrame`
    /// excludes the menu bar, so the top is never tucked under it.
    func centreOnActiveScreen() {
        guard let screen = NSScreen.main ?? NSScreen.screens.first else { return }
        let content = contentView?.fittingSize ?? frame.size
        // With `.fullSizeContentView` these are normally identical; converting
        // anyway keeps the clamp honest if the style mask ever changes.
        let wanted = frameRect(forContentRect: NSRect(origin: .zero, size: content)).size
        let f = PanelPlacement.clampedFrame(content: wanted, visible: screen.visibleFrame)
        // The hosting view must not push back with its ideal size as a
        // minimum, or AppKit grows the window past the clamp again.
        (contentView as? NSHostingView<AnyView>)?.sizingOptions = []
        setFrame(f, display: true)
    }
}

/// Hosts the card and drives it at 30 Hz so the countdown, the inert fade and
/// the hold fill all move.
@MainActor
final class ApprovalWindowController {
    private var panel: ApprovalPanel?
    private var ticker: Timer?
    private let model: AppModel

    init(model: AppModel) { self.model = model }

    func show() {
        let content = ApprovalHost(model: model)
        let p = ApprovalPanel(view: content.environment(model))
        p.centreOnActiveScreen()
        // Activating, unlike the dock: this one wants the user.
        NSApp.activate(ignoringOtherApps: true)
        p.makeKeyAndOrderFront(nil)
        panel = p
    }

    func hide() {
        ticker?.invalidate(); ticker = nil
        panel?.orderOut(nil)
        panel = nil
    }

    var isOpen: Bool { panel != nil }
}

/// A tiny wrapper so the card re-renders on a clock the model owns.
struct ApprovalHost: View {
    let model: AppModel
    @State private var now = Date().timeIntervalSince1970

    var body: some View {
        Group {
            if let gate = model.approval {
                ApprovalCardView(gate: gate, now: now)
            } else {
                Color.clear.frame(width: 560, height: 1)
            }
        }
        .onReceive(Timer.publish(every: 1.0 / 30.0, on: .main, in: .common).autoconnect()) { _ in
            now = Date().timeIntervalSince1970
        }
    }
}
