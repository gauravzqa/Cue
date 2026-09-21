import Foundation
import MicroTest
import DaaDockCore

final class CodeIdentityTests: XCTestCase {

    let adhocA = SigningIdentity(cdhash: "f0b85fa55d2f9f21975bc5bfe869bd8666916870", isAdHoc: true)
    let adhocB = SigningIdentity(cdhash: "dcf6284398200938e937c93cbf1655da310229a1", isAdHoc: true)
    let certA = SigningIdentity(cdhash: "aaaa0000", teamID: "ABCDE12345", isAdHoc: false)
    let certB = SigningIdentity(cdhash: "bbbb1111", teamID: "ABCDE12345", isAdHoc: false)

    func testAdHocGrantsDoNotSurviveARebuild() {
        XCTAssertFalse(adhocA.grantsSurviveRebuild)
        XCTAssertTrue(certA.grantsSurviveRebuild)
        // A signature with no team identifier is ad-hoc for our purposes,
        // whatever the flag says.
        XCTAssertFalse(SigningIdentity(cdhash: "x", teamID: nil, isAdHoc: false).grantsSurviveRebuild)
    }

    func testRebuildingAnAdHocAppIsANewAppToTCC() {
        // Verified experimentally: an ad-hoc signature's designated
        // requirement IS the cdhash, and one changed string moves it.
        let v = CodeIdentityCheck.verdict(current: adhocB, previous: adhocA)
        XCTAssertEqual(v, .rebuiltAdHoc(previous: adhocA.cdhash, current: adhocB))
        XCTAssertTrue(v.demandsExplanation)

        let notice = CodeIdentityCheck.explanation(v)!
        XCTAssertEqual(notice.severity, .loud)
        XCTAssertTrue(notice.title.contains("forgotten its permissions"))
        // The user must be told WHICH permissions fail silently.
        XCTAssertTrue(notice.body.contains("Accessibility"))
        XCTAssertTrue(notice.body.contains("no prompt"))
        XCTAssertTrue(notice.body.contains("f0b85fa55d2f9f21"))
        XCTAssertTrue(notice.body.contains("dcf6284398200938"))
        XCTAssertNotNil(notice.remedy)
    }

    func testRebuildingACertSignedAppKeepsItsGrants() {
        let v = CodeIdentityCheck.verdict(current: certB, previous: certA)
        XCTAssertEqual(v, .rebuiltStable(previous: certA.cdhash, current: certB))
        XCTAssertEqual(CodeIdentityCheck.explanation(v)?.severity, .info)
    }

    func testLosingTheSigningIdentityIsCalledOutSeparately() {
        let v = CodeIdentityCheck.verdict(current: adhocA, previous: certA)
        XCTAssertEqual(v, .downgraded(previous: certA, current: adhocA))
        let n = CodeIdentityCheck.explanation(v)!
        XCTAssertEqual(n.severity, .loud)
        XCTAssertTrue(n.body.contains("ABCDE12345"))
    }

    func testUnchangedIsSilent() {
        let v = CodeIdentityCheck.verdict(current: certA, previous: certA)
        XCTAssertEqual(v, .unchanged(certA))
        XCTAssertFalse(v.demandsExplanation)
        XCTAssertNil(CodeIdentityCheck.explanation(v))
    }

    func testFirstRunOfAnAdHocBuildWarnsBeforeAnyGrantIsMade() {
        // Every grant made before a stable identity exists is thrown away on
        // the next build, so say so BEFORE the first prompt, not after.
        let v = CodeIdentityCheck.verdict(current: adhocA, previous: nil)
        XCTAssertEqual(v, .firstRun(adhocA))
        XCTAssertTrue(v.demandsExplanation)
        XCTAssertEqual(CodeIdentityCheck.explanation(v)?.severity, .warning)

        // A properly signed first run says nothing.
        let ok = CodeIdentityCheck.verdict(current: certA, previous: nil)
        XCTAssertFalse(ok.demandsExplanation)
        XCTAssertNil(CodeIdentityCheck.explanation(ok))
    }

    func testAnUnreadableSignatureIsLoudNotIgnored() {
        let n = CodeIdentityCheck.explanation(.unknown("SecCodeCopySelf failed"))!
        XCTAssertEqual(n.severity, .loud)
        XCTAssertTrue(n.body.contains("Treat every permission as unknown"))
    }

    func testStoreRoundTripsAndDetectsTheChange() throws {
        let url = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("daa-identity-\(UUID().uuidString).json")
        defer { try? FileManager.default.removeItem(at: url) }
        let store = IdentityStore(url: url)

        XCTAssertNil(store.load())
        XCTAssertEqual(store.checkAndRecord(current: .success(adhocA)), .firstRun(adhocA))
        XCTAssertEqual(store.load(), adhocA)
        XCTAssertEqual(store.checkAndRecord(current: .success(adhocA)), .unchanged(adhocA))
        XCTAssertEqual(store.checkAndRecord(current: .success(adhocB)),
                       .rebuiltAdHoc(previous: adhocA.cdhash, current: adhocB))
    }

    func testStoreLivesOutsideTheBundle() {
        // Writing into a signed bundle breaks its seal.
        let p = IdentityStore.defaultURL().path
        XCTAssertTrue(p.contains("Application Support"))
        XCTAssertFalse(p.contains(".app/"))
    }
}

final class PythonLocatorTests: XCTestCase {

    func testAnExplicitOverrideWinsOverEverything() {
        let l = PythonLocator { _ in true }
        let first = l.candidates(bundleResources: "/A/Resources", repoRoot: "/repo",
                                 env: ["DAA_PYTHON": "/custom/python"]).first
        XCTAssertEqual(first, "/custom/python")
    }

    func testTheRepoVenvBeatsTheSystemInterpreter() {
        let l = PythonLocator { $0 == "/repo/.venv/bin/python3" || $0 == "/usr/bin/python3" }
        XCTAssertEqual(l.locate(bundleResources: nil, repoRoot: "/repo", env: [:]),
                       "/repo/.venv/bin/python3")
    }

    func testTheBundledInterpreterBeatsTheRepoVenv() {
        let l = PythonLocator { _ in true }
        XCTAssertEqual(l.locate(bundleResources: "/A/Resources", repoRoot: "/repo", env: [:]),
                       "/A/Resources/python/bin/python3")
    }

    func testSystemPythonIsLast() {
        let c = PythonLocator { _ in true }.candidates(bundleResources: nil, repoRoot: "/r", env: [:])
        XCTAssertEqual(c.last, "/usr/bin/python3")
    }

    func testNoInterpreterIsAnHonestNil() {
        XCTAssertNil(PythonLocator { _ in false }.locate(bundleResources: nil, repoRoot: "/r", env: [:]))
    }

    func testUnbufferedIsNotOptional() {
        // Python's stdout is FULLY BUFFERED on a pipe. Without -u the dock
        // waits forever for a `ready` frame sitting in the child's buffer.
        XCTAssertEqual(PythonLocator.arguments().first, "-u")
        let env = PythonLocator.environment(base: ["PATH": "/bin"], repoRoot: "/r")
        XCTAssertEqual(env["PYTHONUNBUFFERED"], "1")
        XCTAssertEqual(env["PYTHONSTARTUP"], "", "a startup file would write to fd 1")
        XCTAssertEqual(env["PYTHONINSPECT"], "")
        XCTAssertEqual(env["PATH"], "/bin")
    }
}

final class RestartPolicyTests: XCTestCase {

    func testBackoffDoublesAndCaps() {
        var p = RestartPolicy()
        var delays: [TimeInterval] = []
        for i in 0..<5 {
            guard case .restart(let after, let attempt) = p.childDied(at: Double(i), ranFor: 0)
            else { return XCTFail("should restart") }
            XCTAssertEqual(attempt, i + 1)
            delays.append(after)
        }
        XCTAssertEqual(delays, [1, 2, 4, 8, 16])
    }

    func testItStopsRatherThanLoopingForever() {
        var p = RestartPolicy()
        for i in 0..<5 { _ = p.childDied(at: Double(i), ranFor: 0) }
        guard case .giveUp(let reason) = p.childDied(at: 6, ranFor: 0) else {
            return XCTFail("should give up")
        }
        XCTAssertTrue(reason.contains("6 times"))
    }

    func testAChildThatRanFineResetsTheBackoff() {
        var p = RestartPolicy()
        for i in 0..<4 { _ = p.childDied(at: Double(i), ranFor: 0) }
        // One crash an hour must not creep to the 30s cap and stay there.
        guard case .restart(let after, let attempt) = p.childDied(at: 3600, ranFor: 3599) else {
            return XCTFail("should restart")
        }
        XCTAssertEqual(after, 1)
        XCTAssertEqual(attempt, 1)
    }

    func testFailuresOutsideTheWindowDoNotCount() {
        var p = RestartPolicy(window: 10)
        for i in 0..<5 { _ = p.childDied(at: Double(i), ranFor: 0) }
        guard case .restart = p.childDied(at: 100, ranFor: 0) else {
            return XCTFail("stale failures should have aged out")
        }
    }

    func testCapIsThirtySeconds() {
        var p = RestartPolicy(maxFailuresInWindow: 100)
        var last: TimeInterval = 0
        for i in 0..<12 {
            if case .restart(let after, _) = p.childDied(at: Double(i), ranFor: 0) { last = after }
        }
        XCTAssertEqual(last, 30)
    }
}
