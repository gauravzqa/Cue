import Foundation
import CoreGraphics
import MicroTest
import DaaDockCore

/// How the approval card presents a decision Python already made: the stakes
/// banner, the order of the flags, the message spotlight, and where the panel
/// goes on a screen. See `ApprovalPresentation.swift`.
final class PresentationTests: XCTestCase {

    func card(dryRun: Bool?, consequences: [String: String] = ["send": "sending this text: 'on my way'"],
              extra: [String: JSONValue] = [:]) -> ApprovalCard {
        var p: [String: JSONValue] = [
            "tool": .string("run_applescript"),
            "phrase": .string("run a script that sends a message to Alex"),
            "args": .array([
                .object(["key": .string("script"), "isProgram": .bool(true),
                         "value": .string("tell application \"Messages\"\nend tell")]),
                .object(["key": .string("recipient"), "isProgram": .bool(false), "value": .string("Alex")]),
            ]),
            "consequences": .object(consequences.mapValues { .string($0) }),
        ]
        if let dryRun { p["dryRun"] = .bool(dryRun) }
        for (k, v) in extra { p[k] = v }
        return ApprovalCard(id: "cf_1", params: .object(p))!
    }

    // MARK: - defect 1: live is a presence, and the louder of the two

    func testLiveAndDryRunCardsCarryDifferentNonEmptyBanners() {
        let live = card(dryRun: false).stakes
        let dry = card(dryRun: true).stakes
        XCTAssertFalse(live.title.isEmpty, "a live card must SAY it is live, not merely lack a dry-run banner")
        XCTAssertFalse(live.bannerText.isEmpty)
        XCTAssertFalse(dry.bannerText.isEmpty)
        XCTAssertNotEqual(live.bannerText, dry.bannerText)
        XCTAssertNotEqual(live.symbol, dry.symbol)
        XCTAssertEqual(live.kind, .live)
        XCTAssertEqual(dry.kind, .dryRun)
        // An affirmative statement, not a negation of the dry-run one.
        XCTAssertTrue(live.title.lowercased().contains("really"))
        XCTAssertFalse(live.title.lowercased().contains("dry run"))
    }

    func testLiveApproveControlIsMateriallyDifferentFromDryRun() {
        let live = card(dryRun: false).stakes
        let dry = card(dryRun: true).stakes
        XCTAssertEqual(live.emphasis, .serious)
        XCTAssertEqual(dry.emphasis, .calm)
        XCTAssertNotEqual(live.approveLabel, dry.approveLabel)
        XCTAssertTrue(live.approveLabel.contains("for real"))
        XCTAssertNotEqual(live.approveAccessibilityLabel, dry.approveAccessibilityLabel)
        // Both are still a hold. No wording change may imply a click works.
        XCTAssertTrue(live.approveLabel.contains("hold"))
        XCTAssertTrue(dry.approveLabel.contains("hold"))
    }

    func testAnAbsentDryRunIsPresentedAsReal() {
        // If the card cannot tell, it says the thing is real: over-warning on
        // a dry run costs a moment, under-warning on a live one costs a text.
        XCTAssertEqual(card(dryRun: nil).stakes, .live)
    }

    // MARK: - consequence order

    func testConsequencesSortMostDestructiveFirstNotAlphabetically() {
        let c = card(dryRun: false, consequences: [
            "delete": "moving 214 items to the Trash",
            "empty_trash": "emptying the Trash",
            "recursive": "including 9 subfolders",
            "unrecoverable": "this cannot be undone from daa",
        ])
        XCTAssertEqual(c.consequences.map(\.key), ["unrecoverable", "empty_trash", "delete", "recursive"])
    }

    func testAnUnknownConsequenceIsNotSortedBelowInformationalOnes() {
        let c = card(dryRun: false, consequences: [
            "review": "you will see the whole script",
            "zzz_new_thing": "something a new resolver computed",
            "delete": "deleting a file",
        ])
        XCTAssertEqual(c.consequences.map(\.key), ["delete", "zzz_new_thing", "review"])
    }

    func testAnExplicitOrderFromPythonWins() {
        let c = card(dryRun: false,
                     consequences: ["delete": "d", "unrecoverable": "u", "send": "s"],
                     extra: ["consequenceOrder": .array([.string("send"), .string("delete")])])
        XCTAssertEqual(c.consequences.map(\.key), ["send", "delete", "unrecoverable"])
    }

    // MARK: - the message itself

    func testTheMessageAndItsRecipientAreLiftedOutOfTheFlags() {
        let c = card(dryRun: false)
        let m = c.messageSpotlight
        XCTAssertNotNil(m)
        XCTAssertEqual(m?.body, "on my way")
        XCTAssertEqual(m?.recipient, "Alex")
        XCTAssertEqual(m?.consequenceKey, "send")
        XCTAssertTrue(c.flaggedConsequences.isEmpty, "shown once, in the spotlight, not twice")
        // The disclosure record still carries Python's text verbatim.
        XCTAssertTrue(c.fullDisclosureText.contains("sending this text: 'on my way'"))
    }

    func testAnExplicitMessageFieldIsUsedAndDisclosed() {
        let c = card(dryRun: false, consequences: [:],
                     extra: ["message": .object(["to": .string("Sam"), "body": .string("running late")])])
        XCTAssertEqual(c.messageSpotlight?.recipient, "Sam")
        XCTAssertEqual(c.messageSpotlight?.body, "running late")
        XCTAssertTrue(c.fullDisclosureText.contains("running late"))
    }

    func testNoSpotlightWhenNothingIsSent() {
        let c = card(dryRun: false, consequences: ["delete": "deleting report.pdf"])
        XCTAssertNil(c.messageSpotlight)
        XCTAssertEqual(c.flaggedConsequences.map(\.key), ["delete"])
    }

    // MARK: - defect 3: the panel is clamped to the screen

    /// A 13-inch MacBook Air on "Larger Text": 1024×665 pt, 24 pt menu bar,
    /// so visibleFrame is 1024×641 from the bottom.
    let largerText = CGRect(x: 0, y: 0, width: 1024, height: 641)

    func assertInside(_ f: CGRect, _ v: CGRect, _ why: String, line: UInt = #line) {
        XCTAssertTrue(f.minX >= v.minX && f.maxX <= v.maxX, "\(why): x \(f) outside \(v)", line: line)
        XCTAssertTrue(f.minY >= v.minY && f.maxY <= v.maxY, "\(why): y \(f) outside \(v)", line: line)
    }

    func testATallCardOnASmallScreenIsClampedAndStaysBelowTheMenuBar() {
        let f = PanelPlacement.clampedFrame(content: CGSize(width: 560, height: 720), visible: largerText)
        assertInside(f, largerText, "the whole panel, footer included, is on screen")
        XCTAssertLessThanOrEqual(f.maxY, 641 - PanelPlacement.margin)   // under the menu bar
        XCTAssertLessThanOrEqual(f.height, 641 - 2 * PanelPlacement.margin)
        XCTAssertEqual(Double(f.width), 560, accuracy: 0.001)
    }

    func testContentFarTallerThanTheScreenIsClamped() {
        let f = PanelPlacement.clampedFrame(content: CGSize(width: 560, height: 10_000), visible: largerText)
        assertInside(f, largerText, "10 000 pt of content")
        XCTAssertEqual(Double(f.height), Double(641 - 2 * PanelPlacement.margin), accuracy: 0.001)
    }

    func testTheLiftNeverPushesTheTopUnderTheMenuBar() {
        // Fits, but centre + lift would poke above the visible frame.
        let f = PanelPlacement.clampedFrame(content: CGSize(width: 560, height: 600), visible: largerText)
        assertInside(f, largerText, "lifted card")
    }

    func testAMenuBarAndDockOnASecondaryScreenAreRespected() {
        // A display to the right with its own menu bar (top) and a Dock (bottom).
        let v = CGRect(x: 1440, y: 70, width: 1280, height: 730)
        let f = PanelPlacement.clampedFrame(content: CGSize(width: 560, height: 900), visible: v)
        assertInside(f, v, "secondary screen")
    }

    func testACardThatFitsKeepsItsSizeAndSitsCentredSlightlyHigh() {
        let big = CGRect(x: 0, y: 0, width: 1728, height: 1079)
        let f = PanelPlacement.clampedFrame(content: CGSize(width: 560, height: 480), visible: big)
        XCTAssertEqual(Double(f.width), 560, accuracy: 0.001)
        XCTAssertEqual(Double(f.height), 480, accuracy: 0.001)
        XCTAssertEqual(Double(f.midX), Double(big.midX), accuracy: 0.001)
        XCTAssertEqual(Double(f.midY), Double(big.midY + PanelPlacement.lift), accuracy: 0.001)
    }

    func testAScreenNarrowerThanTheCardClampsWidthToo() {
        let v = CGRect(x: 0, y: 0, width: 500, height: 400)
        let f = PanelPlacement.clampedFrame(content: CGSize(width: 560, height: 720), visible: v)
        assertInside(f, v, "narrow screen")
    }
}
