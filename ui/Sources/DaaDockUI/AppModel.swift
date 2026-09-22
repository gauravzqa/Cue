import SwiftUI
import AppKit
import DaaDockCore

/// The dock's single source of truth.
///
/// It renders `state` / `audit` / `confirm.request` and forwards clicks. It
/// holds **no policy**: no tier, no threshold, no allowlist, no "this one
/// looks safe". Every rule lives in Python under the existing test suite, and
/// the one thing this file must never grow is an opinion about whether an
/// action should run.
@MainActor
@Observable
final class AppModel {

    // MARK: - observable state

    var dock = DockState()
    var transcript = TranscriptRing()
    /// The card currently on screen, if any.
    var approval: ApprovalGate?
    var identityNotice: IdentityNotice?
    var supervisorStatus: PythonSupervisor.Status = .stopped
    /// Shown once, the first time always-on is switched on.
    var alwaysOnExplainerPending = false
    var undoInFlight = false
    var lastUndoMessage: String?
    /// Raw audit stream for the History window. Bounded; the deep history is
    /// `~/.daa/audit.jsonl`, read on demand and never streamed into the dock.
    var recentAudit: [AuditRecord] = []

    let pending = PendingApprovals()
    private(set) var supervisor: PythonSupervisor!
    private var approvalTicker: Timer?
    private var onApprovalChange: ((ApprovalGate?) -> Void)?

    // MARK: - init

    init(repoRoot: String?) {
        supervisor = PythonSupervisor(repoRoot: repoRoot)
        supervisor.onFrame = { [weak self] f in self?.handle(f) }
        supervisor.onStatus = { [weak self] s in self?.handle(status: s) }
    }

    /// Called once at launch, before anything can ask for a permission.
    ///
    /// Every grant made before a stable signing identity exists is thrown away
    /// on the next rebuild, so the warning has to come first, not after the
    /// microphone stops working for no visible reason.
    func checkCodeIdentity(store: IdentityStore = IdentityStore(url: IdentityStore.defaultURL())) {
        let verdict = store.checkAndRecord(current: CodeIdentityReader.readSelf())
        identityNotice = CodeIdentityCheck.explanation(verdict)
        if let n = identityNotice {
            supervisor.note("identity: \(n.title) cdhash=\(n.cdhash)")
        }
    }

    func start() { supervisor.start() }

    func stop() {
        // A card on screen when the dock quits is a refusal, not an
        // approval. Fail closed at every boundary.
        resolveApproval(.brainStopped)
        supervisor.stop()
    }

    func setApprovalObserver(_ block: @escaping (ApprovalGate?) -> Void) {
        onApprovalChange = block
    }

    // MARK: - inbound frames

    private func handle(_ frame: Frame) {
        switch frame {
        case .event(let m, let p): handleEvent(m, p)
        case .request(let id, let m, let p): handleRequest(id: id, method: m, params: p)
        case .response:
            // Responses to our own requests are routed by the supervisor.
            break
        }
    }

    private func handleEvent(_ method: String, _ p: JSONValue) {
        switch method {
        case Method.ready:
            let info = ReadyInfo(p)
            dock.ready = info
            dock.alwaysOn = info.alwaysOn
            dock.phase = .idle
            dock.restartAttempt = 0
            dock.gaveUp = false

        case Method.state:
            let s = StateUpdate(p)
            dock.phase = s.phase
            dock.detail = s.detail
            if let rows = p["tasks"]?.arrayValue {
                dock.tasks = rows.compactMap(TaskRow.init)
            }

        case Method.audit:
            let record = AuditRecord(p)
            recentAudit.append(record)
            if recentAudit.count > 500 { recentAudit.removeFirst(recentAudit.count - 500) }
            if let line = TranscriptProjection.line(for: record) { transcript.append(line) }

        case Method.speak:
            if let text = p["text"]?.stringValue, !text.isEmpty {
                dock.detail = text
            }

        case Method.confirmCancel:
            let id = p["id"]?.stringValue ?? ""
            let why = p["reason"]?.stringValue ?? "withdrawn"
            if approval?.card.id == id { resolveApproval(.withdrawn(why), respond: false) }
            _ = pending.consume(id)

        case Method.taskUpdate:
            if let row = TaskRow(p) {
                if let i = dock.tasks.firstIndex(where: { $0.id == row.id }) { dock.tasks[i] = row }
                else { dock.tasks.append(row) }
            }

        case Method.taskDone:
            if let id = p["id"]?.stringValue,
               let i = dock.tasks.firstIndex(where: { $0.id == id }) {
                dock.tasks[i].done = true
                dock.tasks[i].outcome = p["outcome"]?.stringValue ?? ""
            }

        default:
            supervisor.note("unhandled event \(method)")
        }
    }

    private func handleRequest(id: String, method: String, params: JSONValue) {
        guard method == Method.confirmRequest else {
            supervisor.send(.response(id: id, outcome: .error(
                code: "unknown-method", message: "the dock does not implement \(method)")))
            return
        }

        // Every decision about whether this card may appear lives in
        // `ConfirmRouter`, in the pure core, where it is tested without a
        // running app. This function only obeys the verdict.
        let verdict = ConfirmRouter.admit(
            id: id, params: params, pending: pending,
            cardAlreadyOpen: approval?.resolution == nil && approval != nil)

        let card: ApprovalCard
        switch verdict {
        case .drop(let id, let why):
            supervisor.note("dropping confirm.request \(id): \(why)")
            return
        case .refuse(let id, let outcome, let why):
            supervisor.note("refusing confirm.request \(id): \(why)")
            supervisor.answerConfirm(id: id, outcome: outcome)
            return
        case .present(let c):
            card = c
        }

        approval = ApprovalGate(card: card, presentedAt: now())
        dock.approvalOpen = true
        dock.phase = .awaiting
        onApprovalChange?(approval)
        startApprovalTicker()
        // The system's own "a human is needed" signal, and it fires whether or
        // not the dock panel is open — which is what lets a background job
        // raise a card at any time.
        NSApp.requestUserAttention(.criticalRequest)
        NSSound(named: "Funk")?.play()
    }

    // MARK: - the approval card

    private func now() -> TimeInterval { Date().timeIntervalSince1970 }

    private func startApprovalTicker() {
        approvalTicker?.invalidate()
        let t = Timer(timeInterval: 1.0 / 30.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tickApproval() }
        }
        // .common so the countdown keeps running while a menu is tracking.
        RunLoop.main.add(t, forMode: .common)
        approvalTicker = t
    }

    private func tickApproval() {
        guard var g = approval else { return }
        g = g.ticking(at: now())
        approval = g
        onApprovalChange?(g)
        if let outcome = g.resolution { finish(outcome) }
    }

    func beginHold() {
        guard var g = approval else { return }
        g = g.beginningHold(at: now())
        approval = g
        onApprovalChange?(g)
    }

    func endHold() {
        guard var g = approval else { return }
        g = g.endingHold(at: now())
        approval = g
        onApprovalChange?(g)
        if let outcome = g.resolution { finish(outcome) }
    }

    func markScriptFullySeen() {
        guard let g = approval, !g.scriptFullySeen else { return }
        approval = g.scrolledToEnd()
        onApprovalChange?(approval)
    }

    func resolveApproval(_ outcome: ApprovalOutcome, respond: Bool = true) {
        guard let g = approval, g.resolution == nil else { return }
        approval = g.resolving(outcome)
        finish(outcome, respond: respond)
    }

    private func finish(_ outcome: ApprovalOutcome, respond: Bool = true) {
        approvalTicker?.invalidate()
        approvalTicker = nil
        guard let g = approval else { return }
        // Consuming is what makes the token single-use. If it is already gone
        // the card was resolved by another path and we must not answer twice.
        if pending.consume(g.card.id) != nil, respond {
            supervisor.answerConfirm(id: g.card.id, outcome: outcome)
        }
        approval = nil
        dock.approvalOpen = false
        if dock.phase == .awaiting { dock.phase = .idle }
        onApprovalChange?(nil)
    }

    // MARK: - outbound

    func sendTypedText(_ text: String) {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        transcript.append(TranscriptLine(speaker: .you, text: t))
        // Typing is an unambiguous act of address, the same argument
        // `handle_text` makes, so this skips the gate on the Python side.
        supervisor.event(Method.controlText, .object([
            "text": .string(t), "addressed": .bool(true),
        ]))
    }

    func sendUtterance(_ text: String, confidence: Double, complete: Bool, addressed: Bool,
                       startedAt: TimeInterval) {
        supervisor.event(Method.micUtterance, .object([
            "text": .string(text),
            "confidence": .number(confidence),
            "startedAt": .number(startedAt),
            "complete": .bool(complete),
            "addressed": .bool(addressed),
        ]))
    }

    func sendOnset() { supervisor.event(Method.micOnset) }

    func cancelTurn() {
        // Esc always cancels: drop the utterance, return to idle, speak
        // nothing. If a card is up, Esc cancels the card instead.
        if approval != nil { resolveApproval(.escaped); return }
        supervisor.event(Method.controlCancel)
        dock.partial = ""
        dock.phase = .idle
    }

    func setAlwaysOn(_ on: Bool) {
        if on && !UserDefaults.standard.bool(forKey: "daa.alwaysOnExplainerSeen") {
            alwaysOnExplainerPending = true
            return
        }
        commitAlwaysOn(on)
    }

    func commitAlwaysOn(_ on: Bool) {
        alwaysOnExplainerPending = false
        UserDefaults.standard.set(true, forKey: "daa.alwaysOnExplainerSeen")
        dock.alwaysOn = on
        supervisor.request(Method.controlAlwaysOn, .object(["on": .bool(on)])) { [weak self] r in
            Task { @MainActor in
                guard let self else { return }
                if case .error(_, let message) = r {
                    // Never leave the toggle claiming something the brain did
                    // not agree to. A switch that lies about a hot mic is the
                    // worst control on the panel.
                    self.dock.alwaysOn = !on
                    self.transcript.append(TranscriptLine(
                        speaker: .system, text: "Could not change always-on: \(message)",
                        refused: true))
                }
            }
        }
    }

    func undoLast() {
        guard !undoInFlight else { return }
        undoInFlight = true
        lastUndoMessage = nil
        supervisor.request(Method.undoLast) { [weak self] r in
            Task { @MainActor in
                guard let self else { return }
                self.undoInFlight = false
                switch r {
                case .ok(let p):
                    // Undo never runs below CONFIRM_VOICE, so this usually
                    // reports that daa is ASKING, not that it reversed
                    // anything. Say so, or the click looks broken.
                    let summary = p["summary"]?.stringValue ?? "Asked about undoing that."
                    self.lastUndoMessage = summary
                    self.transcript.append(TranscriptLine(speaker: .system, text: summary))
                case .error(_, let message):
                    self.lastUndoMessage = message
                    self.transcript.append(TranscriptLine(
                        speaker: .system, text: "Undo: \(message)", refused: true))
                }
            }
        }
    }

    // MARK: - supervisor status

    private func handle(status: PythonSupervisor.Status) {
        supervisorStatus = status
        switch status {
        case .running, .starting:
            dock.gaveUp = false
            dock.restartAttempt = 0
        case .stopped:
            dock.phase = .degraded
        case .restarting(let secs, let attempt):
            dock.phase = .degraded
            dock.restartAttempt = attempt
            dock.nextRestartIn = secs
            // The brain going away cancels any card on screen. This is the
            // README's fail-closed rule applied to the new boundary: the thing
            // that would carry out the approval no longer exists.
            resolveApproval(.brainStopped, respond: false)
        case .gaveUp:
            dock.phase = .degraded
            dock.gaveUp = true
            resolveApproval(.brainStopped, respond: false)
        case .noInterpreter(let tried):
            dock.phase = .degraded
            dock.gaveUp = true
            supervisor.note("no python found. tried:\n  " + tried.joined(separator: "\n  "))
        }
    }

    var degradedDetail: String {
        if case .noInterpreter = supervisorStatus {
            return "daa cannot find its Python. Set DAA_PYTHON, or run from the repo."
        }
        return dock.degradedText
    }
}
