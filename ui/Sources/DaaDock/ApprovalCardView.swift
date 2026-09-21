import SwiftUI
import AppKit
import DaaDockCore

/// The most safety-critical screen in the product.
///
/// Every rule here is a deliberate piece of friction, and each one has a
/// reason attached, because the first change anyone will want to make to this
/// file is to make it smaller and friendlier. A card someone learns to click
/// through is worse than no card, because it manufactures a consent record for
/// a decision nobody made.
struct ApprovalCardView: View {
    @Environment(AppModel.self) private var model
    let gate: ApprovalGate
    /// Driven by a 30 Hz tick so the countdown, the inert fade and the hold
    /// fill all move without the view owning any state of its own.
    let now: TimeInterval

    private var card: ApprovalCard { gate.card }
    private var block: ApprovalBlock? { gate.block(at: now) }
    private var inert: Bool { if case .inert = block { return true }; return false }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    headline
                    flags
                    // Script args FIRST. `_SCRIPT_KEYS` already encodes which
                    // arguments are programs rather than references to them,
                    // and they are the reason this tier exists.
                    ForEach(card.scriptArguments) { arg in
                        scriptBlock(arg)
                    }
                    if !card.targets.isEmpty { targets }
                    if !card.otherArguments.isEmpty { otherArgs }
                    judgment
                }
                .padding(22)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            // The scroll gate. Measured against real scroll geometry, not an
            // `onAppear` on a sentinel view: a plain VStack inside a ScrollView
            // renders every child eagerly, so an onAppear would fire for
            // content that has never been on screen and silently delete the
            // rule. When the content fits, this is true from the first layout
            // and no friction is invented.
            .onScrollGeometryChange(for: Bool.self) { geo in
                geo.contentOffset.y + geo.containerSize.height >= geo.contentSize.height - 4
            } action: { _, atBottom in
                if atBottom { model.markScriptFullySeen() }
            }
            Divider()
            footer
        }
        .frame(width: 560)
        .frame(minHeight: 360, maxHeight: 720)
        .background(.regularMaterial)
        .opacity(inert ? 0.55 : 1)
        .animation(.easeOut(duration: ApprovalGate.inertFor), value: inert)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("daa needs your approval")
    }

    // MARK: - header

    private var header: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
                    .font(.title3)
                VStack(alignment: .leading, spacing: 2) {
                    Text("daa needs your approval")
                        .font(.headline)
                    Text("This one can't be approved by voice.")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }
            if card.dryRun { dryRunBanner }
        }
        .padding(.horizontal, 22)
        .padding(.top, 18)
        .padding(.bottom, 14)
    }

    /// Otherwise the card teaches people to approve reflexively during
    /// testing, and they carry the habit into production.
    private var dryRunBanner: some View {
        Label {
            Text("Dry run — approving this will not actually run it.")
                .font(.callout.weight(.medium))
        } icon: {
            Image(systemName: "testtube.2")
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.blue.opacity(0.14), in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Color.blue.opacity(0.35)))
    }

    // MARK: - the sentence

    /// Verb first, largest type on the card. The README's own example — "delete
    /// report.pdf" and "reveal report.pdf" read back identically — is the bug
    /// this prevents, and leading with the filename would reintroduce it.
    private var headline: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("I would")
                .font(.callout)
                .foregroundStyle(.secondary)
            Text(card.phrase)
                .font(.system(size: 21, weight: .semibold))
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("I would \(card.phrase)")
    }

    private var flags: some View {
        VStack(alignment: .leading, spacing: 8) {
            // The most dangerous actions are the ones nobody asked for.
            if !card.explicit {
                flag("I inferred this — you didn't ask for it", icon: "flag.fill", tone: .orange)
            }
            // Omitting these makes the confirmation a lie, which is why the
            // type system makes them hard to drop on the Python side.
            ForEach(card.consequences, id: \.key) { c in
                flag(c.text, icon: "flag.fill", tone: .red)
            }
            if card.assessment.synthetic {
                flag("daa could not get a real judgment, so it is assuming the worst.",
                     icon: "questionmark.diamond.fill", tone: .purple)
            }
        }
    }

    private func flag(_ text: String, icon: String, tone: Color) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: icon).foregroundStyle(tone).font(.caption)
            Text(text)
                .font(.callout)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: - THIS RUNS

    /// Never truncated, never behind a disclosure triangle, never scroll-locked
    /// away. If it is 400 lines, the card grows and scrolls — and Approve stays
    /// disabled until the bottom has been on screen.
    private func scriptBlock(_ arg: ApprovalArgument) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            sectionLabel(card.scriptArguments.count > 1 ? "THIS RUNS — \(arg.key)" : "THIS RUNS")
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(arg.lines.enumerated()), id: \.offset) { _, line in
                    Text(line.isEmpty ? " " : line)
                        .font(.system(.callout, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color(nsColor: .textBackgroundColor).opacity(0.7),
                        in: RoundedRectangle(cornerRadius: 8))
            .overlay(RoundedRectangle(cornerRadius: 8)
                .strokeBorder(Color.primary.opacity(0.15)))
            Text("\(arg.lines.count) line\(arg.lines.count == 1 ? "" : "s"), shown in full")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
    }

    private var targets: some View {
        VStack(alignment: .leading, spacing: 6) {
            sectionLabel("TARGETS (\(card.targets.count))")
            ForEach(Array(card.targets.enumerated()), id: \.offset) { _, t in
                Text("• \(t)")
                    .font(.callout)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// Every argument, in full, sorted. "Nothing elided and nothing summarised."
    private var otherArgs: some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionLabel("ARGUMENTS (\(card.otherArguments.count))")
            ForEach(card.otherArguments) { arg in
                VStack(alignment: .leading, spacing: 3) {
                    Text(arg.key)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                    ForEach(Array(arg.lines.enumerated()), id: \.offset) { _, line in
                        Text(line.isEmpty ? " " : line)
                            .font(.system(.callout, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }

    // MARK: - why

    /// Collapsed but present. The user should be able to see why Jev
    /// escalated, and the calibrated numbers are honest enough to show.
    private var judgment: some View {
        DisclosureGroup {
            VStack(alignment: .leading, spacing: 7) {
                row("tool", card.tool)
                row("tier", card.tier)
                row("reason", card.reason)
                HStack(spacing: 10) {
                    Text("blast radius")
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                        .frame(width: 110, alignment: .leading)
                    meter(card.assessment.blastRadius / 3)
                    Text(String(format: "%.1f / 3", card.assessment.blastRadius))
                        .font(.caption.monospaced())
                }
                row("unrecoverable", String(format: "%.2f", card.assessment.unrecoverable))
                row("explicitly asked", String(format: "%.2f", card.assessment.explicitlyRequested))
                row("confidence", String(format: "%.2f", card.assessment.confidence))
                row("targets", card.assessment.targetConfidence)
                row("judgment", card.assessment.synthetic ? "SYNTHETIC (not a real judgment)" : "live")
            }
            .padding(.top, 8)
        } label: {
            Text("Why daa is asking")
                .font(.callout.weight(.medium))
        }
    }

    private func row(_ k: String, _ v: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(k)
                .font(.caption.monospaced())
                .foregroundStyle(.secondary)
                .frame(width: 110, alignment: .leading)
            Text(v)
                .font(.caption.monospaced())
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
    }

    private func meter(_ fraction: Double) -> some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 3).fill(Color.primary.opacity(0.12))
                RoundedRectangle(cornerRadius: 3)
                    .fill(Color.orange)
                    .frame(width: max(2, geo.size.width * min(1, max(0, fraction))))
            }
        }
        .frame(width: 120, height: 7)
    }

    private func sectionLabel(_ t: String) -> some View {
        Text(t)
            .font(.caption.weight(.bold))
            .kerning(0.8)
            .foregroundStyle(.secondary)
    }

    // MARK: - footer

    private var footer: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let b = block, b == .unread {
                Label("Scroll to the end of the script before you can approve it.",
                      systemImage: "arrow.down.circle")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            HStack(spacing: 12) {
                // Silence is a refusal, and the user should know that.
                Label(gate.countdownText(at: now), systemImage: "clock")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(gate.secondsRemaining(at: now) < 15 ? .orange : .secondary)
                Text("Auto-cancels — doing nothing is a no.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                // Esc = Cancel. The ONLY keyboard shortcut on this card.
                Button("Cancel") { model.resolveApproval(.cancelled) }
                    .keyboardShortcut(.cancelAction)
                HoldToApproveButton(
                    enabled: gate.canApprove(at: now),
                    progress: gate.holdProgress(at: now),
                    onDown: { model.beginHold() },
                    onUp: { model.endHold() })
            }
        }
        .padding(.horizontal, 22)
        .padding(.vertical, 14)
    }
}

/// Press and hold for 600 ms, with a fill.
///
/// This is the replacement for `"yes"`-not-`"y"`: the friction is the feature.
/// A hold cannot be produced by a stray Return, by a double-click landing on a
/// card that appeared mid-click, or by the Return key at all — there is
/// deliberately **no default button and no keyboard shortcut on Approve**.
struct HoldToApproveButton: View {
    let enabled: Bool
    let progress: Double
    let onDown: () -> Void
    let onUp: () -> Void

    @State private var down = false

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 7)
                .fill(Color.accentColor.opacity(enabled ? 0.18 : 0.07))
            GeometryReader { geo in
                RoundedRectangle(cornerRadius: 7)
                    .fill(Color.accentColor.opacity(0.55))
                    .frame(width: geo.size.width * progress)
            }
            .clipShape(RoundedRectangle(cornerRadius: 7))
            Text(progress > 0 ? "Keep holding…" : "Approve · hold")
                .font(.callout.weight(.medium))
                .foregroundStyle(enabled ? Color.primary : Color.secondary)
        }
        .frame(width: 148, height: 26)
        .overlay(RoundedRectangle(cornerRadius: 7)
            .strokeBorder(Color.accentColor.opacity(enabled ? 0.6 : 0.2)))
        .contentShape(Rectangle())
        .gesture(
            DragGesture(minimumDistance: 0)
                .onChanged { _ in
                    guard enabled, !down else { return }
                    down = true
                    onDown()
                }
                .onEnded { _ in
                    down = false
                    onUp()
                }
        )
        .disabled(!enabled)
        .help("Press and hold to approve. A click is not enough, on purpose.")
        .accessibilityLabel("Approve by holding")
        .accessibilityHint("Press and hold for six tenths of a second. A single click will not approve this.")
        .accessibilityValue(enabled ? "available" : "not yet available")
    }
}
