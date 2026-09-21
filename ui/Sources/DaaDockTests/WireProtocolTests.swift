import Foundation
import MicroTest
import DaaDockCore

final class WireProtocolTests: XCTestCase {

    // MARK: envelope

    func testDecodesEachFrameKind() throws {
        let req = try FrameCodec.decode(line: #"{"t":"req","id":"r7","m":"undo.last","p":{}}"#)
        XCTAssertEqual(req, .request(id: "r7", method: "undo.last", params: .object([:])))

        let ev = try FrameCodec.decode(line: #"{"t":"ev","m":"state","p":{"phase":"thinking"}}"#)
        XCTAssertEqual(ev.method, "state")
        XCTAssertEqual(ev.params["phase"]?.stringValue, "thinking")

        let ok = try FrameCodec.decode(line: #"{"t":"res","id":"r7","ok":true,"p":{"granted":true}}"#)
        XCTAssertEqual(ok, .response(id: "r7", outcome: .ok(.object(["granted": .bool(true)]))))

        let err = try FrameCodec.decode(line: #"{"t":"res","id":"r7","ok":false,"err":{"code":"nope","message":"no"}}"#)
        XCTAssertEqual(err, .response(id: "r7", outcome: .error(code: "nope", message: "no")))
    }

    func testRoundTrips() throws {
        let frames: [Frame] = [
            .request(id: "a", method: "session.hello", params: .object(["proto": .number(1)])),
            .event(method: "speak", params: .object(["text": .string("on my way")])),
            .response(id: "b", outcome: .ok(.object(["granted": .bool(false)]))),
            .response(id: "c", outcome: .error(code: "x", message: "y")),
        ]
        for f in frames {
            let data = try FrameCodec.encode(f)
            let line = String(data: data, encoding: .utf8)!
            XCTAssertTrue(line.hasSuffix("\n"))
            XCTAssertEqual(try FrameCodec.decode(line: line), f)
        }
    }

    func testMultilineScriptSurvivesEncodingAsOneLine() throws {
        let script = "tell application \"Messages\"\n  send \"hi\" to buddy \"Alex\"\nend tell"
        let f = Frame.event(method: "x", params: .object(["script": .string(script)]))
        let data = try FrameCodec.encode(f)
        // Exactly one newline, the terminator.
        XCTAssertEqual(data.filter { $0 == 0x0A }.count, 1)
        let back = try FrameCodec.decode(line: String(data: data, encoding: .utf8)!)
        XCTAssertEqual(back.params["script"]?.stringValue, script)
    }

    // MARK: malformed input must never kill the reader

    func testMalformedLinesThrowRatherThanCrash() {
        let bad = [
            "", "   ", "not json", "[1,2,3]", "\"a string\"", "null", "42",
            #"{"t":"req","m":"x"}"#,                 // no id
            #"{"t":"req","id":"a"}"#,                // no method
            #"{"t":"ev"}"#,                          // no method
            #"{"t":"res"}"#,                         // no id
            #"{"t":"bogus","m":"x"}"#,
            #"{"m":"x"}"#,                           // no t
            #"{"t":"req","id":"","m":"x"}"#,         // empty id
        ]
        for line in bad {
            XCTAssertThrowsError(try FrameCodec.decode(line: line), "should reject: \(line)")
        }
    }

    func testResponseWithoutOkIsNotAnApproval() throws {
        // Absent `ok` must never read as success. The only thing a response
        // authorises is an action.
        let f = try FrameCodec.decode(line: #"{"t":"res","id":"a","p":{"granted":true}}"#)
        guard case .response(_, .error) = f else {
            return XCTFail("a res with no ok must not decode as success")
        }
    }

    func testDecodeErrorTruncatesTheOffendingLine() {
        let secret = String(repeating: "x", count: 5000)
        do {
            _ = try FrameCodec.decode(line: secret)
            XCTFail("should throw")
        } catch let e as FrameDecodeError {
            XCTAssertLessThanOrEqual(e.line.count, 200)
        } catch { XCTFail("wrong error") }
    }

    // MARK: framing

    func testLineFramerReassemblesSplitReads() {
        var framer = LineFramer()
        let whole = "{\"a\":1}\n{\"b\":2}\n"
        var lines: [String] = []
        for byte in Array(whole.utf8) {
            lines += framer.push(Data([byte])).lines
        }
        XCTAssertEqual(lines, ["{\"a\":1}", "{\"b\":2}"])
        XCTAssertEqual(framer.pending, 0)
    }

    func testLineFramerSplitsOnBytesNotCharacters() {
        var framer = LineFramer()
        let text = "{\"t\":\"ev\",\"m\":\"speak\",\"p\":{\"text\":\"café ☕\"}}\n"
        let bytes = Array(text.utf8)
        // Split in the middle of the multi-byte é.
        let cut = bytes.firstIndex(of: 0xC3)! + 1
        var out = framer.push(Data(bytes[..<cut])).lines
        out += framer.push(Data(bytes[cut...])).lines
        XCTAssertEqual(out.count, 1)
        let f = try! FrameCodec.decode(line: out[0])
        XCTAssertEqual(f.params["text"]?.stringValue, "café ☕")
    }

    func testLineFramerDropsAnUnboundedLine() {
        var framer = LineFramer(limit: 64)
        let r = framer.push(Data(repeating: 0x41, count: 512))
        XCTAssertTrue(r.overflow)
        XCTAssertTrue(r.lines.isEmpty)
        XCTAssertEqual(framer.pending, 0)
    }

    func testFramerKeepsPartialLine() {
        var framer = LineFramer()
        XCTAssertTrue(framer.push(Data("{\"a\"".utf8)).lines.isEmpty)
        XCTAssertGreaterThan(framer.pending, 0)
        XCTAssertEqual(framer.push(Data(":1}\n".utf8)).lines, ["{\"a\":1}"])
    }

    // MARK: JSONValue

    func testWholeNumbersDoNotGrowADecimalTail() throws {
        let data = try FrameCodec.encode(.event(method: "m", params: .object(["n": .number(3)])))
        XCTAssertTrue(String(data: data, encoding: .utf8)!.contains("\"n\":3"))
    }
}
