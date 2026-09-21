import Foundation

/// A JSON document, decoded but not interpreted.
///
/// The wire protocol's `p` field is whatever Python put there. Modelling it as
/// a concrete Swift type per method would mean a malformed or newer frame
/// fails to decode *at the envelope level*, which would take the reader thread
/// down. Decoding to `JSONValue` first means an unknown frame is still a
/// well-formed frame we can log and skip.
public enum JSONValue: Sendable, Equatable, Hashable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])
}

extension JSONValue: Codable {
    public init(from decoder: any Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null; return }
        if let v = try? c.decode(Bool.self) { self = .bool(v); return }
        if let v = try? c.decode(Double.self) { self = .number(v); return }
        if let v = try? c.decode(String.self) { self = .string(v); return }
        if let v = try? c.decode([JSONValue].self) { self = .array(v); return }
        if let v = try? c.decode([String: JSONValue].self) { self = .object(v); return }
        throw DecodingError.dataCorruptedError(in: c, debugDescription: "not JSON")
    }

    public func encode(to encoder: any Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .null: try c.encodeNil()
        case .bool(let v): try c.encode(v)
        case .number(let v):
            // Emit whole numbers without a ".0" tail so round-tripping an id
            // or a count does not quietly change its spelling.
            if v == v.rounded(), abs(v) < 9_007_199_254_740_992 { try c.encode(Int64(v)) }
            else { try c.encode(v) }
        case .string(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .object(let v): try c.encode(v)
        }
    }
}

// MARK: - Reading

public extension JSONValue {
    subscript(key: String) -> JSONValue? {
        guard case .object(let o) = self else { return nil }
        return o[key]
    }

    var stringValue: String? {
        if case .string(let s) = self { return s }
        return nil
    }

    var doubleValue: Double? {
        switch self {
        case .number(let n): return n
        case .string(let s): return Double(s)
        case .bool(let b): return b ? 1 : 0
        default: return nil
        }
    }

    var intValue: Int? { doubleValue.map(Int.init) }

    var boolValue: Bool? {
        switch self {
        case .bool(let b): return b
        case .number(let n): return n != 0
        default: return nil
        }
    }

    var arrayValue: [JSONValue]? {
        if case .array(let a) = self { return a }
        return nil
    }

    var objectValue: [String: JSONValue]? {
        if case .object(let o) = self { return o }
        return nil
    }

    /// Every string in an array-of-strings field, skipping anything that is not
    /// a string rather than failing the whole frame.
    var stringArray: [String] {
        (arrayValue ?? []).compactMap(\.stringValue)
    }

    /// `["send": "sending this text"]` from an object of strings.
    /// Order is not preserved by JSON objects, so callers that display these
    /// must sort; see `ApprovalCard`.
    var stringMap: [String: String] {
        var out: [String: String] = [:]
        for (k, v) in objectValue ?? [:] { if let s = v.stringValue { out[k] = s } }
        return out
    }
}

// MARK: - Writing

public extension JSONValue {
    static func of(_ pairs: [String: JSONValue]) -> JSONValue { .object(pairs) }

    init(_ v: String) { self = .string(v) }
    init(_ v: Bool) { self = .bool(v) }
    init(_ v: Int) { self = .number(Double(v)) }
    init(_ v: Double) { self = .number(v) }
}
