import Foundation
import MicroTest
import DaaDockCore

/// The approval card is the most safety-critical screen in the product. These
/// tests are the specification for it: a card someone learns to click through
/// is worse than no card, because it manufactures a consent record for a
/// decision nobody made.
final class ApprovalTests: XCTestCase {

    let script = """
    tell application "Messages"
      send "on my way" to buddy "Alex"
    end tell
    """

    func makeFrame(expiresInMs: Double = 90_000) -> JSONValue {
        .object([
            "tool": .string("run_applescript"),
            "tier": .string("CONFIRM_VISUAL"),
            "reason": .string("unrecoverable and not explicitly requested"),
            "phrase": .string("run a script that sends a message to Alex"),
            "verb": .string("run"),
            "explicit": .bool(false),
            "dryRun": .bool(true),
            "targets": .array([.string("a script of 3 lines")]),
            "args": .array([
                .object(["key": .string("script"), "isProgram": .bool(true), "value": .string(script)]),
                .object(["key": .string("recipient"), "isProgram": .bool(false), "value": .string("Alex")]),
            ]),
            "consequences": .object(["send": .string("sending this text: 'on my way'")]),
            "assessment": .object([
                "blastRadius": .number(2.4), "unrecoverable": .number(0.81),
                "explicitlyRequested": .number(0.19), "targetConfidence": .string("probable"),
                "confidence": .number(0.77), "synthetic": .bool(false),
            ]),
            "expiresInMs": .number(expiresInMs),
        ])
    }

    func makeCard(expiresInMs: Double = 90_000) -> ApprovalCard {
        ApprovalCard(id: "cf_3b91c0ad", params: makeFrame(expiresInMs: expiresInMs))!
    }

    // MARK: - the payload

    func testParsesEveryFieldThatChangesWhatConsentMeans() {
        let c = makeCard()
        XCTAssertEqual(c.tool, "run_applescript")
        XCTAssertEqual(c.phrase, "run a script that sends a message to Alex")
        XCTAssertFalse(c.explicit)
        XCTAssertTrue(c.dryRun)
        XCTAssertEqual(c.consequences.map(\.text), ["sending this text: 'on my way'"])
        XCTAssertEqual(c.assessment.blastRadius, 2.4, accuracy: 0.001)
        XCTAssertFalse(c.assessment.synthetic)
        XCTAssertEqual(c.expiresIn, 90)
    }

    func testScriptArgumentsSortFirst() {
        // `_SCRIPT_KEYS` exists because those args are the reason the tier
        // exists. They are labelled THIS RUNS and they go first.
        XCTAssertEqual(makeCard().arguments.map(\.key), ["script", "recipient"])
        XCTAssertEqual(makeCard().scriptArguments.map(\.key), ["script"])
        XCTAssertEqual(makeCard().otherArguments.map(\.key), ["recipient"])
    }

    func testNothingIsElidedAndNothingIsSummarised() {
        // Risk 3 in the plan: design pressure pushes toward summarising the
        // script. Given an N-line script and M args, every line and every arg
        // must be present in what the card renders.
        let big = (1...400).map { "line \($0) -- do something \($0)" }.joined(separator: "\n")
        let frame = JSONValue.object([
            "tool": .string("run_shell"), "phrase": .string("run a 400 line script"),
            "explicit": .bool(true), "dryRun": .bool(false),
            "targets": .array([.string("t1"), .string("t2")]),
            "args": .array([
                .object(["key": .string("script"), "isProgram": .bool(true), "value": .string(big)]),
                .object(["key": .string("cwd"), "isProgram": .bool(false), "value": .string("/tmp")]),
            ]),
        ])
        let card = ApprovalCard(id: "x", params: frame)!
        XCTAssertEqual(card.scriptArguments[0].lines.count, 400)
        let disclosed = card.fullDisclosureText
        for n in 1...400 {
            XCTAssertTrue(disclosed.contains("line \(n) -- do something \(n)"), "line \(n) elided")
        }
        XCTAssertTrue(disclosed.contains("/tmp"))
        XCTAssertTrue(disclosed.contains("t1"))
        XCTAssertTrue(disclosed.contains("t2"))
    }

    func testUnreadableFrameProducesNoCardAtAll() {
        // A card assembled from a frame we cannot read in full would still
        // LOOK like a card, and a card someone approves is a consent record.
        XCTAssertNil(ApprovalCard(id: "", params: makeFrame()))
        XCTAssertNil(ApprovalCard(id: "x", params: .object(["phrase": .string("p")])))       // no tool
        XCTAssertNil(ApprovalCard(id: "x", params: .object(["tool": .string("t")])))          // no phrase
        XCTAssertNil(ApprovalCard(id: "x", params: .object(["tool": .string("t"), "phrase": .string("")])))
    }

    func testMissingFieldsDefaultToTheWorstCase() {
        let bare = ApprovalCard(id: "x", params: .object([
            "tool": .string("t"), "phrase": .string("do a thing"),
        ]))!
        XCTAssertFalse(bare.explicit, "absent explicit must show the 'I inferred this' flag")
        XCTAssertTrue(bare.assessment.synthetic, "an unreadable assessment is not a mild one")
        XCTAssertEqual(bare.assessment.blastRadius, 3)
        XCTAssertEqual(bare.assessment.confidence, 0)
    }

    func testExpiryIsClampedNotTrusted() {
        XCTAssertEqual(makeCard(expiresInMs: 36_000_000).expiresIn, 300)
        XCTAssertEqual(makeCard(expiresInMs: 1).expiresIn, 5)
    }

    func testIsProgramFallsBackToScriptKeys() {
        let f = JSONValue.object([
            "tool": .string("t"), "phrase": .string("p"),
            "args": .array([.object(["key": .string("applescript"), "value": .string("x")])]),
        ])
        XCTAssertTrue(ApprovalCard(id: "x", params: f)!.arguments[0].isProgram)
    }

    // MARK: - the gate

    func testCardIsInertForItsFirstFourHundredMilliseconds() {
        // Kills the click-through case where a card appears under a cursor
        // already descending.
        var g = ApprovalGate(card: makeCard(), presentedAt: 0).scrolledToEnd()
        XCTAssertEqual(g.block(at: 0.0), .inert(remaining: 0.4))
        XCTAssertEqual(g.block(at: 0.399), .inert(remaining: 0.4 - 0.399))
        XCTAssertNil(g.block(at: 0.401))

        // A hold started during the inert window does not start.
        g = g.beginningHold(at: 0.1)
        XCTAssertNil(g.holdStartedAt)
        g = g.endingHold(at: 1.0)
        XCTAssertNil(g.resolution)
    }

    func testApprovalRequiresACompletedHold() {
        var g = ApprovalGate(card: makeCard(), presentedAt: 0).scrolledToEnd()
        g = g.beginningHold(at: 1.0)
        XCTAssertEqual(g.holdProgress(at: 1.3), 0.5, accuracy: 0.001)

        // Released early: not an approval, and not a refusal either.
        var early = g.endingHold(at: 1.4)
        XCTAssertNil(early.resolution)
        XCTAssertNil(early.holdStartedAt)

        // A second, complete hold approves.
        early = early.beginningHold(at: 2.0).endingHold(at: 2.7)
        XCTAssertEqual(early.resolution, .held)
        XCTAssertTrue(early.resolution!.granted)
    }

    func testHoldResolvesOnItsOwnAtFullFill() {
        var g = ApprovalGate(card: makeCard(), presentedAt: 0).scrolledToEnd()
        g = g.beginningHold(at: 1.0)
        XCTAssertNil(g.ticking(at: 1.5).resolution)
        XCTAssertEqual(g.ticking(at: 1.6).resolution, .held)
    }

    func testYouCannotApproveWhatYouHaveNotScrolledPast() {
        var g = ApprovalGate(card: makeCard(), presentedAt: 0)
        XCTAssertEqual(g.block(at: 1.0), .unread)
        XCTAssertFalse(g.canApprove(at: 1.0))
        g = g.beginningHold(at: 1.0).endingHold(at: 2.0)
        XCTAssertNil(g.resolution, "a hold must not count while script text is below the fold")

        g = g.scrolledToEnd()
        XCTAssertTrue(g.canApprove(at: 1.0))
    }

    func testACardWithNoProgramHasNothingToScrollPast() {
        let f = JSONValue.object(["tool": .string("move_file"), "phrase": .string("move report.pdf")])
        let g = ApprovalGate(card: ApprovalCard(id: "x", params: f)!, presentedAt: 0)
        XCTAssertTrue(g.scriptFullySeen, "do not invent friction that teaches nothing")
        XCTAssertTrue(g.canApprove(at: 1.0))
    }

    func testSilenceIsARefusalAndIsAuditedAsATimeout() {
        var g = ApprovalGate(card: makeCard(), presentedAt: 0).scrolledToEnd()
        XCTAssertEqual(g.countdownText(at: 2), "1:28")
        XCTAssertFalse(g.canApprove(at: 91))
        XCTAssertEqual(g.block(at: 91), .expired)
        g = g.ticking(at: 90.1)
        XCTAssertEqual(g.resolution, .timedOut)
        XCTAssertFalse(g.resolution!.granted)
        XCTAssertEqual(g.resolution!.wireReason, "timeout")
    }

    func testAHoldStraddlingExpiryDoesNotApprove() {
        var g = ApprovalGate(card: makeCard(), presentedAt: 0).scrolledToEnd()
        g = g.beginningHold(at: 89.8)
        g = g.ticking(at: 90.01)
        XCTAssertEqual(g.resolution, .timedOut)
        // And the release afterwards cannot revive it.
        XCTAssertEqual(g.endingHold(at: 90.5).resolution, .timedOut)
    }

    func testEveryNonHoldOutcomeIsARefusal() {
        for outcome: ApprovalOutcome in [.cancelled, .escaped, .timedOut,
                                         .withdrawn("child exit"), .brainStopped, .unreadable] {
            XCTAssertFalse(outcome.granted, "\(outcome) must not grant")
        }
        XCTAssertTrue(ApprovalOutcome.held.granted)
    }

    func testFirstResolutionWins() {
        let g = ApprovalGate(card: makeCard(), presentedAt: 0).scrolledToEnd()
        let cancelled = g.resolving(.cancelled)
        XCTAssertEqual(cancelled.resolving(.held).resolution, .cancelled)
    }

    // MARK: - single-use tokens

    func testATokenIsAdmittedOnceAndConsumedOnce() {
        let pending = PendingApprovals()
        let card = makeCard()
        XCTAssertEqual(pending.admit(card), .admitted)
        XCTAssertEqual(pending.admit(card), .duplicate, "the same card must not open twice")
        XCTAssertNotNil(pending.consume(card.id))
        XCTAssertNil(pending.consume(card.id), "exactly one confirm.result per confirm.request")
        XCTAssertEqual(pending.admit(card), .replayed, "a consumed token can never be reused")
        XCTAssertTrue(pending.isEmpty)
    }

    func testConsumingAnUnknownTokenYieldsNothing() {
        XCTAssertNil(PendingApprovals().consume("cf_forged"))
    }

    // MARK: - what the wire carries back

    func testResponseFrameForEachOutcome() throws {
        func reply(_ o: ApprovalOutcome) throws -> JSONValue {
            let f = Frame.response(id: "cf_1", outcome: .ok(.object([
                "granted": .bool(o.granted), "reason": .string(o.wireReason),
            ])))
            return try FrameCodec.decode(line: String(data: FrameCodec.encode(f), encoding: .utf8)!).params
        }
        XCTAssertEqual(try reply(.held)["granted"]?.boolValue, true)
        XCTAssertEqual(try reply(.timedOut)["granted"]?.boolValue, false)
        XCTAssertEqual(try reply(.timedOut)["reason"]?.stringValue, "timeout")
        XCTAssertEqual(try reply(.brainStopped)["granted"]?.boolValue, false)
    }
}

/// The boundary between the wire and the screen.
final class ConfirmRouterTests: XCTestCase {

    let good = JSONValue.object([
        "tool": .string("run_applescript"),
        "phrase": .string("run a script that sends a message to Alex"),
        "args": .array([.object([
            "key": .string("script"), "isProgram": .bool(true),
            "value": .string("tell application \"Messages\"\nend tell"),
        ])]),
    ])

    func testAGoodFrameIsPresented() {
        let r = ConfirmRouter.admit(id: "cf_1", params: good,
                                    pending: PendingApprovals(), cardAlreadyOpen: false)
        guard case .present(let card) = r else { return XCTFail("expected a card, got \(r)") }
        XCTAssertEqual(card.id, "cf_1")
    }

    func testAnUnreadableFrameIsRefusedNotShown() {
        let r = ConfirmRouter.admit(id: "cf_1", params: .object(["tool": .string("t")]),
                                    pending: PendingApprovals(), cardAlreadyOpen: false)
        guard case .refuse(let id, let outcome, _) = r else { return XCTFail("expected a refusal") }
        XCTAssertEqual(id, "cf_1")
        XCTAssertEqual(outcome, .unreadable)
        XCTAssertFalse(outcome.granted)
    }

    func testAReplayedTokenIsDroppedWithNoSecondAnswer() {
        // Exactly one confirm.result per confirm.request. Answering a replay
        // would produce two, and the second would be an answer nobody gave.
        let pending = PendingApprovals()
        guard case .present(let card) = ConfirmRouter.admit(
            id: "cf_1", params: good, pending: pending, cardAlreadyOpen: false)
        else { return XCTFail("setup") }
        _ = pending.consume(card.id)

        let again = ConfirmRouter.admit(id: "cf_1", params: good,
                                        pending: pending, cardAlreadyOpen: false)
        guard case .drop = again else { return XCTFail("a replay must be silent, got \(again)") }
    }

    func testTheSameTokenArrivingTwiceDoesNotOpenTwoCards() {
        let pending = PendingApprovals()
        _ = ConfirmRouter.admit(id: "cf_1", params: good, pending: pending, cardAlreadyOpen: false)
        let second = ConfirmRouter.admit(id: "cf_1", params: good,
                                         pending: pending, cardAlreadyOpen: false)
        guard case .drop = second else { return XCTFail("expected a drop, got \(second)") }
    }

    func testASecondCardIsRefusedRatherThanStacked() {
        // Stacking would let a click aimed at one card land on the other, and
        // queueing would run the queued card's countdown while it was invisible.
        let pending = PendingApprovals()
        let r = ConfirmRouter.admit(id: "cf_2", params: good,
                                    pending: pending, cardAlreadyOpen: true)
        guard case .refuse(let id, let outcome, _) = r else { return XCTFail("expected a refusal") }
        XCTAssertEqual(id, "cf_2")
        XCTAssertFalse(outcome.granted)
        XCTAssertTrue(pending.isEmpty, "the refused token must not stay open")
    }

    /// The exact frame `ui/tools/fake-bridge` emits, byte for byte. If the two
    /// sides ever disagree about the shape of `confirm.request`, this is where
    /// it shows up rather than in a card that silently renders half an action.
    func testTheFakeBridgeFrameDecodesCompletely() throws {
        let line = #"""
        {"t":"req","id":"cf_deadbeef","m":"confirm.request","p":{"tool":"run_applescript","tier":"CONFIRM_VISUAL","reason":"unrecoverable and not explicitly requested","phrase":"run a script that sends a message to Alex","verb":"run","explicit":false,"dryRun":true,"targets":["a script of 3 lines"],"args":[{"key":"script","isProgram":true,"value":"tell application \"Messages\"\n  send \"on my way\" to buddy \"Alex\"\nend tell"},{"key":"recipient","isProgram":false,"value":"Alex"}],"consequences":{"send":"sending this text: 'on my way'"},"assessment":{"blastRadius":2.4,"unrecoverable":0.81,"explicitlyRequested":0.19,"targetConfidence":"probable","confidence":0.77,"synthetic":false},"expiresInMs":90000}}
        """#
        let frame = try FrameCodec.decode(line: line)
        guard case .request(let id, let method, let params) = frame else {
            return XCTFail("expected a request")
        }
        XCTAssertEqual(method, Method.confirmRequest)
        let card = ApprovalCard(id: id, params: params)
        XCTAssertNotNil(card)
        XCTAssertEqual(card?.scriptArguments.first?.lines.count, 3)
        XCTAssertEqual(card?.consequences.count, 1)
        XCTAssertEqual(card?.expiresIn, 90)
        XCTAssertFalse(card?.explicit ?? true)
        XCTAssertTrue(card?.dryRun ?? false)
    }
}
