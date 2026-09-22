import Foundation
import DaaDockCore

/// Serialises access to a `LineFramer` across pipe callbacks.
private final class FramerBox: @unchecked Sendable {
    private var framer = LineFramer()
    private let lock = NSLock()
    func push(_ d: Data) -> (lines: [String], overflow: Bool) {
        lock.lock(); defer { lock.unlock() }
        return framer.push(d)
    }
}

/// Spawns, watches and restarts the Python child, and carries frames both ways.
///
/// The child is an **ordinary, non-disclaimed child process**. That is the
/// whole TCC argument for this architecture: macOS attributes privacy grants
/// to the responsible code, "the nearest parent of the process that the user
/// knows about", and for a plain child of a signed `.app` that is the app.
///
/// So this file must never:
///   - call `responsibility_spawnattrs_setdisclaim`
///   - double-fork, `setsid`, or daemonize
///   - hand the child to `launchd`
///
/// Each of those breaks attribution and puts the grants back on the
/// interpreter, which is the problem the dock exists to solve.
@MainActor
final class PythonSupervisor {

    enum Status: Equatable {
        case stopped
        case starting
        case running
        case restarting(inSeconds: TimeInterval, attempt: Int)
        case gaveUp(String)
        case noInterpreter([String])
    }

    private(set) var status: Status = .stopped
    private(set) var lastExit: Int32?

    /// Every decoded frame, on the main actor.
    var onFrame: ((Frame) -> Void)?
    var onStatus: ((Status) -> Void)?

    private var process: Process?
    private var stdinPipe: FileHandle?
    private var startedAt: TimeInterval = 0
    private var policy = RestartPolicy()
    private var restartWork: DispatchWorkItem?
    private var nextRequestID = 0
    private var pendingRequests: [String: (ResponseOutcome) -> Void] = [:]
    private var intentionalStop = false

    private let repoRoot: String?
    private let logURL: URL
    private let writeLock = NSLock()

    init(repoRoot: String?) {
        self.repoRoot = repoRoot
        let logs = URL(fileURLWithPath: NSHomeDirectory())
            .appendingPathComponent("Library/Logs/daa", isDirectory: true)
        try? FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
        logURL = logs.appendingPathComponent("python.log")
    }

    // MARK: - lifecycle

    func start() {
        guard process == nil else { return }
        intentionalStop = false
        restartWork?.cancel()

        let locator = PythonLocator.real
        let env = ProcessInfo.processInfo.environment
        let resources = Bundle.main.resourcePath
        guard let python = locator.locate(bundleResources: resources, repoRoot: repoRoot, env: env) else {
            let tried = locator.candidates(bundleResources: resources, repoRoot: repoRoot, env: env)
            set(.noInterpreter(tried))
            return
        }

        set(.starting)
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = PythonLocator.arguments()
        p.environment = PythonLocator.environment(base: env, repoRoot: repoRoot)
        if let repoRoot { p.currentDirectoryURL = URL(fileURLWithPath: repoRoot) }

        let out = Pipe(), inp = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardInput = inp
        p.standardError = err

        // fd 1 is the protocol and nothing else. Every `print()` in the tree
        // is redirected to stderr on the Python side; this end tees stderr to
        // a log file so a traceback is not lost.
        let logHandle = openLog()
        err.fileHandleForReading.readabilityHandler = { h in
            let d = h.availableData
            guard !d.isEmpty else { return }
            logHandle?.write(d)
            FileHandle.standardError.write(d)
        }

        // `readabilityHandler` is invoked serially on one queue, so the
        // framer has exactly one mutator at a time; the box is what tells the
        // compiler that.
        let framer = FramerBox()
        out.fileHandleForReading.readabilityHandler = { [weak self] h in
            let d = h.availableData
            guard !d.isEmpty else { return }
            let (lines, overflow) = framer.push(d)
            let frames: [Result<Frame, FrameDecodeError>] = lines.map { line in
                do { return .success(try FrameCodec.decode(line: line)) }
                catch let e as FrameDecodeError { return .failure(e) }
                catch { return .failure(FrameDecodeError(reason: "\(error)", line: line)) }
            }
            Task { @MainActor [weak self] in
                guard let self else { return }
                if overflow { self.note("dropped an oversized frame from the bridge") }
                for r in frames {
                    switch r {
                    // An unparseable line is logged and skipped. It must never
                    // take the reader down: one bad frame is not a dead dock.
                    case .failure(let e): self.note("bad frame: \(e.reason)")
                    case .success(let f): self.deliver(f)
                    }
                }
            }
        }

        p.terminationHandler = { [weak self] proc in
            let code = proc.terminationStatus
            Task { @MainActor [weak self] in self?.childDied(code) }
        }

        do { try p.run() } catch {
            note("could not launch \(python): \(error)")
            set(.gaveUp("could not launch \(python)"))
            return
        }

        process = p
        stdinPipe = inp.fileHandleForWriting
        startedAt = Date().timeIntervalSince1970
        set(.running)

        send(.request(id: mintID(), method: Method.hello, params: .object([
            "proto": .number(Double(daaProtocolVersion)),
            "app": .string(appVersion),
            "caps": .array([.string("stt.local"), .string("tts"), .string("hotkey"),
                            .string("confirm.visual")]),
        ])))
    }

    /// Ask the child to exit, then make sure it does.
    func stop() {
        intentionalStop = true
        restartWork?.cancel()
        guard let p = process else { return }
        send(.request(id: mintID(), method: Method.shutdown, params: .object([:])))
        // A bridge that will not exit is not allowed to outlive the dock: a
        // detached child holding the microphone is exactly the state the whole
        // parent-owns-the-child design exists to avoid.
        let deadline = DispatchTime.now() + .milliseconds(1500)
        DispatchQueue.main.asyncAfter(deadline: deadline) { [weak p] in
            guard let p, p.isRunning else { return }
            p.terminate()
            DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(500)) {
                if p.isRunning { kill(p.processIdentifier, SIGKILL) }
            }
        }
    }

    private func childDied(_ code: Int32) {
        let ranFor = Date().timeIntervalSince1970 - startedAt
        lastExit = code
        process = nil
        stdinPipe = nil

        // Every in-flight request resolves as a failure. Nothing waits on a
        // process that no longer exists.
        let waiting = pendingRequests
        pendingRequests.removeAll()
        for (_, cb) in waiting {
            cb(.error(code: "brain-stopped", message: "daa's brain stopped"))
        }

        if intentionalStop { set(.stopped); return }

        switch policy.childDied(at: Date().timeIntervalSince1970, ranFor: ranFor) {
        case .giveUp(let reason):
            note("giving up: \(reason)")
            set(.gaveUp(reason))
        case .restart(let after, let attempt):
            note("child exited \(code) after \(Int(ranFor))s; restarting in \(Int(after))s")
            set(.restarting(inSeconds: after, attempt: attempt))
            let work = DispatchWorkItem { [weak self] in self?.start() }
            restartWork = work
            DispatchQueue.main.asyncAfter(deadline: .now() + after, execute: work)
        }
    }

    // MARK: - frames

    private func deliver(_ frame: Frame) {
        if case .response(let id, let outcome) = frame, let cb = pendingRequests.removeValue(forKey: id) {
            cb(outcome)
            return
        }
        if case .event(Method.ready, _) = frame { policy.childReady() }
        onFrame?(frame)
    }

    func send(_ frame: Frame) {
        guard let handle = stdinPipe else { return }
        guard let data = try? FrameCodec.encode(frame) else {
            note("refusing to send an unencodable frame")
            return
        }
        writeLock.lock()
        defer { writeLock.unlock() }
        do { try handle.write(contentsOf: data) } catch {
            // EPIPE and friends are a clean disconnect: the child is gone and
            // the termination handler is about to say so. Anything else is
            // worth a line in the log.
            let code = (error as NSError).code
            if ![EPIPE, ECONNRESET, EBADF, ESHUTDOWN].contains(Int32(code)) {
                note("write failed: \(error)")
            }
        }
    }

    func event(_ method: String, _ params: JSONValue = .object([:])) {
        send(.event(method: method, params: params))
    }

    func request(_ method: String, _ params: JSONValue = .object([:]),
                 reply: @escaping (ResponseOutcome) -> Void) {
        guard process != nil else {
            reply(.error(code: "brain-stopped", message: "daa's brain is not running"))
            return
        }
        let id = mintID()
        pendingRequests[id] = reply
        send(.request(id: id, method: method, params: params))
        // Nothing waits forever. A request with no answer resolves as a
        // failure so the UI never sits in a state the child cannot leave.
        DispatchQueue.main.asyncAfter(deadline: .now() + 30) { [weak self] in
            guard let self, let cb = self.pendingRequests.removeValue(forKey: id) else { return }
            cb(.error(code: "timeout", message: "daa's brain did not answer"))
        }
    }

    /// Answer a `confirm.request`. Exactly one of these per card; the
    /// single-use rule is enforced by `PendingApprovals` upstream.
    func answerConfirm(id: String, outcome: ApprovalOutcome) {
        send(.response(id: id, outcome: .ok(.object([
            "granted": .bool(outcome.granted),
            "reason": .string(outcome.wireReason),
        ]))))
    }

    // MARK: - bits

    private func mintID() -> String {
        nextRequestID += 1
        return "s\(nextRequestID)"
    }

    private func set(_ s: Status) {
        status = s
        onStatus?(s)
    }

    private func openLog() -> FileHandle? {
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        guard let h = try? FileHandle(forWritingTo: logURL) else { return nil }
        try? h.seekToEnd()
        return h
    }

    func note(_ message: String) {
        let line = "[\(ISO8601DateFormatter().string(from: Date()))] dock: \(message)\n"
        FileHandle.standardError.write(Data(line.utf8))
        openLog()?.write(Data(line.utf8))
    }

    var logPath: String { logURL.path }

    var appVersion: String {
        (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? "0.1.0-dev"
    }
}
