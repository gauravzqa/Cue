import Foundation

/// Why an approval card ended. Everything except `.held` is a refusal.
public enum ApprovalOutcome: Sendable, Equatable {
    /// The only way to approve: a deliberate press-and-hold that completed.
    case held
    case cancelled
    case escaped
    case timedOut
    /// Python withdrew the card (`confirm.cancel`).
    case withdrawn(String)
    /// The child died, or the dock is quitting, with a card on screen.
    case brainStopped
    /// The frame could not be rendered honestly.
    case unreadable

    public var granted: Bool { self == .held }

    public var wireReason: String {
        switch self {
        case .held: return "approved"
        case .cancelled: return "cancelled"
        case .escaped: return "escaped"
        case .timedOut: return "timeout"
        case .withdrawn(let r): return "withdrawn: \(r)"
        case .brainStopped: return "brain stopped"
        case .unreadable: return "unreadable request"
        }
    }
}

/// Why the Approve control is not usable right now. `nil` means it is.
public enum ApprovalBlock: Sendable, Equatable {
    /// The card is still fading in. Kills the click-through case where a card
    /// appears under a cursor that is already descending.
    case inert(remaining: TimeInterval)
    /// There is script text below the fold. You cannot approve what you have
    /// not scrolled past.
    case unread
    case expired
}

/// The rules of the approval card, with no UI and no clock of their own.
///
/// Every number here is a deliberate piece of friction, and each one answers a
/// specific way a card gets approved by accident:
///
///   `inertFor`  — a card appearing under a descending cursor
///   `holdFor`   — a stray Return, or a double-click landing on a new card
///   scroll gate — approving a script whose second half you never saw
///   `expiresIn` — walking away; silence is a refusal, and it is audited as a
///                 timeout rather than as a user saying no
///
/// It is a value type with explicit `now` so the tests drive time directly and
/// never sleep.
public struct ApprovalGate: Sendable, Equatable {
    public static let inertFor: TimeInterval = 0.400
    public static let holdFor: TimeInterval = 0.600

    public let card: ApprovalCard
    public let presentedAt: TimeInterval

    /// Set by the scroll view once the bottom of the script block has actually
    /// been on screen. Starts true only when there is nothing to scroll.
    public var scriptFullySeen: Bool

    /// Non-nil while the pointer is down on Approve.
    public var holdStartedAt: TimeInterval?

    public var resolution: ApprovalOutcome?

    public init(card: ApprovalCard, presentedAt: TimeInterval, scriptFullySeen: Bool = false) {
        self.card = card
        self.presentedAt = presentedAt
        // A card with no program argument has nothing to scroll past, so the
        // gate does not invent friction that teaches nothing.
        self.scriptFullySeen = scriptFullySeen || card.scriptArguments.isEmpty
        self.holdStartedAt = nil
        self.resolution = nil
    }

    public var expiresAt: TimeInterval { presentedAt + card.expiresIn }
    public var inertUntil: TimeInterval { presentedAt + Self.inertFor }

    public func secondsRemaining(at now: TimeInterval) -> TimeInterval {
        max(0, expiresAt - now)
    }

    /// The countdown, as the card shows it: "1:28".
    public func countdownText(at now: TimeInterval) -> String {
        let s = Int(secondsRemaining(at: now).rounded(.up))
        return String(format: "%d:%02d", s / 60, s % 60)
    }

    public func block(at now: TimeInterval) -> ApprovalBlock? {
        if now >= expiresAt { return .expired }
        if now < inertUntil { return .inert(remaining: inertUntil - now) }
        if !scriptFullySeen { return .unread }
        return nil
    }

    public func canApprove(at now: TimeInterval) -> Bool {
        resolution == nil && block(at: now) == nil
    }

    /// 0...1 fill of the hold control.
    public func holdProgress(at now: TimeInterval) -> Double {
        guard let start = holdStartedAt else { return 0 }
        return min(1, max(0, (now - start) / Self.holdFor))
    }

    // MARK: - Transitions. Each returns the new gate; none has a side effect.

    public func beginningHold(at now: TimeInterval) -> ApprovalGate {
        guard canApprove(at: now) else { return self }
        var g = self
        g.holdStartedAt = now
        return g
    }

    /// Release before the fill completes is NOT an approval and not a refusal
    /// either — the card stays up. A half-press means nothing.
    public func endingHold(at now: TimeInterval) -> ApprovalGate {
        var g = self
        if let start = holdStartedAt, now - start >= Self.holdFor, canApprove(at: now) {
            g.resolution = .held
        }
        g.holdStartedAt = nil
        return g
    }

    /// Called by the UI's display link so a hold that reaches full fill
    /// resolves without waiting for the release.
    public func ticking(at now: TimeInterval) -> ApprovalGate {
        var g = self
        if g.resolution == nil, now >= expiresAt {
            g.resolution = .timedOut
            g.holdStartedAt = nil
            return g
        }
        if g.resolution == nil, let start = holdStartedAt,
           now - start >= Self.holdFor, canApprove(at: now) {
            g.resolution = .held
            g.holdStartedAt = nil
        }
        return g
    }

    public func resolving(_ outcome: ApprovalOutcome) -> ApprovalGate {
        guard resolution == nil else { return self }
        var g = self
        g.resolution = outcome
        g.holdStartedAt = nil
        return g
    }

    public func scrolledToEnd() -> ApprovalGate {
        var g = self
        g.scriptFullySeen = true
        return g
    }
}

// MARK: - single-use tokens

/// The dock's half of the single-use rule.
///
/// Python mints `id` per card and drops any `confirm.result` carrying an
/// unknown, consumed or stale one. This registry is the matching guarantee on
/// this side: a card resolves exactly once, so a double-click on Approve, a
/// hold that completes as the timeout fires, and a `confirm.cancel` racing a
/// hold all produce exactly one response frame.
public final class PendingApprovals: @unchecked Sendable {
    private let lock = NSLock()
    private var open: [String: ApprovalCard] = [:]
    private var consumed: Set<String> = []

    public init() {}

    public enum AdmitResult: Sendable, Equatable {
        case admitted
        case duplicate
        case replayed
    }

    /// Returns false if this id has been seen before, in which case the caller
    /// must not put a card on screen.
    @discardableResult
    public func admit(_ card: ApprovalCard) -> AdmitResult {
        lock.lock(); defer { lock.unlock() }
        if consumed.contains(card.id) { return .replayed }
        if open[card.id] != nil { return .duplicate }
        open[card.id] = card
        return .admitted
    }

    /// Returns the card exactly once. Every later call returns nil, so there
    /// is exactly one `confirm.result` per `confirm.request`.
    public func consume(_ id: String) -> ApprovalCard? {
        lock.lock(); defer { lock.unlock() }
        guard let card = open.removeValue(forKey: id) else { return nil }
        consumed.insert(id)
        if consumed.count > 4096 { consumed = Set(consumed.dropFirst(1024)) }
        return card
    }

    public var openIDs: [String] {
        lock.lock(); defer { lock.unlock() }
        return Array(open.keys)
    }

    public var isEmpty: Bool {
        lock.lock(); defer { lock.unlock() }
        return open.isEmpty
    }
}
