import AppKit
import SwiftUI

/// Offscreen rendering, with no screen capture anywhere in it.
///
/// Two backends, because neither alone is enough:
///
///  - `.hosting` (the default): the view is put in an `NSHostingView` inside an
///    `NSWindow` that is **never ordered on screen**, laid out, and asked to
///    draw itself into a bitmap with `cacheDisplay(in:to:)`. This is the view
///    painting its own layer tree into memory we own -- not a screenshot, not
///    `CGWindowListCreateImage`, not ScreenCaptureKit -- so it needs no Screen
///    Recording grant and works with no display attached. It renders the
///    AppKit-backed SwiftUI controls (`ScrollView`, `TextField`, switches,
///    `ProgressView`) that `ImageRenderer` cannot.
///
///  - `.imageRenderer`: SwiftUI's `ImageRenderer`. Pure SwiftUI, no window at
///    all, but every AppKit-backed control comes out as a yellow "unsupported"
///    placeholder -- including the `ScrollView` that holds the whole body of
///    the approval card. Kept so that limitation stays visible and checkable.
@MainActor
enum Renderer {
    enum Backend: String { case hosting, imageRenderer }

    enum Scheme: String, CaseIterable {
        case light, dark
        var appearance: NSAppearance { NSAppearance(named: self == .light ? .aqua : .darkAqua)! }
        var colorScheme: ColorScheme { self == .light ? .light : .dark }
    }

    enum Scroll { case top, bottom }

    /// A window that claims to be key, so controls draw in their active state
    /// (accent-coloured switches and focus rings) rather than the greyed-out
    /// look of a background window. It is never shown.
    final class OffscreenWindow: NSWindow {
        override var isKeyWindow: Bool { true }
        override var isMainWindow: Bool { true }
        override var canBecomeKey: Bool { true }
    }

    static let scale: CGFloat = 2

    static func render<V: View>(_ view: V, scheme: Scheme, scroll: Scroll = .top,
                                backend: Backend = .hosting) -> NSBitmapImageRep? {
        let root = view.environment(\.colorScheme, scheme.colorScheme)
        switch backend {
        case .imageRenderer:
            let r = ImageRenderer(content: root)
            r.scale = scale
            guard let cg = r.cgImage else { return nil }
            return NSBitmapImageRep(cgImage: cg)
        case .hosting:
            return renderHosted(root, scheme: scheme, scroll: scroll)
        }
    }

    private static func renderHosted<V: View>(_ view: V, scheme: Scheme, scroll: Scroll) -> NSBitmapImageRep? {
        let hv = NSHostingView(rootView: view)
        hv.appearance = scheme.appearance
        let size = hv.fittingSize
        let rect = NSRect(origin: .zero, size: NSSize(width: ceil(size.width), height: ceil(size.height)))
        let window = OffscreenWindow(contentRect: rect, styleMask: [.borderless],
                                     backing: .buffered, defer: true)
        window.appearance = scheme.appearance
        window.isOpaque = false
        window.backgroundColor = .clear
        window.isReleasedWhenClosed = false
        window.contentView = hv
        hv.frame = rect
        settle(hv)

        if scroll == .bottom {
            // Scroll every scroll view to its end, the way a reader would, so
            // the gate's "seen the bottom" state can be rendered.
            for sv in scrollViews(in: hv) {
                guard let doc = sv.documentView else { continue }
                let clip = sv.contentView
                let maxY = doc.isFlipped ? doc.frame.height - clip.bounds.height : 0
                clip.scroll(to: NSPoint(x: 0, y: max(0, maxY)))
                sv.reflectScrolledClipView(clip)
            }
            settle(hv)
        }

        guard let rep = hv.bitmapImageRepForCachingDisplay(in: hv.bounds) else { return nil }
        hv.cacheDisplay(in: hv.bounds, to: rep)
        window.contentView = nil
        return rep
    }

    /// Let SwiftUI finish a layout / update pass.
    private static func settle(_ v: NSView) {
        for _ in 0..<3 {
            v.layoutSubtreeIfNeeded()
            RunLoop.main.run(until: Date().addingTimeInterval(0.03))
        }
        v.layoutSubtreeIfNeeded()
    }

    static func scrollViews(in v: NSView) -> [NSScrollView] {
        var out: [NSScrollView] = []
        if let s = v as? NSScrollView { out.append(s) }
        for sub in v.subviews { out += scrollViews(in: sub) }
        return out
    }

    /// Draws with AppKit into a bitmap: used for the menu-bar glyph, which is
    /// an `NSImage` drawing handler, not a SwiftUI view.
    static func draw(size: NSSize, scale: CGFloat, scheme: Scheme,
                     _ body: (NSRect) -> Void) -> NSBitmapImageRep {
        let rep = NSBitmapImageRep(
            bitmapDataPlanes: nil, pixelsWide: Int(size.width * scale), pixelsHigh: Int(size.height * scale),
            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
            colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        rep.size = size
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
        scheme.appearance.performAsCurrentDrawingAppearance {
            body(NSRect(origin: .zero, size: size))
        }
        NSGraphicsContext.restoreGraphicsState()
        return rep
    }

    static func write(_ rep: NSBitmapImageRep, to url: URL) throws {
        guard let data = rep.representation(using: .png, properties: [:]) else {
            throw NSError(domain: "snapshots", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "PNG encode failed for \(url.lastPathComponent)"])
        }
        try data.write(to: url)
    }
}
