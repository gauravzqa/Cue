import SwiftUI
import AppKit
import DaaDockCore

/// The dock proper: ~320 pt wide, height driven by content.
///
/// Principle 1: state is legible from the menu bar alone; this panel is for
/// detail. Principle 3: the privacy boundary is visible — in always-on mode
/// this shows a level meter and never live text, until the address gate wakes.
struct DockView: View {
    @Environment(AppModel.self) private var model
    @State private var typed = ""
    @FocusState private var typingFocused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let notice = model.identityNotice { IdentityBanner(notice: notice) }
            header
            Divider()
            liveZone
            Divider()
            transcript
            if !model.dock.tasks.isEmpty {
                Divider()
                taskStrip
            }
            Divider()
            footer
        }
        .frame(width: 340)
        .background(.regularMaterial)
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.primary.opacity(0.12)))
        .sheet(isPresented: Binding(
            get: { model.alwaysOnExplainerPending },
            set: { if !$0 { model.alwaysOnExplainerPending = false } })) {
            AlwaysOnExplainer()
        }
    }

    // MARK: - header

    private var header: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(model.dock.menuBar.tint == .amber ? Color.orange : Color.secondary)
                .frame(width: 8, height: 8)
            Text("daa").font(.headline)
            Text(model.dock.ready.daaVersion)
                .font(.caption2).foregroundStyle(.tertiary)
            Spacer()
            Text("⌥Space")
                .font(.caption.monospaced())
                .foregroundStyle(.secondary)
                .help("Hold to talk. Tap to latch.")
            // Persistent, high-contrast, never subtle. It changes what the
            // product IS.
            if model.dock.ready.dryRun {
                Text("DRY RUN")
                    .font(.caption2.weight(.heavy))
                    .kerning(0.6)
                    .padding(.horizontal, 7).padding(.vertical, 3)
                    .background(Color.blue.opacity(0.22), in: Capsule())
                    .overlay(Capsule().strokeBorder(Color.blue.opacity(0.5)))
                    .help("daa will describe what it would do and not do it.")
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
    }

    // MARK: - live zone

    private var liveZone: some View {
        HStack(alignment: .center, spacing: 10) {
            LevelMeter(level: model.dock.micLevel,
                       active: model.dock.phase == .listening,
                       hot: model.dock.alwaysOn)
            VStack(alignment: .leading, spacing: 2) {
                Text(model.dock.phase == .degraded ? model.degradedDetail : model.dock.liveText)
                    .font(.callout)
                    .foregroundStyle(model.dock.mayShowPartial ? .primary : .secondary)
                    .lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true)
                if model.dock.alwaysOn && !model.dock.mayShowPartial && model.dock.phase == .listening {
                    Text("nothing is written down until it's addressed to daa")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }
            Spacer(minLength: 0)
            if model.dock.phase == .degraded {
                Button("Show log") { NSWorkspace.shared.selectFile(
                    model.supervisor.logPath, inFileViewerRootedAtPath: "") }
                    .font(.caption)
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 12)
    }

    // MARK: - transcript

    private var transcript: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    if model.transcript.lines.isEmpty {
                        Text("Hold ⌥Space and say something, or type below.")
                            .font(.caption)
                            .foregroundStyle(.tertiary)
                            .padding(.vertical, 6)
                    }
                    ForEach(model.transcript.lines) { line in
                        TranscriptRow(line: line)
                            .id(line.id)
                    }
                }
                .padding(.horizontal, 14).padding(.vertical, 10)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(height: 180)
            .onChange(of: model.transcript.lines.count) { _, _ in
                if let last = model.transcript.lines.last {
                    withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                }
            }
        }
    }

    // MARK: - tasks (room for background jobs)

    private var taskStrip: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(model.dock.tasks) { task in
                HStack(spacing: 8) {
                    if task.done {
                        Image(systemName: "checkmark.circle.fill")
                            .foregroundStyle(.secondary).font(.caption)
                    } else if let p = task.progress {
                        ProgressView(value: p).frame(width: 40)
                    } else {
                        ProgressView().controlSize(.mini)
                    }
                    Text(task.title).font(.caption).lineLimit(1)
                    Spacer(minLength: 0)
                    if task.cancellable && !task.done {
                        Button {
                            model.supervisor.event("task.cancel", .object(["id": .string(task.id)]))
                        } label: { Image(systemName: "xmark.circle") }
                            .buttonStyle(.plain).font(.caption)
                    }
                }
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 8)
    }

    // MARK: - footer

    private var footer: some View {
        VStack(spacing: 8) {
            HStack(spacing: 6) {
                TextField("type instead of talking…", text: $typed)
                    .textFieldStyle(.roundedBorder)
                    .font(.callout)
                    .focused($typingFocused)
                    .onSubmit {
                        model.sendTypedText(typed)
                        typed = ""
                    }
            }
            HStack(spacing: 10) {
                Toggle(isOn: Binding(
                    get: { model.dock.alwaysOn },
                    set: { model.setAlwaysOn($0) })) {
                    Label("Always on", systemImage: model.dock.alwaysOn ? "mic.fill" : "mic.slash")
                        .font(.caption)
                }
                .toggleStyle(.switch)
                .controlSize(.mini)

                Spacer()

                if let line = model.transcript.undoableLine {
                    Button {
                        model.undoLast()
                    } label: {
                        Label(model.undoInFlight ? "confirming…" : "Undo",
                              systemImage: "arrow.uturn.backward")
                            .font(.caption)
                    }
                    .disabled(model.undoInFlight)
                    // Undo never runs below CONFIRM_VOICE: the instruction
                    // came from a file on disk, not from the user. So the
                    // click produces a SPOKEN confirmation, not a reversal.
                    .help("Asks out loud before reversing “\(line.text)”.")
                }

                Button { HistoryWindowController.shared.show(model: model) } label: {
                    Image(systemName: "clock.arrow.circlepath")
                }
                .buttonStyle(.plain)
                .help("History, undo journal and set-up")

                Menu {
                    Button("Restart daa's brain") { model.supervisor.stop(); }
                    Button("Open log") {
                        NSWorkspace.shared.selectFile(model.supervisor.logPath,
                                                      inFileViewerRootedAtPath: "")
                    }
                    Divider()
                    Button("Quit daa") { NSApp.terminate(nil) }
                } label: {
                    Image(systemName: "ellipsis.circle")
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
    }
}

// MARK: - pieces

struct TranscriptRow: View {
    let line: TranscriptLine

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(label)
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.tertiary)
                .frame(width: 30, alignment: .leading)
            Text(line.text)
                .font(.callout)
                .foregroundStyle(tone)
                // A dry run is rendered in a distinct, quieter style so it can
                // never be mistaken for a real one.
                .italic(line.dryRun)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
            if line.undoID != nil && !line.dryRun {
                Image(systemName: "arrow.uturn.backward")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .help("This one can be undone.")
            }
        }
    }

    private var label: String {
        switch line.speaker {
        case .you: return "you"
        case .daa: return "daa"
        case .system: return "•"
        }
    }

    private var tone: Color {
        if line.refused { return .orange }
        if line.dryRun { return .secondary }
        return .primary
    }
}

/// Three bars, driven by Swift's own RMS. The audio never leaves this process
/// and the level is never sent to Python.
struct LevelMeter: View {
    let level: Double
    let active: Bool
    let hot: Bool

    var body: some View {
        HStack(alignment: .center, spacing: 2) {
            ForEach(0..<3, id: \.self) { i in
                Capsule()
                    .fill(active ? Color.primary.opacity(0.8) : Color.secondary.opacity(0.35))
                    .frame(width: 3, height: height(i))
            }
        }
        .frame(width: 16, height: 22)
        .overlay {
            // The hairline ring: an open mic at rest must not look like a
            // closed one.
            if hot && !active {
                Circle().strokeBorder(Color.secondary.opacity(0.5), lineWidth: 1)
                    .frame(width: 20, height: 20)
            }
        }
        .animation(.easeOut(duration: 0.08), value: level)
    }

    private func height(_ i: Int) -> Double {
        guard active else { return [6.0, 10.0, 6.0][i] }
        let base = [0.6, 1.0, 0.7][i]
        return max(4, min(22, 4 + level * 18 * base))
    }
}

struct IdentityBanner: View {
    let notice: IdentityNotice
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: icon).foregroundStyle(tone)
                Text(notice.title)
                    .font(.caption.weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                Button(expanded ? "Less" : "Why?") { expanded.toggle() }
                    .buttonStyle(.plain).font(.caption2).foregroundStyle(.secondary)
            }
            if expanded {
                Text(notice.body)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if let remedy = notice.remedy {
                    Text(remedy)
                        .font(.caption2.weight(.medium))
                        .fixedSize(horizontal: false, vertical: true)
                }
                Text("cdhash \(notice.cdhash.prefix(16))…")
                    .font(.caption2.monospaced())
                    .foregroundStyle(.tertiary)
                    .textSelection(.enabled)
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 9)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(tone.opacity(0.14))
    }

    private var tone: Color {
        switch notice.severity {
        case .loud: return .orange
        case .warning: return .yellow
        case .info: return .secondary
        }
    }

    private var icon: String {
        switch notice.severity {
        case .loud: return "exclamationmark.triangle.fill"
        case .warning: return "lock.trianglebadge.exclamationmark"
        case .info: return "info.circle"
        }
    }
}

/// Shown once, the first time always-on is switched on.
///
/// Every sentence here is enforced in `handle_chunk`, which is the difference
/// between a feature and a creep.
struct AlwaysOnExplainer: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label("Always on means the microphone stays open.", systemImage: "mic.fill")
                .font(.headline)
            Text("""
                daa will listen continuously. Every utterance goes through the address \
                gate first, and anything judged not to be addressed to daa is dropped \
                without being written down, transcribed by a cloud service, or sent to a \
                model.

                Speech is only recorded once it has woken daa. Until then the dock shows \
                a level meter and no text — including in this window.
                """)
                .font(.callout)
                .fixedSize(horizontal: false, vertical: true)
            if !model.dock.ready.jevLive {
                Label("""
                     The judgment model is not live in this build, so the address gate is \
                     not making real decisions yet.
                     """, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Spacer()
                Button("Not now") { model.alwaysOnExplainerPending = false }
                Button("Open the microphone") { model.commitAlwaysOn(true) }
                    .buttonStyle(.borderedProminent)
            }
        }
        .padding(22)
        .frame(width: 420)
    }
}
