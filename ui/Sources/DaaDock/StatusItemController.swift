import AppKit
import SwiftUI
import DaaDockCore

/// The 22×22 truth.
///
/// It must answer "is it listening? is it doing something? does it need me?"
/// from across the room, and the amber badge must be the only colour it ever
/// takes. Every decision about what it looks like lives in
/// `DockState.menuBar`, which is pure and tested; this file only draws it.
@MainActor
final class StatusItemController {
    private let item: NSStatusItem
    private var appearance: MenuBarAppearance?
    private var animationPhase: Double = 0
    private var timer: Timer?
    private var level: Double = 0

    var onLeftClick: (() -> Void)?
    var onRightClick: (() -> Void)?

    init() {
        item = NSStatusBar.system.statusItem(withLength: 26)
        item.button?.target = self
        item.button?.action = #selector(clicked)
        item.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
        item.button?.imagePosition = .imageOnly
        startAnimating()
    }

    @objc private func clicked() {
        let event = NSApp.currentEvent
        if event?.type == .rightMouseUp
            || (event?.modifierFlags.contains(.control) ?? false) {
            onRightClick?()
        } else {
            onLeftClick?()
        }
    }

    func update(_ a: MenuBarAppearance, level: Double) {
        appearance = a
        self.level = level
        item.button?.toolTip = a.accessibilityLabel
        item.button?.setAccessibilityLabel(a.accessibilityLabel)
        redraw()
    }

    var buttonFrameOnScreen: NSRect? {
        guard let window = item.button?.window else { return nil }
        return window.frame
    }

    private func startAnimating() {
        let t = Timer(timeInterval: 1.0 / 20.0, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self, let a = self.appearance, a.motion != .still else { return }
                self.animationPhase += 0.05
                self.redraw()
            }
        }
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    private func redraw() {
        guard let a = appearance, let button = item.button else { return }
        let size = NSSize(width: 22, height: 22)
        let image = NSImage(size: size, flipped: false) { [weak self] rect in
            guard let self else { return true }
            self.draw(a, in: rect)
            return true
        }
        // `isTemplate` only when monochrome: a template image is recoloured by
        // the system and would erase the amber, which is the one thing that
        // must survive.
        image.isTemplate = a.tint == .monochrome
        button.image = image
        button.alphaValue = a.opacity
    }

    private func draw(_ a: MenuBarAppearance, in rect: NSRect) {
        let ink: NSColor = a.tint == .amber ? .systemOrange : .labelColor

        switch a.motion {
        case .level:
            drawBars(rect, ink: ink, level: level)
        case .pulse:
            let t = (sin(animationPhase * 2) + 1) / 2
            drawBars(rect, ink: ink, level: 0.25 + 0.35 * t)
        case .bounce:
            let t = (sin(animationPhase * 4) + 1) / 2
            drawBars(rect, ink: ink, level: 0.35 + 0.5 * t)
        case .arc:
            drawArc(rect, ink: ink)
        case .still:
            drawBars(rect, ink: ink, level: 0.32)
        }

        if a.hotMic {
            // An open mic at rest must never look like a closed one.
            ink.withAlphaComponent(0.55).setStroke()
            let ring = NSBezierPath(ovalIn: rect.insetBy(dx: 1.5, dy: 1.5))
            ring.lineWidth = 1
            ring.stroke()
        }

        if a.slashed {
            ink.setStroke()
            let slash = NSBezierPath()
            slash.move(to: NSPoint(x: rect.minX + 3, y: rect.minY + 3))
            slash.line(to: NSPoint(x: rect.maxX - 3, y: rect.maxY - 3))
            slash.lineWidth = 1.6
            slash.stroke()
        }

        if a.needsAttention {
            NSColor.systemOrange.setFill()
            NSBezierPath(ovalIn: NSRect(x: rect.maxX - 6.5, y: rect.maxY - 6.5,
                                        width: 5, height: 5)).fill()
        }
    }

    private func drawBars(_ rect: NSRect, ink: NSColor, level: Double) {
        ink.setFill()
        let heights = [0.45, 1.0, 0.6].map { max(0.18, min(1, $0 * (0.35 + level))) }
        let w: CGFloat = 2.6
        let gap: CGFloat = 2.4
        let total = w * 3 + gap * 2
        var x = rect.midX - total / 2
        for h in heights {
            let bh = rect.height * 0.62 * h
            let bar = NSRect(x: x, y: rect.midY - bh / 2, width: w, height: bh)
            NSBezierPath(roundedRect: bar, xRadius: w / 2, yRadius: w / 2).fill()
            x += w + gap
        }
    }

    private func drawArc(_ rect: NSRect, ink: NSColor) {
        ink.setStroke()
        let path = NSBezierPath()
        let start = animationPhase * 90
        path.appendArc(withCenter: NSPoint(x: rect.midX, y: rect.midY),
                       radius: rect.width * 0.3,
                       startAngle: start, endAngle: start + 250)
        path.lineWidth = 1.8
        path.lineCapStyle = .round
        path.stroke()
    }
}
