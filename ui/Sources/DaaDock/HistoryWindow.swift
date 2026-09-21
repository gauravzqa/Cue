import SwiftUI
import AppKit
import DaaDockCore

/// The second window: history, the undo journal, and set-up.
///
/// An ordinary resizable window, not a panel, opened on demand. Deep history
/// comes from `~/.daa/audit.jsonl` and is read here — never streamed into the
/// dock, which mirrors only the short horizon the model itself can see.
@MainActor
final class HistoryWindowController: NSObject, NSWindowDelegate {
    static let shared = HistoryWindowController()
    private var window: NSWindow?

    func show(model: AppModel) {
        if let w = window {
            w.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let view = HistoryView().environment(model)
        let w = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 760, height: 560),
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered, defer: false)
        w.title = "daa — history and set-up"
        w.contentView = NSHostingView(rootView: view)
        w.center()
        w.delegate = self
        w.isReleasedWhenClosed = false
        window = w
        w.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func windowWillClose(_ notification: Notification) { window = nil }
}

struct HistoryView: View {
    @Environment(AppModel.self) private var model
    @State private var tab = Tab.history
    @State private var filter = ""
    @State private var onlyDecisions = true

    enum Tab: String, CaseIterable { case history = "History", setup = "Set-up" }

    var body: some View {
        VStack(spacing: 0) {
            Picker("", selection: $tab) {
                ForEach(Tab.allCases, id: \.self) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .padding(10)
            Divider()
            switch tab {
            case .history: history
            case .setup: setup
            }
        }
    }

    // MARK: - history

    private var rows: [AuditRecord] {
        model.recentAudit.reversed().filter { r in
            if onlyDecisions && !AuditRecord.loadBearing.contains(r.kind) { return false }
            guard !filter.isEmpty else { return true }
            return r.kind.localizedCaseInsensitiveContains(filter)
                || (r.toolName ?? "").localizedCaseInsensitiveContains(filter)
        }
    }

    private var history: some View {
        VStack(spacing: 0) {
            HStack {
                TextField("filter by kind or tool", text: $filter)
                    .textFieldStyle(.roundedBorder)
                Toggle("Decisions only", isOn: $onlyDecisions)
                    .toggleStyle(.checkbox)
                Button("Open audit.jsonl") {
                    let p = NSHomeDirectory() + "/.daa/audit.jsonl"
                    NSWorkspace.shared.selectFile(p, inFileViewerRootedAtPath: "")
                }
            }
            .padding(10)
            Divider()
            if rows.isEmpty {
                Spacer()
                Text("Nothing yet this session.\nThe full log is at ~/.daa/audit.jsonl.")
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.secondary)
                Spacer()
            } else {
                List(rows) { record in
                    AuditRow(record: record)
                }
                .listStyle(.inset)
            }
        }
    }

    // MARK: - set-up

    /// The honest home for `daa doctor`: providers live or fake, what is
    /// missing, and the dock's own signing state. Every row says what it is,
    /// not whether it is fine.
    private var setup: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                if let notice = model.identityNotice {
                    GroupBox("Code signature") {
                        VStack(alignment: .leading, spacing: 6) {
                            Text(notice.title).font(.callout.weight(.semibold))
                            Text(notice.body).font(.caption).foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                            if let r = notice.remedy {
                                Text(r).font(.caption.weight(.medium))
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            Text("cdhash \(notice.cdhash)")
                                .font(.caption2.monospaced()).textSelection(.enabled)
                                .foregroundStyle(.tertiary)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(6)
                    }
                }

                GroupBox("Providers") {
                    VStack(alignment: .leading, spacing: 5) {
                        if model.dock.ready.providers.isEmpty {
                            Text("Not reported yet.").foregroundStyle(.secondary).font(.caption)
                        }
                        ForEach(model.dock.ready.providers.sorted(by: { $0.key < $1.key }),
                                id: \.key) { k, v in
                            HStack {
                                Text(k).font(.callout.monospaced()).frame(width: 90, alignment: .leading)
                                Text(v)
                                    .font(.caption.weight(.medium))
                                    .padding(.horizontal, 6).padding(.vertical, 2)
                                    .background(v == "live" ? Color.green.opacity(0.18)
                                                            : Color.orange.opacity(0.2),
                                                in: Capsule())
                                Spacer()
                            }
                        }
                        if !model.dock.ready.jevLive {
                            Text("""
                                The judgment model is not live, so risk assessments are \
                                synthetic and every approval card will say so.
                                """)
                                .font(.caption).foregroundStyle(.orange)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(6)
                }

                GroupBox("Mode") {
                    VStack(alignment: .leading, spacing: 5) {
                        row("dry run", model.dock.ready.dryRun ? "ON — nothing is actually done"
                                                               : "OFF — actions really run")
                        row("always on", model.dock.alwaysOn ? "ON — microphone open" : "OFF — push to talk only")
                        row("bridge", statusText)
                        row("log", model.supervisor.logPath)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(6)
                }

                if !model.dock.ready.missing.isEmpty {
                    GroupBox("Missing") {
                        VStack(alignment: .leading, spacing: 4) {
                            ForEach(model.dock.ready.missing, id: \.self) { m in
                                Text("• \(m)").font(.callout)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(6)
                    }
                }

                GroupBox("Tools") {
                    VStack(alignment: .leading, spacing: 3) {
                        ForEach(model.dock.ready.tools, id: \.name) { t in
                            HStack {
                                Text(t.name).font(.callout.monospaced())
                                Spacer()
                                Text(t.floor).font(.caption.monospaced()).foregroundStyle(.secondary)
                            }
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(6)
                }
            }
            .padding(16)
        }
    }

    private func row(_ k: String, _ v: String) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(k).font(.callout.monospaced()).frame(width: 90, alignment: .leading)
            Text(v).font(.callout).textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            Spacer()
        }
    }

    private var statusText: String {
        switch model.supervisorStatus {
        case .running: return "running"
        case .starting: return "starting"
        case .stopped: return "stopped"
        case .restarting(let s, let a): return "restarting in \(Int(s))s (attempt \(a))"
        case .gaveUp(let r): return "stopped — \(r)"
        case .noInterpreter(let tried): return "no interpreter. tried: " + tried.joined(separator: ", ")
        }
    }
}

struct AuditRow: View {
    let record: AuditRecord
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Text(record.kind)
                    .font(.caption.weight(.semibold).monospaced())
                    .padding(.horizontal, 6).padding(.vertical, 2)
                    .background(tone.opacity(0.18), in: RoundedRectangle(cornerRadius: 4))
                if let tool = record.toolName {
                    Text(tool).font(.caption.monospaced()).foregroundStyle(.secondary)
                }
                if record.isDryRun {
                    Text("DRY RUN").font(.caption2.weight(.bold))
                        .padding(.horizontal, 5).padding(.vertical, 1)
                        .background(Color.blue.opacity(0.2), in: Capsule())
                }
                // "This decision came from FakeJev" must be visible.
                if record.isSynthetic {
                    Text("SYNTHETIC").font(.caption2.weight(.bold))
                        .padding(.horizontal, 5).padding(.vertical, 1)
                        .background(Color.purple.opacity(0.2), in: Capsule())
                }
                Spacer()
                Text(Date(timeIntervalSince1970: record.at)
                    .formatted(date: .omitted, time: .standard))
                    .font(.caption2.monospacedDigit()).foregroundStyle(.tertiary)
                Button(expanded ? "−" : "+") { expanded.toggle() }
                    .buttonStyle(.plain).font(.caption)
            }
            if expanded {
                ForEach(record.payload.sorted(by: { $0.key < $1.key }), id: \.key) { k, v in
                    Text("\(k) = \(describe(v))")
                        .font(.caption2.monospaced())
                        .textSelection(.enabled)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(.vertical, 2)
    }

    private func describe(_ v: JSONValue) -> String {
        switch v {
        case .string(let s): return s
        case .number(let n): return n == n.rounded() ? "\(Int(n))" : "\(n)"
        case .bool(let b): return "\(b)"
        case .null: return "null"
        case .array(let a): return "[" + a.map(describe).joined(separator: ", ") + "]"
        case .object(let o): return "{" + o.keys.sorted().joined(separator: ", ") + "}"
        }
    }

    private var tone: Color {
        switch record.kind {
        case "execution", "undo": return .green
        case "refused", "abandoned", "undo_rejected", "error", "deferred_visual": return .orange
        case "dry_run": return .blue
        case "confirmation", "confirmation_event", "visual_confirm": return .purple
        default: return .secondary
        }
    }
}
