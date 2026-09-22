import Foundation
import DaaDockCore

/// The frames the snapshots are driven by.
///
/// Everything marked `fake-bridge` is a byte-for-byte transcription of what
/// `tools/fake-bridge` sends -- same keys, same values, same script text -- and
/// it goes through the same `FrameCodec.decode` and `ConfirmRouter.admit` path
/// the running app uses, so a payload the app would refuse cannot be rendered
/// here either. Everything marked `variant` is a deliberate change to one of
/// those payloads, to reach a state fake-bridge has no phrase for; each says
/// what it changes.
///
/// If `tools/fake-bridge` changes, change this file to match.
@MainActor
enum Fixtures {

    // MARK: - fake-bridge: card(kind)

    static let script = """
        tell application "Messages"
          send "on my way" to buddy "Alex"
        end tell
        """

    static let longScript = (1...400).map { "do shell script \"echo step \($0) of 400\"" }
        .joined(separator: "\n")

    /// `card(kind)` in fake-bridge, for kind in script / long / synthetic / live.
    static func fakeBridgeCard(_ kind: String) -> [String: Any] {
        let s = kind == "long" ? longScript : script
        return [
            "tool": "run_applescript",
            "tier": "CONFIRM_VISUAL",
            "reason": "unrecoverable and not explicitly requested",
            "phrase": "run a script that sends a message to Alex",
            "verb": "run",
            "explicit": false,
            "dryRun": kind != "live",
            "targets": ["a script of \(s.components(separatedBy: "\n").count) lines"],
            "args": [
                ["key": "script", "isProgram": true, "value": s],
                ["key": "recipient", "isProgram": false, "value": "Alex"],
            ],
            "consequences": ["send": "sending this text: 'on my way'"],
            "assessment": [
                "blastRadius": 2.4,
                "unrecoverable": 0.81,
                "explicitlyRequested": 0.19,
                "targetConfidence": "probable",
                "confidence": 0.77,
                "synthetic": kind == "synthetic",
            ],
            "expiresInMs": 90_000,
        ]
    }

    // MARK: - variants

    /// variant: several destructive consequences, live (not a dry run), and a
    /// script that deletes rather than sends. fake-bridge only ever sends one
    /// consequence, so the multi-flag layout needs its own payload.
    static func destructiveCard() -> [String: Any] {
        let s = """
            tell application "Finder"
              delete (every item of folder "Downloads" of home whose modification date < (current date) - 30 * days)
              empty trash
            end tell
            """
        var c = fakeBridgeCard("live")
        c["phrase"] = "run a script that deletes old files in Downloads and empties the Trash"
        c["verb"] = "run"
        c["targets"] = ["~/Downloads (214 items older than 30 days)", "the Trash (1.3 GB)"]
        c["args"] = [
            ["key": "script", "isProgram": true, "value": s],
            ["key": "folder", "isProgram": false, "value": "~/Downloads"],
            ["key": "older_than_days", "isProgram": false, "value": "30"],
        ]
        c["consequences"] = [
            "delete": "moving 214 items from Downloads to the Trash",
            "empty_trash": "emptying the Trash — everything already in it is gone for good",
            "recursive": "including everything inside 9 subfolders",
            "unrecoverable": "this cannot be undone from daa",
        ]
        c["assessment"] = [
            "blastRadius": 2.9, "unrecoverable": 0.97, "explicitlyRequested": 0.12,
            "targetConfidence": "guessing", "confidence": 0.64, "synthetic": false,
        ]
        return c
    }

    /// variant: `explicit: true` -- the user asked for exactly this, so the
    /// "I inferred this" flag must be absent. For contrast with every other card.
    static func explicitCard() -> [String: Any] {
        var c = fakeBridgeCard("script")
        c["explicit"] = true
        c["reason"] = "unrecoverable"
        return c
    }

    // MARK: - wire

    /// Serialises to a real wire line and decodes it with the app's own codec.
    static func frame(_ obj: [String: Any]) -> Frame {
        let data = try! JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])
        return try! FrameCodec.decode(line: String(decoding: data, as: UTF8.self))
    }

    static func ev(_ method: String, _ payload: [String: Any]) -> Frame {
        frame(["t": "ev", "m": method, "p": payload])
    }

    /// The card as the app would admit it, via `ConfirmRouter`.
    static func admittedCard(_ payload: [String: Any], id: String) -> ApprovalCard {
        let f = frame(["t": "req", "id": id, "m": "confirm.request", "p": payload])
        guard case .request(let rid, _, let p) = f else { fatalError("not a request frame") }
        switch ConfirmRouter.admit(id: rid, params: p, pending: PendingApprovals(), cardAlreadyOpen: false) {
        case .present(let card): return card
        case let other: fatalError("the app would not present this card: \(other)")
        }
    }

    // MARK: - fake-bridge: ready / state / audit / task

    static let t0: Double = 1_790_000_000

    static let ready = ev("ready", [
        "daa": "0.1.0-fake", "dryRun": true, "alwaysOn": false, "jevLive": false,
        "providers": ["mic": "fake", "stt": "fake", "llm": "fake", "jev": "fake"],
        "tools": [
            ["name": "run_applescript", "floor": "CONFIRM_VISUAL"],
            ["name": "move_file", "floor": "CONFIRM_VOICE"],
            ["name": "open_app", "floor": "ANNOUNCE"],
            ["name": "read_clipboard", "floor": "SILENT"],
        ],
        "missing": ["this is the fake bridge -- nothing here is real"],
    ])

    /// `speak` — daa's answer. It is written into the transcript, never said.
    static func speak(_ text: String) -> Frame { ev("speak", ["text": text]) }

    static func state(_ phase: String, _ detail: String = "", tasks: [[String: Any]]? = nil) -> Frame {
        var p: [String: Any] = ["phase": phase, "detail": detail, "since": t0]
        if let tasks { p["tasks"] = tasks }
        return ev("state", p)
    }

    static var auditSeq = 0
    static func audit(_ kind: String, at offset: Double = 0, _ payload: [String: Any]) -> Frame {
        auditSeq += 1
        return ev("audit", ["kind": kind, "id": String(format: "a%011d", auditSeq),
                            "at": t0 + offset, "payload": payload])
    }

    /// An ordinary turn exactly as fake-bridge's `ordinary_turn` emits it,
    /// followed by the approval-card turn's refusal path and some real-world
    /// lines fake-bridge has no phrase for (marked).
    static var transcriptMix: [Frame] {
        [
            audit("woke", at: 0, ["text": "move those screenshots to Archive", "addressed_p": 0.91]),
            audit("disposition", at: 1, ["tool": "move_file", "tier": "ANNOUNCE",
                                          "reason": "recoverable and explicitly requested"]),
            audit("spoke", at: 2, ["text": "Moved 3 files to Archive."]),
            audit("execution", at: 3, ["tool": "move_file", "undo_id": "u1a2b3c", "dry_run": true,
                                       "summary": "Dry run: I would move 3 files to Archive."]),
            // fake-bridge `script`, then Cancel:
            audit("woke", at: 10, ["text": "script", "addressed_p": 0.98]),
            audit("visual_confirm", at: 20, ["tool": "run_applescript", "granted": false,
                                             "target_count": 1, "reason": "cancelled"]),
            audit("abandoned", at: 20, ["tool": "run_applescript", "reason": "cancelled"]),
            audit("spoke", at: 21, ["text": "Okay, leaving it."]),
            // variant: a live execution with an undo entry (drives the ↩︎).
            audit("woke", at: 30, ["text": "rename the Q3 draft to final", "addressed_p": 0.95]),
            audit("execution", at: 31, ["tool": "rename_file", "undo_id": "u9f8e7d", "dry_run": false,
                                        "summary": "Renamed “Q3 draft.key” to “Q3 final.key”."]),
            // variant: a refusal whose judgment was synthetic.
            audit("woke", at: 40, ["text": "clear out my desktop", "addressed_p": 0.88]),
            audit("refused", at: 41, ["tool": "move_file", "synthetic": true,
                                      "reason": "daa could not get a real judgment, so it assumed the worst and did not act"]),
            // fake-bridge's stale-token path.
            audit("error", at: 50, ["where": "confirm", "error": "unknown confirmation token cf_3e91a0c2"]),
        ]
    }

    static func task(_ id: String, _ title: String, progress: Double?, cancellable: Bool = true) -> Frame {
        var p: [String: Any] = ["id": id, "title": title, "cancellable": cancellable,
                                "startedAt": t0, "tier": "SILENT"]
        if let progress { p["progress"] = progress }
        return ev("task.update", p)
    }

    // MARK: - identity

    static let previousHash = "4be1d0c29f7a33e8b61a0d5c7e2f9a41c0de5b77"
    static let currentHash = "a93f02d17b4c58e6019fd2c3b5a7e8d40f61c2aa"

    static var rebuiltAdHoc: IdentityNotice {
        CodeIdentityCheck.explanation(.rebuiltAdHoc(
            previous: previousHash,
            current: SigningIdentity(cdhash: currentHash, isAdHoc: true, bundleID: "dev.daa.dock")))!
    }

    static var firstRunAdHoc: IdentityNotice {
        CodeIdentityCheck.explanation(.firstRun(
            SigningIdentity(cdhash: currentHash, isAdHoc: true, bundleID: "dev.daa.dock")))!
    }
}
