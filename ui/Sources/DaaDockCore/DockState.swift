import Foundation

/// How the 22×22 status item should look. Derived, never set.
///
/// Rule 1 of the UI: state is legible from the menu bar alone. Rule 2: the
/// amber badge is the ONLY colour the icon ever takes. If the icon has colour,
/// something needs a human — that single rule is worth more than any amount of
/// animation, and it is why `tint` is a three-case enum rather than a colour.
public struct MenuBarAppearance: Sendable, Equatable {
    public enum Tint: Sendable, Equatable {
        case monochrome
        /// The one exception: a decision is waiting for a person.
        case amber
    }

    public enum Motion: Sendable, Equatable {
        case still
        case level        // live 3-bar meter from mic RMS
        case pulse        // indeterminate "thinking"
        case bounce       // synced to TTS
        case arc          // background work in progress
    }

    public var symbol: String
    public var tint: Tint
    public var motion: Motion
    public var opacity: Double
    /// Drawn over the glyph, never instead of it.
    public var slashed: Bool
    /// The hairline ring that distinguishes an open mic at rest from a closed
    /// one. A hot mic must never look like a cold one.
    public var hotMic: Bool
    /// Read verbatim by VoiceOver, and by anyone hovering the status item.
    public var accessibilityLabel: String

    public var needsAttention: Bool { tint == .amber }
}

/// The dock's whole observable state, and the only place a phase is decided.
///
/// The Swift side holds NO policy: no tier, no threshold, no allowlist. This
/// type renders `state` / `audit` / `confirm.request` and forwards clicks.
/// Everything it decides is about pixels.
public struct DockState: Sendable, Equatable {
    public var phase: Phase = .idle
    public var detail: String = ""
    public var ready: ReadyInfo = ReadyInfo()
    public var alwaysOn: Bool = false
    /// 0...1, from Swift's own RMS. Never leaves this process.
    public var micLevel: Double = 0
    /// Live partial text. Populated ONLY under push-to-talk; see `mayShowPartial`.
    public var partial: String = ""
    public var pushToTalkHeld: Bool = false
    public var tasks: [TaskRow] = []
    public var approvalOpen: Bool = false
    public var restartAttempt: Int = 0
    public var nextRestartIn: TimeInterval = 0
    public var gaveUp: Bool = false

    public init() {}

    /// *"Speech is not written down until it is addressed to you."*
    ///
    /// Under push-to-talk the user has already addressed daa by holding a key,
    /// so a live partial is theirs to see. In always-on mode nothing is shown
    /// as text until the address gate has woken — the dock shows a waveform
    /// instead. The difference is itself a piece of UI that teaches the model.
    public var mayShowPartial: Bool { pushToTalkHeld }

    public var liveText: String {
        if mayShowPartial { return partial }
        switch phase {
        case .listening: return alwaysOn ? "listening — nothing written down yet" : "listening…"
        case .thinking: return detail.isEmpty ? "thinking…" : detail
        case .speaking: return detail
        case .awaiting: return "waiting for your approval"
        case .working: return tasks.first?.title ?? "working…"
        case .degraded: return degradedText
        case .idle: return alwaysOn ? "always on" : ""
        }
    }

    public var degradedText: String {
        if gaveUp { return "daa's brain keeps stopping. It is not restarting on its own." }
        if restartAttempt == 0 { return "daa's brain stopped." }
        let s = Int(nextRestartIn.rounded())
        return "daa's brain stopped — restarting in \(s)s (attempt \(restartAttempt))"
    }

    public var hasActiveTasks: Bool { tasks.contains { !$0.done } }

    /// The one derivation that matters. Approval always outranks progress:
    /// if any task is running AND a card is open, amber wins.
    public var menuBar: MenuBarAppearance {
        if approvalOpen || phase == .awaiting {
            return MenuBarAppearance(
                symbol: "waveform.badge.exclamationmark", tint: .amber, motion: .still,
                opacity: 1, slashed: false, hotMic: alwaysOn,
                accessibilityLabel: "daa needs your approval")
        }
        if phase == .degraded {
            return MenuBarAppearance(
                symbol: "waveform.slash", tint: .monochrome, motion: .still,
                opacity: 1, slashed: true, hotMic: false,
                accessibilityLabel: degradedText)
        }
        if phase == .working || hasActiveTasks {
            return MenuBarAppearance(
                symbol: "waveform", tint: .monochrome, motion: .arc,
                opacity: 1, slashed: false, hotMic: alwaysOn,
                accessibilityLabel: "daa is working on \(tasks.filter { !$0.done }.count) thing(s)")
        }
        switch phase {
        case .listening:
            return MenuBarAppearance(
                symbol: "waveform", tint: .monochrome, motion: .level,
                opacity: 1, slashed: false, hotMic: alwaysOn,
                accessibilityLabel: "daa is listening")
        case .thinking:
            return MenuBarAppearance(
                symbol: "waveform", tint: .monochrome, motion: .pulse,
                opacity: 1, slashed: false, hotMic: alwaysOn,
                accessibilityLabel: "daa is thinking")
        case .speaking:
            return MenuBarAppearance(
                symbol: "waveform", tint: .monochrome, motion: .bounce,
                opacity: 1, slashed: false, hotMic: alwaysOn,
                accessibilityLabel: "daa is speaking. Click to interrupt.")
        default:
            return MenuBarAppearance(
                symbol: "waveform", tint: .monochrome, motion: .still,
                opacity: alwaysOn ? 0.85 : 0.55, slashed: false, hotMic: alwaysOn,
                accessibilityLabel: alwaysOn ? "daa is idle, microphone open" : "daa is idle")
        }
    }
}

// MARK: - transcript

public struct TranscriptLine: Sendable, Equatable, Identifiable {
    public enum Speaker: String, Sendable { case you, daa, system }
    public var id: String
    public var speaker: Speaker
    public var text: String
    public var at: Double
    /// Present only when this turn produced an undo entry. Drives the ↩︎.
    public var undoID: String?
    /// Rendered in the quieter dry-run style so a dry run can never be
    /// mistaken for a real one.
    public var dryRun: Bool
    public var refused: Bool

    public init(id: String = UUID().uuidString, speaker: Speaker, text: String,
                at: Double = Date().timeIntervalSince1970, undoID: String? = nil,
                dryRun: Bool = false, refused: Bool = false) {
        self.id = id; self.speaker = speaker; self.text = text; self.at = at
        self.undoID = undoID; self.dryRun = dryRun; self.refused = refused
    }
}

/// Turns the audit stream into transcript rows.
///
/// The read path needs no new instrumentation: `VoiceLoop.audit` is already
/// structured, already redacted, and already carries `synthetic`. This is the
/// only place that decides which kinds become visible lines, and it is a pure
/// function so the mapping is testable without a running loop.
public enum TranscriptProjection {

    public static func line(for record: AuditRecord) -> TranscriptLine? {
        switch record.kind {
        case "heard":
            // `heard` carries the SHAPE of an utterance, never its content
            // (`_shape` in loop.py: chars, words, sha256_8). So it cannot
            // produce a transcript line, and must not pretend to.
            return nil

        case "woke":
            guard let text = record.payload["text"]?.stringValue, !text.isEmpty else { return nil }
            return TranscriptLine(id: record.id, speaker: .you, text: text, at: record.at)

        case "spoke":
            guard let text = record.payload["text"]?.stringValue, !text.isEmpty else { return nil }
            return TranscriptLine(id: record.id, speaker: .daa, text: text, at: record.at)

        case "execution":
            let tool = record.toolName ?? "something"
            let summary = record.payload["summary"]?.stringValue ?? "Ran \(tool)."
            return TranscriptLine(id: record.id, speaker: .daa, text: summary, at: record.at,
                                  undoID: record.undoID, dryRun: record.isDryRun)

        case "dry_run":
            let tool = record.toolName ?? "that"
            let summary = record.payload["summary"]?.stringValue ?? "Dry run: I would run \(tool)."
            return TranscriptLine(id: record.id, speaker: .daa, text: summary,
                                  at: record.at, dryRun: true)

        case "refused", "abandoned", "undo_rejected", "deferred_visual":
            let why = record.payload["reason"]?.stringValue ?? record.kind
            return TranscriptLine(id: record.id, speaker: .system,
                                  text: "Not done — \(why)", at: record.at, refused: true)

        case "error":
            let where_ = record.payload["where"]?.stringValue ?? "somewhere"
            let err = record.payload["error"]?.stringValue ?? "unknown"
            return TranscriptLine(id: record.id, speaker: .system,
                                  text: "Error in \(where_): \(err)", at: record.at, refused: true)

        default:
            return nil
        }
    }
}

/// A bounded ring of transcript lines.
///
/// Capped to mirror the in-memory `Transcript` (12 turns / 2000 chars) rather
/// than to save memory: showing more than the model can see would be a lie
/// about what it remembers. Deeper history comes from `~/.daa/audit.jsonl` in
/// the History window, opened on demand, never streamed into the dock.
public struct TranscriptRing: Sendable, Equatable {
    public private(set) var lines: [TranscriptLine] = []
    public let capacity: Int

    public init(capacity: Int = 24) { self.capacity = capacity }

    public mutating func append(_ line: TranscriptLine) {
        lines.append(line)
        if lines.count > capacity { lines.removeFirst(lines.count - capacity) }
    }

    public mutating func clear() { lines.removeAll() }

    /// The most recent line that produced an undo entry, if any.
    public var undoableLine: TranscriptLine? {
        lines.last { $0.undoID != nil && !$0.dryRun }
    }
}
