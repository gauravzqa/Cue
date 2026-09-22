import Foundation
import CoreGraphics

// How the approval card PRESENTS a decision Python already made.
//
// Nothing in this file decides whether anything needs approval, how risky it
// is, or whether it may run. It takes facts that arrived in the frame
// (`dryRun`, the consequence map, the arguments) and chooses words, emphasis,
// order and geometry for them. It lives in Core, with no SwiftUI, so every
// one of those choices is a plain value the test suite can assert on.

// MARK: - stakes: what approving will actually do

/// The affirmative statement at the top of every card of what approving it
/// will do.
///
/// Both states are a PRESENCE. The original card had a dry-run banner and, for
/// a live action, nothing -- so the card that would really text someone was
/// calmer than the one that would do nothing, and "live" was defined by an
/// absence nobody could see or test.
public struct ApprovalStakes: Sendable, Equatable {
    public enum Kind: Sendable, Equatable { case live, dryRun }
    /// `.serious` is the loudest treatment the card has. `.calm` is for a
    /// dry run: low stakes, and it should look it.
    public enum Emphasis: Sendable, Equatable { case calm, serious }

    public let kind: Kind
    public let title: String
    public let detail: String
    /// SF Symbol name.
    public let symbol: String
    public let emphasis: Emphasis
    /// The Approve control's own words. Differs between the two states so the
    /// control itself says what pressing it means, not just the banner.
    public let approveLabel: String
    public let approveAccessibilityLabel: String

    public static let live = ApprovalStakes(
        kind: .live,
        title: "This will really happen.",
        detail: "Approving runs it now, on this Mac, for real. This is not a dry run.",
        symbol: "bolt.fill",
        emphasis: .serious,
        approveLabel: "Approve for real · hold",
        approveAccessibilityLabel: "Approve for real by holding. This will really happen.")

    public static let dryRun = ApprovalStakes(
        kind: .dryRun,
        title: "Dry run — approving this will not actually run it.",
        detail: "",
        symbol: "testtube.2",
        emphasis: .calm,
        approveLabel: "Approve · hold",
        approveAccessibilityLabel: "Approve by holding. Dry run: nothing will actually run.")

    /// The banner's full text, as a screen reader or a test sees it.
    public var bannerText: String { detail.isEmpty ? title : "\(title) \(detail)" }
}

// MARK: - consequence order

/// Display order for consequence flags: the most destructive first.
///
/// The keys come from the resolvers in `src/daa/tools/`. A key missing from
/// this table is NOT hidden or demoted below the benign ones -- an unknown
/// consequence is assumed to matter and sits between the destructive and the
/// informational groups. This only orders lines that are all shown anyway.
///
/// Python owning this order (sending an ordered list) would be better; until
/// it does, an explicit `consequenceOrder` array in the frame overrides the
/// table entirely.
public enum ConsequenceOrder {
    /// Most destructive first.
    public static let known: [String] = [
        "unrecoverable", "empty_trash", "delete", "overwrite", "recursive",
        "protected", "contents", "send", "message", "input", "does",
        "blocked", "review",
    ]
    /// Where an unrecognised key lands: after the destructive group, before
    /// the informational one ("does", "blocked", "review").
    static let unknownRank = known.firstIndex(of: "does")!

    public static func rank(_ key: String) -> Int {
        known.firstIndex(of: key) ?? unknownRank
    }

    /// `explicitOrder`, when Python sends one, wins outright; keys it omits
    /// follow in table order.
    public static func sorted(_ map: [String: String], explicitOrder: [String] = [])
        -> [(key: String, text: String)]
    {
        func r(_ k: String) -> (Int, Int, String) {
            if let i = explicitOrder.firstIndex(of: k) { return (0, i, k) }
            return (1, rank(k), k)
        }
        return map.sorted { r($0.key) < r($1.key) }.map { (key: $0.key, text: $0.value) }
    }
}

// MARK: - the message itself

/// On a card that sends a message, what is being sent and to whom is the most
/// important fact on it, so it is lifted out of the flag list and shown large.
///
/// Sourced, in order, from an explicit `message: {to, body}` in the frame, or
/// from a consequence whose text begins "sending this text:" (the wording the
/// resolvers use today) plus a recipient-like argument. Either way the text is
/// Python's, verbatim; nothing is paraphrased. If neither is present there is
/// no spotlight and the consequence stays an ordinary flag.
public struct MessageSpotlight: Sendable, Equatable {
    public let recipient: String?
    public let body: String
    /// The consequence this replaces in the flag list, so it is shown once.
    public let consequenceKey: String?

    public init(recipient: String?, body: String, consequenceKey: String?) {
        self.recipient = recipient; self.body = body; self.consequenceKey = consequenceKey
    }

    static let prefix = "sending this text:"
    static let recipientKeys = ["recipient", "to", "buddy", "contact"]

    static func unquoted(_ s: String) -> String {
        let t = s.trimmingCharacters(in: .whitespaces)
        for (open, close) in [("'", "'"), ("\"", "\""), ("“", "”"), ("‘", "’")] {
            if t.count >= 2, t.hasPrefix(open), t.hasSuffix(close) {
                return String(t.dropFirst().dropLast())
            }
        }
        return t
    }
}

extension ApprovalCard {
    public var stakes: ApprovalStakes { dryRun ? .dryRun : .live }

    public var messageSpotlight: MessageSpotlight? {
        if let m = explicitMessage { return m }
        guard let c = consequences.first(where: {
            $0.text.lowercased().hasPrefix(MessageSpotlight.prefix)
        }) else { return nil }
        let body = MessageSpotlight.unquoted(String(c.text.dropFirst(MessageSpotlight.prefix.count)))
        guard !body.isEmpty else { return nil }
        let to = MessageSpotlight.recipientKeys.lazy
            .compactMap { k in self.otherArguments.first(where: { $0.key == k })?.value }
            .first
        return MessageSpotlight(recipient: to, body: body, consequenceKey: c.key)
    }

    /// Consequences still shown as flags: everything except the one the
    /// message spotlight already shows.
    public var flaggedConsequences: [(key: String, text: String)] {
        guard let k = messageSpotlight?.consequenceKey else { return consequences }
        return consequences.filter { $0.key != k }
    }
}

// MARK: - where the panel goes

/// Placement of the approval panel on a screen, as a pure function.
///
/// The card is sized to its content (up to 720 pt), which on a 1024×665
/// "Larger Text" display is taller than the screen: Cancel, Approve and the
/// countdown ended up below the bottom edge, so the user who needs large text
/// could never approve anything. The frame is therefore clamped to the
/// screen's `visibleFrame` (which already excludes the menu bar and the Dock)
/// and the card's scrolling region absorbs the difference; its header, pinned
/// claim and footer are outside that region and always on screen.
///
/// AppKit coordinates: origin bottom-left, y up.
public enum PanelPlacement {
    /// Kept clear of the visible frame's edges.
    public static let margin: CGFloat = 12
    /// A centred card sits slightly above centre, where the eye already is.
    public static let lift: CGFloat = 40

    public static func clampedFrame(content: CGSize, visible: CGRect,
                                    margin: CGFloat = margin, lift: CGFloat = lift) -> CGRect {
        let room = visible.insetBy(dx: min(margin, visible.width / 4),
                                   dy: min(margin, visible.height / 4))
        let w = min(content.width, room.width)
        let h = min(content.height, room.height)
        var x = visible.midX - w / 2
        var y = visible.midY - h / 2 + lift
        // Top edge below the menu bar first, then bottom edge above the Dock.
        // With h <= room.height both hold at once.
        y = min(y, room.maxY - h)
        y = max(y, room.minY)
        x = min(max(x, room.minX), room.maxX - w)
        return CGRect(x: x, y: y, width: w, height: h)
    }
}
