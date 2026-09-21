import AppKit
import Carbon.HIToolbox
import DaaDockCore

/// Push-to-talk, on Carbon's `RegisterEventHotKey`.
///
/// This API is ancient and it is still the right one, for exactly one reason:
/// **it needs no Accessibility grant.** An `NSEvent.addGlobalMonitor` or a
/// `CGEventTap` both do, and push-to-talk must work on first launch, before
/// the user has been asked for anything but the microphone.
///
/// The nicer gesture (hold right-⌘, or double-tap-and-hold fn) does need an
/// event tap; that is a Settings opt-in that degrades to this and says why.
@MainActor
final class HotkeyManager {

    /// Tap < 300 ms latches (toggle-to-talk, tap again to send).
    /// Hold ≥ 300 ms is push-to-talk, released to send.
    static let tapThreshold: TimeInterval = 0.300

    enum Gesture: Equatable {
        case pressStart
        case pushToTalkEnd
        case latchOn
        case latchOff
    }

    var onGesture: ((Gesture) -> Void)?
    /// True when the hotkey could not be registered — almost always because
    /// another app already owns ⌥Space. Surfaced rather than swallowed.
    private(set) var registrationError: String?

    private var ref: EventHotKeyRef?
    private var handler: EventHandlerRef?
    private var pressedAt: TimeInterval?
    private var latched = false

    private static let signature: OSType = 0x64616168  // 'daah'

    func register(keyCode: UInt32 = UInt32(kVK_Space),
                  modifiers: UInt32 = UInt32(optionKey)) {
        unregister()

        var spec = [
            EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                          eventKind: UInt32(kEventHotKeyPressed)),
            EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                          eventKind: UInt32(kEventHotKeyReleased)),
        ]
        let me = Unmanaged.passUnretained(self).toOpaque()
        let status = InstallEventHandler(GetEventDispatcherTarget(), { _, event, ctx in
            guard let ctx, let event else { return noErr }
            let manager = Unmanaged<HotkeyManager>.fromOpaque(ctx).takeUnretainedValue()
            let kind = GetEventKind(event)
            Task { @MainActor in
                if kind == UInt32(kEventHotKeyPressed) { manager.keyDown() }
                else { manager.keyUp() }
            }
            return noErr
        }, 2, &spec, me, &handler)

        guard status == noErr else {
            registrationError = "could not install the hotkey handler (OSStatus \(status))"
            return
        }

        let id = EventHotKeyID(signature: Self.signature, id: 1)
        let reg = RegisterEventHotKey(keyCode, modifiers, id, GetEventDispatcherTarget(), 0, &ref)
        if reg != noErr {
            registrationError = "⌥Space is already taken by another app (OSStatus \(reg))"
        } else {
            registrationError = nil
        }
    }

    func unregister() {
        if let ref { UnregisterEventHotKey(ref) }
        ref = nil
        if let handler { RemoveEventHandler(handler) }
        handler = nil
    }

    // No `deinit` cleanup: Carbon's handles are not Sendable and a
    // nonisolated deinit cannot touch them. `unregister()` is called
    // explicitly from `applicationWillTerminate`, and the process is going
    // away in every other case.

    // MARK: - the state machine

    private func keyDown() {
        // A key repeat while already down is not a second press.
        guard pressedAt == nil else { return }
        if latched {
            latched = false
            onGesture?(.latchOff)
            return
        }
        pressedAt = Date().timeIntervalSince1970
        onGesture?(.pressStart)
    }

    private func keyUp() {
        guard let start = pressedAt else { return }
        pressedAt = nil
        let held = Date().timeIntervalSince1970 - start
        if held < Self.tapThreshold {
            latched = true
            onGesture?(.latchOn)
        } else {
            onGesture?(.pushToTalkEnd)
        }
    }

    var isLatched: Bool { latched }
    var isHeld: Bool { pressedAt != nil }
}

/// Esc, while the dock is frontmost or a card is up.
///
/// A global Esc would need an event tap, so this is a local monitor: it fires
/// only when one of daa's own windows has focus, which is the only place Esc
/// should mean "cancel daa" rather than "cancel whatever I was doing".
@MainActor
final class EscapeMonitor {
    private var monitor: Any?
    var onEscape: (() -> Void)?

    func start() {
        monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard event.keyCode == UInt16(kVK_Escape) else { return event }
            self?.onEscape?()
            return nil
        }
    }

    func stop() {
        if let monitor { NSEvent.removeMonitor(monitor) }
        monitor = nil
    }
}
