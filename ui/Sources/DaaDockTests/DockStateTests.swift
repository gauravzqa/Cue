import Foundation
import MicroTest
import DaaDockCore

final class DockStateTests: XCTestCase {

    func testAmberIsTheOnlyColourTheIconEverTakes() {
        // If the icon has colour, something needs a human. That single rule is
        // worth more than any amount of animation.
        for phase in Phase.allCases where phase != .awaiting {
            var s = DockState()
            s.phase = phase
            XCTAssertEqual(s.menuBar.tint, .monochrome, "\(phase) must be monochrome")
            XCTAssertFalse(s.menuBar.needsAttention, "\(phase)")
        }
        var awaiting = DockState()
        awaiting.phase = .awaiting
        XCTAssertEqual(awaiting.menuBar.tint, .amber)
        XCTAssertTrue(awaiting.menuBar.needsAttention)
    }

    func testApprovalAlwaysOutranksProgress() {
        var s = DockState()
        s.phase = .working
        s.tasks = [TaskRow(id: "t1", title: "indexing"), TaskRow(id: "t2", title: "fetching")]
        XCTAssertEqual(s.menuBar.motion, .arc)
        XCTAssertEqual(s.menuBar.tint, .monochrome)

        s.approvalOpen = true
        XCTAssertEqual(s.menuBar.tint, .amber, "a card open during background work still wins")
        XCTAssertTrue(s.menuBar.needsAttention)
    }

    func testAHotMicNeverLooksLikeAColdOne() {
        var cold = DockState(); cold.phase = .idle; cold.alwaysOn = false
        var hot = DockState(); hot.phase = .idle; hot.alwaysOn = true
        XCTAssertNotEqual(cold.menuBar, hot.menuBar)
        XCTAssertFalse(cold.menuBar.hotMic)
        XCTAssertTrue(hot.menuBar.hotMic)
    }

    func testDegradedIsVisiblyDifferentAndSaysWhatIsHappening() {
        var s = DockState()
        s.phase = .degraded
        s.restartAttempt = 2
        s.nextRestartIn = 4
        XCTAssertTrue(s.menuBar.slashed)
        XCTAssertTrue(s.degradedText.contains("restarting in 4s"))
        s.gaveUp = true
        XCTAssertTrue(s.degradedText.contains("not restarting on its own"))
    }

    func testAnUnknownPhaseIsNotIdle() {
        // A dock that renders "calm" for a state it does not understand is
        // lying about the machine behind it.
        XCTAssertEqual(StateUpdate(.object(["phase": .string("teleporting")])).phase, .degraded)
        XCTAssertEqual(StateUpdate(.object([:])).phase, .degraded)
        XCTAssertEqual(StateUpdate(.object(["phase": .string("thinking")])).phase, .thinking)
    }

    func testThereIsNoSpeakingPhase() {
        // daa does not speak aloud. Nothing may put the dock into a phase that
        // says it is talking, and `speaking` on the wire is an unknown phase
        // like any other -- so it renders as degraded, not as idle.
        XCTAssertEqual(Phase.allCases.map(\.rawValue),
                       ["idle", "listening", "thinking", "awaiting", "working", "degraded"])
        XCTAssertNil(Phase(rawValue: "speaking"))
        XCTAssertEqual(StateUpdate(.object(["phase": .string("speaking")])).phase, .degraded)

        // And no phase animates the icon to a rhythm it no longer has.
        for phase in Phase.allCases {
            var s = DockState()
            s.phase = phase
            XCTAssertTrue([.still, .level, .pulse, .arc].contains(s.menuBar.motion), "\(phase)")
        }
    }

    func testDaaAnswersInWriting() {
        // The `speak` frame stays, and its text becomes an ordinary `daa`
        // transcript line -- the only place daa's answer ever appears.
        let r = AuditRecord(.object([
            "kind": .string("spoke"), "id": .string("s1"), "at": .number(1),
            "payload": .object(["text": .string("Moved 3 files to Archive.")]),
        ]))
        let line = TranscriptProjection.line(for: r)!
        XCTAssertEqual(line.speaker, .daa)
        XCTAssertEqual(line.text, "Moved 3 files to Archive.")
    }

    func testTheSameAnswerArrivingTwiceIsOneLine() {
        // It reaches the dock once as `speak` and once as the `spoke` audit
        // record. Printed twice it would read as daa answering twice.
        var ring = TranscriptRing()
        ring.append(TranscriptLine(speaker: .daa, text: "Moved 3 files to Archive."))
        ring.append(TranscriptLine(speaker: .daa, text: "Moved 3 files to Archive."))
        XCTAssertEqual(ring.lines.count, 1)

        // Only back-to-back plain answers collapse: the same sentence said
        // again later in the turn, or a line that carries an undo entry, is
        // its own event and stays.
        ring.append(TranscriptLine(speaker: .you, text: "again"))
        ring.append(TranscriptLine(speaker: .daa, text: "Moved 3 files to Archive."))
        ring.append(TranscriptLine(speaker: .daa, text: "Moved 3 files to Archive.", undoID: "u1"))
        XCTAssertEqual(ring.lines.count, 4)
        XCTAssertEqual(ring.undoableLine?.undoID, "u1")
    }

    // MARK: - the privacy boundary is visible

    func testLivePartialsAreOnlyShownUnderPushToTalk() {
        // "Speech is not written down until it is addressed to you."
        var s = DockState()
        s.phase = .listening
        s.alwaysOn = true
        s.partial = "i was telling my colleague about the merger"
        XCTAssertFalse(s.mayShowPartial)
        XCTAssertFalse(s.liveText.contains("merger"))
        XCTAssertTrue(s.liveText.contains("nothing written down"))

        // Holding the hotkey IS an unambiguous act of address.
        s.pushToTalkHeld = true
        XCTAssertTrue(s.mayShowPartial)
        XCTAssertEqual(s.liveText, "i was telling my colleague about the merger")
    }

    // MARK: - ready

    func testAbsentDryRunMeansDryRun() {
        // If the dock cannot tell whether the thing behind it is live, it shows
        // the safer of the two.
        XCTAssertTrue(ReadyInfo(.object([:])).dryRun)
        XCTAssertFalse(ReadyInfo(.object(["dryRun": .bool(false)])).dryRun)
    }

    func testFakeProvidersAreVisible() {
        let r = ReadyInfo(.object(["providers": .object([
            "mic": .string("fake"), "llm": .string("live"), "jev": .string("live"),
        ])]))
        XCTAssertTrue(r.hasFakeProviders)
        let live = ReadyInfo(.object(["providers": .object(["llm": .string("live")])]))
        XCTAssertFalse(live.hasFakeProviders)
    }

    // MARK: - transcript projection

    func testHeardNeverBecomesATranscriptLine() {
        // `heard` carries the SHAPE of an utterance, never its content.
        let r = AuditRecord(.object([
            "kind": .string("heard"), "id": .string("a"), "at": .number(1),
            "payload": .object(["chars": .number(19), "words": .number(4),
                                "sha256_8": .string("deadbeef")]),
        ]))
        XCTAssertNil(TranscriptProjection.line(for: r))
    }

    func testUndoAffordanceAppearsOnlyWhenTheTurnProducedAnUndoEntry() {
        let withUndo = AuditRecord(.object([
            "kind": .string("execution"), "id": .string("e1"), "at": .number(1),
            "payload": .object(["tool": .string("move_file"), "undo_id": .string("u7"),
                                "summary": .string("Moved 3 files to Archive.")]),
        ]))
        XCTAssertEqual(TranscriptProjection.line(for: withUndo)?.undoID, "u7")

        let without = AuditRecord(.object([
            "kind": .string("execution"), "id": .string("e2"), "at": .number(1),
            "payload": .object(["tool": .string("open_app")]),
        ]))
        XCTAssertNil(TranscriptProjection.line(for: without)?.undoID)
    }

    func testDryRunIsCarriedThroughToTheLineStyle() {
        let r = AuditRecord(.object([
            "kind": .string("dry_run"), "id": .string("d"), "at": .number(1),
            "payload": .object(["tool": .string("delete_file"),
                                "summary": .string("Dry run: I would move 3 files to the Trash.")]),
        ]))
        let line = TranscriptProjection.line(for: r)!
        XCTAssertTrue(line.dryRun, "a dry run must never be mistaken for a real one")
        XCTAssertTrue(line.text.hasPrefix("Dry run:"))
    }

    func testARefusalIsShownAsARefusal() {
        for kind in ["refused", "abandoned", "undo_rejected", "deferred_visual"] {
            let r = AuditRecord(.object([
                "kind": .string(kind), "id": .string(kind), "at": .number(1),
                "payload": .object(["reason": .string("no screen")]),
            ]))
            let line = TranscriptProjection.line(for: r)
            XCTAssertNotNil(line, kind)
            XCTAssertTrue(line!.refused, kind)
        }
    }

    func testRingIsBoundedAndFindsTheUndoableTurn() {
        var ring = TranscriptRing(capacity: 3)
        for i in 0..<5 { ring.append(TranscriptLine(id: "\(i)", speaker: .you, text: "\(i)")) }
        XCTAssertEqual(ring.lines.map(\.id), ["2", "3", "4"])

        var r2 = TranscriptRing()
        r2.append(TranscriptLine(speaker: .daa, text: "moved", undoID: "u1"))
        r2.append(TranscriptLine(speaker: .daa, text: "would move", undoID: "u2", dryRun: true))
        XCTAssertEqual(r2.undoableLine?.undoID, "u1",
                       "a dry run produced nothing to undo")
    }

    func testChattyKindsAndLoadBearingKindsDoNotOverlap() {
        // The audit tee may drop the chatty ones under backpressure and must
        // never drop the others.
        XCTAssertTrue(AuditRecord.chatty.isDisjoint(with: AuditRecord.loadBearing))
    }
}
