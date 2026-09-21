import Foundation

// A ~120-line stand-in for XCTest.
//
// Command Line Tools ship no XCTest and no swift-testing, so `swift test` does
// not work on a machine without Xcode -- which is this machine, and which the
// plan did not anticipate. "A UI you cannot test at all is a UI you cannot
// change", so rather than skip the tests, this provides the small slice of the
// XCTest API the suite actually uses.
//
// The assertion names are XCTest's on purpose: when Xcode is installed, moving
// to a real `.testTarget` is deleting this file and changing one import.

public struct Failure: Sendable {
    public var message: String
    public var file: String
    public var line: Int
}

public final class TestRun: @unchecked Sendable {
    public static let shared = TestRun()
    private let lock = NSLock()
    private var failures: [Failure] = []
    public private(set) var assertions = 0
    var currentTest = ""

    func record(_ message: String, _ file: StaticString, _ line: UInt) {
        lock.lock(); defer { lock.unlock() }
        failures.append(Failure(message: "\(currentTest): \(message)",
                                file: "\(file)", line: Int(line)))
    }

    func counted() {
        lock.lock(); defer { lock.unlock() }
        assertions += 1
    }

    var failureCount: Int { lock.lock(); defer { lock.unlock() }; return failures.count }
    var allFailures: [Failure] { lock.lock(); defer { lock.unlock() }; return failures }
}

@inline(__always) private func fail(_ m: String, _ f: StaticString, _ l: UInt) {
    TestRun.shared.record(m, f, l)
}
@inline(__always) private func note() { TestRun.shared.counted() }

// MARK: - the XCTest surface this suite uses

open class XCTestCase {
    public init() {}
    open func setUp() {}
    open func tearDown() {}
}

public func XCTAssertTrue(_ e: @autoclosure () throws -> Bool, _ m: String = "",
                          file: StaticString = #filePath, line: UInt = #line) {
    note()
    guard let v = try? e() else { return fail("threw. \(m)", file, line) }
    if !v { fail("expected true. \(m)", file, line) }
}

public func XCTAssertFalse(_ e: @autoclosure () throws -> Bool, _ m: String = "",
                           file: StaticString = #filePath, line: UInt = #line) {
    note()
    guard let v = try? e() else { return fail("threw. \(m)", file, line) }
    if v { fail("expected false. \(m)", file, line) }
}

public func XCTAssertEqual<T: Equatable>(_ a: @autoclosure () throws -> T,
                                         _ b: @autoclosure () throws -> T, _ m: String = "",
                                         file: StaticString = #filePath, line: UInt = #line) {
    note()
    guard let x = try? a(), let y = try? b() else { return fail("threw. \(m)", file, line) }
    if x != y { fail("\(x) != \(y). \(m)", file, line) }
}

public func XCTAssertEqual(_ a: @autoclosure () throws -> Double,
                           _ b: @autoclosure () throws -> Double,
                           accuracy: Double, _ m: String = "",
                           file: StaticString = #filePath, line: UInt = #line) {
    note()
    guard let x = try? a(), let y = try? b() else { return fail("threw. \(m)", file, line) }
    if abs(x - y) > accuracy { fail("\(x) != \(y) ±\(accuracy). \(m)", file, line) }
}

public func XCTAssertNotEqual<T: Equatable>(_ a: @autoclosure () throws -> T,
                                            _ b: @autoclosure () throws -> T, _ m: String = "",
                                            file: StaticString = #filePath, line: UInt = #line) {
    note()
    guard let x = try? a(), let y = try? b() else { return fail("threw. \(m)", file, line) }
    if x == y { fail("both \(x). \(m)", file, line) }
}

public func XCTAssertNil<T>(_ e: @autoclosure () throws -> T?, _ m: String = "",
                            file: StaticString = #filePath, line: UInt = #line) {
    note()
    do {
        if let v = try e() { fail("expected nil, got \(v). \(m)", file, line) }
    } catch { fail("threw \(error). \(m)", file, line) }
}

public func XCTAssertNotNil<T>(_ e: @autoclosure () throws -> T?, _ m: String = "",
                               file: StaticString = #filePath, line: UInt = #line) {
    note()
    do {
        if try e() == nil { fail("expected non-nil. \(m)", file, line) }
    } catch { fail("threw \(error). \(m)", file, line) }
}

public func XCTAssertLessThanOrEqual<T: Comparable>(_ a: @autoclosure () -> T,
                                                    _ b: @autoclosure () -> T, _ m: String = "",
                                                    file: StaticString = #filePath, line: UInt = #line) {
    note()
    let (x, y) = (a(), b())
    if !(x <= y) { fail("\(x) > \(y). \(m)", file, line) }
}

public func XCTAssertGreaterThan<T: Comparable>(_ a: @autoclosure () -> T,
                                                _ b: @autoclosure () -> T, _ m: String = "",
                                                file: StaticString = #filePath, line: UInt = #line) {
    note()
    let (x, y) = (a(), b())
    if !(x > y) { fail("\(x) <= \(y). \(m)", file, line) }
}

public func XCTAssertThrowsError<T>(_ e: @autoclosure () throws -> T, _ m: String = "",
                                    file: StaticString = #filePath, line: UInt = #line,
                                    _ handler: (any Error) -> Void = { _ in }) {
    note()
    do { _ = try e(); fail("expected a throw. \(m)", file, line) }
    catch { handler(error) }
}

public func XCTFail(_ m: String = "", file: StaticString = #filePath, line: UInt = #line) {
    note()
    fail(m.isEmpty ? "XCTFail" : m, file, line)
}

// MARK: - the runner

public struct Test: Sendable {
    public let name: String
    public let body: @Sendable () throws -> Void
    public init(_ name: String, _ body: @escaping @Sendable () throws -> Void) {
        self.name = name
        self.body = body
    }
}

public enum Runner {
    /// Runs everything, prints a report, and returns the process exit code.
    public static func run(_ suites: [(String, [Test])], filter: String? = nil) -> Int32 {
        var ran = 0
        for (suite, tests) in suites {
            var printedSuite = false
            for t in tests {
                let full = "\(suite).\(t.name)"
                if let filter, !full.localizedCaseInsensitiveContains(filter) { continue }
                if !printedSuite { print("\n\(suite)"); printedSuite = true }
                TestRun.shared.currentTest = full
                let before = TestRun.shared.failureCount
                do { try t.body() }
                catch { TestRun.shared.record("threw \(error)", #filePath, #line) }
                ran += 1
                let ok = TestRun.shared.failureCount == before
                print("  \(ok ? "ok  " : "FAIL") \(t.name)")
            }
        }
        let failures = TestRun.shared.allFailures
        print("\n\(ran) tests, \(TestRun.shared.assertions) assertions, \(failures.count) failures")
        for f in failures {
            print("  FAIL \(f.message)\n       \(f.file):\(f.line)")
        }
        return failures.isEmpty ? 0 : 1
    }
}
