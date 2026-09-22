import Foundation

// Every type here is built from a `JSONValue` with a failable-but-forgiving
// initialiser. The rule is asymmetric on purpose:
//
//   display payloads  -> missing fields degrade to something harmless
//   the approval card -> a missing field that changes what consent MEANS is a
//                        hard failure, because a card that silently omits a
//                        consequence is worse than no card at all
//
// `ApprovalCard.init?` is the only one that returns nil.

// MARK: - ready

/// The dock's whole boot state in one frame.
public struct ReadyInfo: Sendable, Equatable {
    public var daaVersion: String = "?"
    public var dryRun: Bool = true
    public var alwaysOn: Bool = false
    public var jevLive: Bool = false
    /// "mic" -> "fake", "llm" -> "live", ...
    public var providers: [String: String] = [:]
    public var tools: [ToolInfo] = []
    public var missing: [String] = []

    public struct ToolInfo: Sendable, Equatable {
        public var name: String
        public var floor: String
        public init(name: String, floor: String) { self.name = name; self.floor = floor }
    }

    public init() {}

    public init(_ p: JSONValue) {
        daaVersion = p["daa"]?.stringValue ?? "?"
        // Absent dryRun means dry run. If the dock cannot tell whether the
        // thing behind it is live, it says the safer of the two out loud.
        dryRun = p["dryRun"]?.boolValue ?? true
        alwaysOn = p["alwaysOn"]?.boolValue ?? false
        jevLive = p["jevLive"]?.boolValue ?? false
        providers = p["providers"]?.stringMap ?? [:]
        missing = p["missing"]?.stringArray ?? []
        tools = (p["tools"]?.arrayValue ?? []).compactMap { t in
            guard let n = t["name"]?.stringValue else { return nil }
            return ToolInfo(name: n, floor: t["floor"]?.stringValue ?? "?")
        }
    }

    /// True when any safety-relevant provider is a fake. Shown in the dock,
    /// because "the risk gate is FakeJev" is not a detail.
    public var hasFakeProviders: Bool {
        providers.contains { $0.value.lowercased() != "live" }
    }
}

// MARK: - state

public enum Phase: String, Sendable, CaseIterable {
    case idle, listening, thinking, speaking, awaiting, working, degraded
}

public struct StateUpdate: Sendable, Equatable {
    public var phase: Phase
    public var detail: String
    public var since: Double

    public init(phase: Phase, detail: String = "", since: Double = 0) {
        self.phase = phase
        self.detail = detail
        self.since = since
    }

    public init(_ p: JSONValue) {
        // An unrecognised phase is not idle. A dock that renders "calm" for a
        // state it does not understand is lying about the machine behind it.
        phase = Phase(rawValue: p["phase"]?.stringValue ?? "") ?? .degraded
        detail = p["detail"]?.stringValue ?? ""
        since = p["since"]?.doubleValue ?? 0
    }
}

// MARK: - audit

/// A verbatim, already-redacted `AuditEvent`. The dock never redacts: Python
/// did it, by key, by scope and by shape, before the event left the loop.
public struct AuditRecord: Sendable, Equatable, Identifiable {
    public var id: String
    public var kind: String
    public var at: Double
    public var payload: [String: JSONValue]

    public init(id: String, kind: String, at: Double, payload: [String: JSONValue] = [:]) {
        self.id = id; self.kind = kind; self.at = at; self.payload = payload
    }

    public init(_ p: JSONValue) {
        id = p["id"]?.stringValue ?? UUID().uuidString
        kind = p["kind"]?.stringValue ?? "unknown"
        at = p["at"]?.doubleValue ?? Date().timeIntervalSince1970
        payload = p["payload"]?.objectValue ?? [:]
    }

    public var undoID: String? { payload["undo_id"]?.stringValue }
    public var toolName: String? { payload["tool"]?.stringValue }
    public var isDryRun: Bool { payload["dry_run"]?.boolValue ?? false }
    public var isSynthetic: Bool { payload["synthetic"]?.boolValue ?? false }

    /// Kinds that are display-only and may be dropped under backpressure.
    /// Everything else is a record of a decision and is never dropped.
    public static let chatty: Set<String> = ["heard", "spoke", "barge_in", "buffered"]

    /// Kinds that must survive any amount of load. Mirrors the Python-side tee.
    public static let loadBearing: Set<String> = [
        "disposition", "confirmation", "confirmation_event", "execution",
        "undo", "undo_rejected", "undo_retained", "refused", "dry_run",
        "visual_confirm", "deferred_visual", "abandoned", "error", "judgment",
    ]
}

// MARK: - confirm.request

public struct ApprovalArgument: Sendable, Equatable, Identifiable {
    public var key: String
    public var value: String
    /// True when this argument IS a program rather than a reference to one.
    /// Mirrors `_SCRIPT_KEYS` in loop.py. Script args sort first and are
    /// labelled THIS RUNS; they are the reason the tier exists.
    public var isProgram: Bool

    public var id: String { key }

    public init(key: String, value: String, isProgram: Bool) {
        self.key = key; self.value = value; self.isProgram = isProgram
    }

    public var lines: [String] {
        let l = value.components(separatedBy: "\n")
        return l.isEmpty ? [""] : l
    }
}

public struct RiskSummary: Sendable, Equatable {
    public var blastRadius: Double
    public var unrecoverable: Double
    public var explicitlyRequested: Double
    public var targetConfidence: String
    public var confidence: Double
    /// True when the judgment came from FakeJev or from a fail-closed default.
    /// The card says so in plain words. Hiding it would be dishonest about the
    /// current state of this repo.
    public var synthetic: Bool

    public init(blastRadius: Double = 3, unrecoverable: Double = 1,
                explicitlyRequested: Double = 0, targetConfidence: String = "guessing",
                confidence: Double = 0, synthetic: Bool = true) {
        self.blastRadius = blastRadius
        self.unrecoverable = unrecoverable
        self.explicitlyRequested = explicitlyRequested
        self.targetConfidence = targetConfidence
        self.confidence = confidence
        self.synthetic = synthetic
    }

    public init(_ p: JSONValue?) {
        // Every default here is the worst case. An assessment the dock could
        // not read is not a mild one.
        blastRadius = p?["blastRadius"]?.doubleValue ?? 3
        unrecoverable = p?["unrecoverable"]?.doubleValue ?? 1
        explicitlyRequested = p?["explicitlyRequested"]?.doubleValue ?? 0
        targetConfidence = p?["targetConfidence"]?.stringValue ?? "guessing"
        confidence = p?["confidence"]?.doubleValue ?? 0
        synthetic = p?["synthetic"]?.boolValue ?? true
    }
}

/// The payload of `confirm.request`: everything the most safety-critical
/// screen in the product needs, and nothing it is allowed to summarise.
public struct ApprovalCard: Sendable, Equatable, Identifiable {
    /// The single-use token minted by Python. The dock forwards it back once
    /// and never interprets it. It is not `_VISUAL_OK`; nothing on this side
    /// can produce that.
    public let id: String
    public let tool: String
    public let tier: String
    public let reason: String
    /// `_phrase(action)` — verb first. The largest type on the card.
    public let phrase: String
    public let verb: String
    public let explicit: Bool
    public let dryRun: Bool
    public let targets: [String]
    public let arguments: [ApprovalArgument]
    /// Destructive modifiers the user MUST see, because they change what
    /// consent means. Most destructive first (`ConsequenceOrder`), not by
    /// alphabet: "delete" used to render above "unrecoverable" because d < u.
    public let consequences: [(key: String, text: String)]
    public let assessment: RiskSummary
    public let expiresIn: TimeInterval
    /// `message: {to, body}` when Python sends one. See `MessageSpotlight`.
    public let explicitMessage: MessageSpotlight?

    public static func == (a: ApprovalCard, b: ApprovalCard) -> Bool {
        a.id == b.id && a.tool == b.tool && a.tier == b.tier && a.reason == b.reason
            && a.phrase == b.phrase && a.verb == b.verb && a.explicit == b.explicit
            && a.dryRun == b.dryRun && a.targets == b.targets && a.arguments == b.arguments
            && a.consequences.map(\.key) == b.consequences.map(\.key)
            && a.consequences.map(\.text) == b.consequences.map(\.text)
            && a.assessment == b.assessment && a.expiresIn == b.expiresIn
            && a.explicitMessage == b.explicitMessage
    }

    public init(
        id: String, tool: String, tier: String, reason: String, phrase: String,
        verb: String = "", explicit: Bool, dryRun: Bool, targets: [String],
        arguments: [ApprovalArgument], consequences: [(key: String, text: String)],
        assessment: RiskSummary, expiresIn: TimeInterval,
        explicitMessage: MessageSpotlight? = nil
    ) {
        self.id = id; self.tool = tool; self.tier = tier; self.reason = reason
        self.phrase = phrase; self.verb = verb; self.explicit = explicit
        self.dryRun = dryRun; self.targets = targets; self.arguments = arguments
        self.consequences = consequences; self.assessment = assessment
        self.expiresIn = expiresIn
        self.explicitMessage = explicitMessage
    }

    /// Returns nil when the frame cannot be rendered honestly.
    ///
    /// The bar is deliberately high. An approval card assembled out of a frame
    /// with no id, no tool or no phrase would still LOOK like a card, and a
    /// card someone approves is a consent record. A frame we cannot read in
    /// full is refused at the boundary instead.
    public init?(id: String, params p: JSONValue) {
        guard !id.isEmpty,
              let tool = p["tool"]?.stringValue, !tool.isEmpty,
              let phrase = p["phrase"]?.stringValue, !phrase.isEmpty
        else { return nil }

        self.id = id
        self.tool = tool
        self.tier = p["tier"]?.stringValue ?? "CONFIRM_VISUAL"
        self.reason = p["reason"]?.stringValue ?? "no reason given"
        self.phrase = phrase
        self.verb = p["verb"]?.stringValue ?? ""
        // Absent `explicit` means inferred, so the card shows the flag.
        self.explicit = p["explicit"]?.boolValue ?? false
        self.dryRun = p["dryRun"]?.boolValue ?? false
        self.targets = p["targets"]?.stringArray ?? []

        var args: [ApprovalArgument] = []
        for a in p["args"]?.arrayValue ?? [] {
            guard let k = a["key"]?.stringValue else { continue }
            args.append(ApprovalArgument(
                key: k,
                value: a["value"]?.stringValue ?? String(describing: a["value"] ?? .null),
                isProgram: a["isProgram"]?.boolValue ?? ApprovalCard.scriptKeys.contains(k)
            ))
        }
        // Script args first, then alphabetical. Same order as `_visual_detail`.
        self.arguments = args.sorted {
            ($0.isProgram ? 0 : 1, $0.key) < ($1.isProgram ? 0 : 1, $1.key)
        }

        self.consequences = ConsequenceOrder.sorted(
            p["consequences"]?.stringMap ?? [:],
            explicitOrder: p["consequenceOrder"]?.stringArray ?? [])

        if let m = p["message"], let body = m["body"]?.stringValue, !body.isEmpty {
            let to = m["to"]?.stringValue
            self.explicitMessage = MessageSpotlight(
                recipient: (to?.isEmpty ?? true) ? nil : to, body: body, consequenceKey: nil)
        } else {
            self.explicitMessage = nil
        }

        self.assessment = RiskSummary(p["assessment"])

        let ms = p["expiresInMs"]?.doubleValue ?? 90_000
        // Clamped, not trusted. A peer claiming a ten-hour window does not get
        // to leave an approval card sitting open overnight.
        self.expiresIn = min(max(ms / 1000, 5), 300)
    }

    /// Mirrors `_SCRIPT_KEYS` in `src/daa/voice/loop.py`. Kept as a fallback
    /// only: Python sends `isProgram` and that is authoritative.
    public static let scriptKeys: Set<String> =
        ["script", "applescript", "code", "command", "argv", "source"]

    public var scriptArguments: [ApprovalArgument] { arguments.filter(\.isProgram) }
    public var otherArguments: [ApprovalArgument] { arguments.filter { !$0.isProgram } }

    /// Every character the card must display. The completeness test asserts
    /// against this: nothing elided and nothing summarised.
    public var fullDisclosureText: String {
        var parts = [phrase]
        parts += consequences.map { "\($0.key): \($0.text)" }
        parts += targets
        parts += arguments.map { "\($0.key)\n\($0.value)" }
        if let m = explicitMessage { parts += [m.recipient ?? "", m.body] }
        return parts.joined(separator: "\n")
    }
}

// MARK: - tasks (room for background jobs)

public struct TaskRow: Sendable, Equatable, Identifiable {
    public var id: String
    public var title: String
    /// nil = indeterminate.
    public var progress: Double?
    public var cancellable: Bool
    public var startedAt: Double
    public var tier: String
    public var done: Bool = false
    public var outcome: String = ""

    public init(id: String, title: String, progress: Double? = nil,
                cancellable: Bool = false, startedAt: Double = 0, tier: String = "") {
        self.id = id; self.title = title; self.progress = progress
        self.cancellable = cancellable; self.startedAt = startedAt; self.tier = tier
    }

    public init?(_ p: JSONValue) {
        guard let id = p["id"]?.stringValue, !id.isEmpty else { return nil }
        self.id = id
        title = p["title"]?.stringValue ?? "working"
        progress = p["progress"]?.doubleValue
        cancellable = p["cancellable"]?.boolValue ?? false
        startedAt = p["startedAt"]?.doubleValue ?? Date().timeIntervalSince1970
        tier = p["tier"]?.stringValue ?? ""
    }
}
