import Foundation

/// The protocol version this build speaks. Sent in `session.hello`; a Python
/// side answering with a different major refuses to run rather than guessing.
public let daaProtocolVersion = 1

// MARK: - Frames

/// One newline-delimited JSON object.
///
/// Three kinds, JSON-RPC-shaped and deliberately not the full spec. Requests
/// flow **both** ways: Python raises `confirm.request` at the dock exactly as
/// the dock raises `undo.last` at Python.
public enum Frame: Sendable, Equatable {
    case request(id: String, method: String, params: JSONValue)
    case response(id: String, outcome: ResponseOutcome)
    case event(method: String, params: JSONValue)

    public var id: String? {
        switch self {
        case .request(let id, _, _), .response(let id, _): return id
        case .event: return nil
        }
    }

    public var method: String? {
        switch self {
        case .request(_, let m, _), .event(let m, _): return m
        case .response: return nil
        }
    }

    public var params: JSONValue {
        switch self {
        case .request(_, _, let p), .event(_, let p): return p
        case .response(_, let o):
            if case .ok(let p) = o { return p }
            return .null
        }
    }
}

public enum ResponseOutcome: Sendable, Equatable {
    case ok(JSONValue)
    case error(code: String, message: String)
}

public struct FrameDecodeError: Error, Equatable, CustomStringConvertible {
    public let reason: String
    public let line: String
    public init(reason: String, line: String) {
        self.reason = reason
        // Keep the offending line short in logs. It may contain an utterance.
        self.line = String(line.prefix(200))
    }
    public var description: String { "bad frame: \(reason)" }
}

// MARK: - Codec

/// Encode and decode one frame. Stateless and synchronous on purpose: the
/// reader thread does nothing but call this, so there is nothing in it that
/// can block, and a throw here is a skipped line rather than a dead reader.
public enum FrameCodec {

    public static func decode(line: String) throws -> Frame {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            throw FrameDecodeError(reason: "empty line", line: line)
        }
        guard let data = trimmed.data(using: .utf8) else {
            throw FrameDecodeError(reason: "not utf-8", line: line)
        }
        let root: JSONValue
        do { root = try JSONDecoder().decode(JSONValue.self, from: data) }
        catch { throw FrameDecodeError(reason: "not json: \(error)", line: line) }
        guard case .object = root else {
            throw FrameDecodeError(reason: "top level is not an object", line: line)
        }
        guard let kind = root["t"]?.stringValue else {
            throw FrameDecodeError(reason: "missing t", line: line)
        }
        let params = root["p"] ?? .object([:])

        switch kind {
        case "req":
            guard let id = root["id"]?.stringValue, !id.isEmpty else {
                throw FrameDecodeError(reason: "req without id", line: line)
            }
            guard let m = root["m"]?.stringValue, !m.isEmpty else {
                throw FrameDecodeError(reason: "req without m", line: line)
            }
            return .request(id: id, method: m, params: params)

        case "res":
            guard let id = root["id"]?.stringValue, !id.isEmpty else {
                throw FrameDecodeError(reason: "res without id", line: line)
            }
            // A `res` with no `ok` is not a permissive default. Absent means
            // false, because the only thing a response ever authorises is an
            // action, and an unreadable answer is not an approval.
            let ok = root["ok"]?.boolValue ?? false
            if ok { return .response(id: id, outcome: .ok(params)) }
            let err = root["err"]
            return .response(id: id, outcome: .error(
                code: err?["code"]?.stringValue ?? "error",
                message: err?["message"]?.stringValue ?? "the other side said no"
            ))

        case "ev":
            guard let m = root["m"]?.stringValue, !m.isEmpty else {
                throw FrameDecodeError(reason: "ev without m", line: line)
            }
            return .event(method: m, params: params)

        default:
            throw FrameDecodeError(reason: "unknown t=\(kind)", line: line)
        }
    }

    /// One line, newline-terminated, no embedded raw newlines.
    public static func encode(_ frame: Frame) throws -> Data {
        var root: [String: JSONValue] = [:]
        switch frame {
        case .request(let id, let m, let p):
            root = ["t": .string("req"), "id": .string(id), "m": .string(m), "p": p]
        case .event(let m, let p):
            root = ["t": .string("ev"), "m": .string(m), "p": p]
        case .response(let id, let outcome):
            root = ["t": .string("res"), "id": .string(id)]
            switch outcome {
            case .ok(let p):
                root["ok"] = .bool(true)
                root["p"] = p
            case .error(let code, let message):
                root["ok"] = .bool(false)
                root["err"] = .object(["code": .string(code), "message": .string(message)])
            }
        }
        let enc = JSONEncoder()
        enc.outputFormatting = [.withoutEscapingSlashes, .sortedKeys]
        var data = try enc.encode(JSONValue.object(root))
        // JSONEncoder escapes control characters, so this can only fire if
        // that ever stops being true. Cheap, and the alternative is a corrupt
        // stream that looks like a protocol bug on the other side.
        precondition(!data.contains(0x0A), "frame contains a raw newline")
        data.append(0x0A)
        return data
    }
}

// MARK: - Line framing

/// Reassembles `\n`-delimited lines out of arbitrary pipe reads.
///
/// A pipe read boundary lands anywhere, including the middle of a multi-byte
/// character, so splitting bytes (not decoded text) is the only correct way to
/// do this.
public struct LineFramer: Sendable {
    private var buffer = Data()
    /// A single frame larger than this is treated as a desynchronised stream
    /// rather than a big approval card. Scripts can legitimately be long, so
    /// this is generous; it exists only so a stuck peer cannot grow the buffer
    /// without bound.
    public let limit: Int

    public init(limit: Int = 8 * 1024 * 1024) { self.limit = limit }

    /// Returns complete lines. `overflow` is true when the buffer was dropped.
    public mutating func push(_ chunk: Data) -> (lines: [String], overflow: Bool) {
        buffer.append(chunk)
        var lines: [String] = []
        while let idx = buffer.firstIndex(of: 0x0A) {
            let lineData = buffer[buffer.startIndex..<idx]
            buffer = buffer[buffer.index(after: idx)...]
            if let s = String(data: lineData, encoding: .utf8) { lines.append(s) }
            else { lines.append("") }  // decoded as a bad frame downstream
        }
        buffer = Data(buffer)
        if buffer.count > limit {
            buffer.removeAll(keepingCapacity: false)
            return (lines, true)
        }
        return (lines, false)
    }

    public var pending: Int { buffer.count }
}

// MARK: - Method names

/// Every method name in the protocol, in one place, so a typo is a compile
/// error rather than a frame nobody handles.
public enum Method {
    // Swift -> Python
    public static let hello = "session.hello"
    public static let shutdown = "session.shutdown"
    public static let micUtterance = "mic.utterance"
    public static let micOnset = "mic.onset"
    public static let controlAlwaysOn = "control.alwaysOn"
    public static let controlCancel = "control.cancel"
    public static let controlText = "control.text"
    public static let undoLast = "undo.last"
    public static let doctor = "doctor"

    // Python -> Swift
    public static let ready = "ready"
    public static let state = "state"
    public static let audit = "audit"
    public static let speak = "speak"
    public static let confirmRequest = "confirm.request"
    public static let confirmCancel = "confirm.cancel"
    public static let taskUpdate = "task.update"
    public static let taskDone = "task.done"
}
