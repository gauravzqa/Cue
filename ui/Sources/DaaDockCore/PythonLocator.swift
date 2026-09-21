import Foundation

/// Finding the interpreter, as a pure function over a probe.
///
/// Stages 1–3 point at the developer's own `.venv`; the embedded interpreter
/// in `Contents/Resources/python` only exists from the packaging stage. Both
/// are searched here so the same binary works either way.
public struct PythonLocator: Sendable {
    /// Returns true when the path is an executable file. Injected so the
    /// ordering is testable without a filesystem.
    public let probe: @Sendable (String) -> Bool

    public init(probe: @escaping @Sendable (String) -> Bool) { self.probe = probe }

    public static let real = PythonLocator { path in
        var isDir: ObjCBool = false
        let fm = FileManager.default
        guard fm.fileExists(atPath: path, isDirectory: &isDir), !isDir.boolValue else { return false }
        return fm.isExecutableFile(atPath: path)
    }

    /// In order of decreasing "we know what this is".
    public func candidates(bundleResources: String?, repoRoot: String?, env: [String: String]) -> [String] {
        var out: [String] = []
        // An explicit override always wins, and is how the tests and a
        // developer with a non-standard layout point the dock somewhere else.
        if let explicit = env["DAA_PYTHON"], !explicit.isEmpty { out.append(explicit) }
        // The bundled interpreter, once there is one.
        if let res = bundleResources { out.append(res + "/python/bin/python3") }
        // The repo's own venv: the stage 1–3 answer.
        if let root = repoRoot {
            out.append(root + "/.venv/bin/python3")
            out.append(root + "/.venv/bin/python")
        }
        if let venv = env["VIRTUAL_ENV"], !venv.isEmpty { out.append(venv + "/bin/python3") }
        out.append(NSHomeDirectory() + "/daa/.venv/bin/python3")
        out.append("/opt/homebrew/bin/python3")
        out.append("/usr/local/bin/python3")
        // Deliberately last. The system interpreter almost certainly does not
        // have daa installed, and picking it would produce a confusing
        // ImportError rather than an honest "no interpreter".
        out.append("/usr/bin/python3")
        var seen = Set<String>()
        return out.filter { seen.insert($0).inserted }
    }

    public func locate(bundleResources: String?, repoRoot: String?, env: [String: String]) -> String? {
        candidates(bundleResources: bundleResources, repoRoot: repoRoot, env: env).first(where: probe)
    }

    /// `-u` is not optional. Python's stdout is FULLY BUFFERED on a pipe, so
    /// without it the dock waits forever for a `ready` frame that is sitting
    /// in a 8 KiB buffer inside the child. This is the classic version of this
    /// bug and it costs an evening every time.
    public static func arguments() -> [String] { ["-u", "-m", "daa.cli", "bridge"] }

    /// Belt and braces for the same problem, plus one thing that matters more:
    /// the child must NOT inherit a stray `PYTHONSTARTUP` or `-i`, either of
    /// which would write to fd 1 and corrupt the protocol.
    public static func environment(base: [String: String], repoRoot: String?) -> [String: String] {
        var env = base
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONSTARTUP"] = ""
        env["PYTHONINSPECT"] = ""
        env["DAA_UI"] = "dock"
        if let root = repoRoot { env["DAA_REPO_ROOT"] = root }
        return env
    }
}

/// When to respawn a dead child, and when to stop.
///
/// A crashing child respawned forever is a hot loop that looks like a hang.
/// Backoff caps at 30s and, after enough consecutive failures in a short
/// window, the dock stops trying and sits visibly in `degraded` with the log
/// one click away — which is more useful than a spinner that never resolves.
public struct RestartPolicy: Sendable, Equatable {
    public var attempt: Int = 0
    /// Failures counted inside `window`.
    public var recentFailures: [TimeInterval] = []

    public let base: TimeInterval
    public let cap: TimeInterval
    public let window: TimeInterval
    public let maxFailuresInWindow: Int
    /// A child that ran at least this long before dying was working, so the
    /// backoff resets. Without this, one crash an hour eventually reaches the
    /// 30s cap and stays there.
    public let healthyAfter: TimeInterval

    public init(base: TimeInterval = 1, cap: TimeInterval = 30,
                window: TimeInterval = 120, maxFailuresInWindow: Int = 5,
                healthyAfter: TimeInterval = 20) {
        self.base = base; self.cap = cap; self.window = window
        self.maxFailuresInWindow = maxFailuresInWindow; self.healthyAfter = healthyAfter
    }

    public enum Decision: Sendable, Equatable {
        case restart(after: TimeInterval, attempt: Int)
        case giveUp(reason: String)
    }

    public mutating func childDied(at now: TimeInterval, ranFor: TimeInterval) -> Decision {
        if ranFor >= healthyAfter {
            attempt = 0
            recentFailures = []
        }
        recentFailures.append(now)
        recentFailures.removeAll { now - $0 > window }
        if recentFailures.count > maxFailuresInWindow {
            return .giveUp(reason: "daa's brain stopped \(recentFailures.count) times in \(Int(window)) seconds")
        }
        attempt += 1
        let delay = min(cap, base * pow(2, Double(attempt - 1)))
        return .restart(after: delay, attempt: attempt)
    }

    public mutating func childReady() {
        attempt = 0
        recentFailures = []
    }
}
