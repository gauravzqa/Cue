import Foundation
import AVFoundation
import Speech
import DaaDockCore

/// Microphone, VAD and on-device transcription, all in Swift.
///
/// This is the half of the architecture that pays for itself twice:
///
/// 1. **TCC.** The microphone grant lands on the signed `.app` — one stable
///    bundle identifier — instead of on `.venv/bin/python`, which can never
///    hold a stable grant. Python never opens an audio device at all.
/// 2. **Bundle size.** `SpeechDetector` + `SpeechTranscriber` replace
///    `silero-vad` + `torch` + `sounddevice` and the `LocalTranscriber` stub,
///    which is the difference between a ~100 MB bundle and a multi-GB one.
///
/// Measured against `evals/address_gate_cases.jsonl` before this was written:
/// 8.1–9.0% WER and ~100 ms per utterance on synthesised speech, stable across
/// two voices. Good enough to replace the stub; see `ui/README.md` for the
/// caveat about the word "daa" itself, which it never once got right.
@MainActor
final class SpeechEngine {

    enum Failure: Equatable {
        case micDenied
        case micRestricted
        case modelUnavailable(String)
        case engineFailed(String)
        case noLocale
    }

    /// Partial text, only ever shown under push-to-talk.
    var onPartial: ((String) -> Void)?
    /// A complete utterance. `complete` distinguishes a silence-cut from a
    /// release-cut, which is what feeds the gate's honest `end_of_turn`.
    var onUtterance: ((_ text: String, _ confidence: Double, _ complete: Bool,
                       _ startedAt: TimeInterval) -> Void)?
    /// Speech started. Drives barge-in, so it must be cheap and early.
    var onSpeechOnset: (() -> Void)?
    var onLevel: ((Double) -> Void)?
    var onFailure: ((Failure) -> Void)?
    /// First-run model download, as real UI rather than a silent stall.
    var onModelDownload: ((Double) -> Void)?

    private(set) var running = false

    private let engine = AVAudioEngine()
    private var analyzer: SpeechAnalyzer?
    private var transcriber: SpeechTranscriber?
    private var detector: SpeechDetector?
    private var inputContinuation: AsyncStream<AnalyzerInput>.Continuation?
    private var tasks: [Task<Void, Never>] = []
    private var utteranceStartedAt: TimeInterval = 0
    private var accumulated = ""
    private var speaking = false

    // MARK: - permission

    /// Asks for the microphone. Under the plan this prompt appears against
    /// `ai.daa.dock` — the moment the architecture pays for itself.
    func requestAccess() async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return true
        case .notDetermined: return await AVCaptureDevice.requestAccess(for: .audio)
        case .denied: onFailure?(.micDenied); return false
        case .restricted: onFailure?(.micRestricted); return false
        @unknown default: onFailure?(.micDenied); return false
        }
    }

    // MARK: - lifecycle

    func start() async {
        guard !running else { return }
        guard await requestAccess() else { return }

        var picked = await SpeechTranscriber.supportedLocale(equivalentTo: Locale.current)
        if picked == nil {
            picked = await SpeechTranscriber.supportedLocale(equivalentTo: Locale(identifier: "en-US"))
        }
        guard let locale = picked else {
            onFailure?(.noLocale)
            return
        }

        let t = SpeechTranscriber(
            locale: locale,
            transcriptionOptions: [],
            // `volatileResults` is what makes a live partial possible at all.
            // It is shown ONLY under push-to-talk; see `DockState.mayShowPartial`.
            reportingOptions: [.volatileResults],
            attributeOptions: [.transcriptionConfidence])
        let d = SpeechDetector(
            detectionOptions: .init(sensitivityLevel: .medium),
            reportResults: true)

        // The model assets may need a first-run download. Treat it as an
        // onboarding step with real UI; never fail silently.
        let status = await AssetInventory.status(forModules: [t, d])
        if status != .installed {
            do {
                if let request = try await AssetInventory.assetInstallationRequest(supporting: [t, d]) {
                    onModelDownload?(0)
                    try await request.downloadAndInstall()
                    onModelDownload?(1)
                }
            } catch {
                onFailure?(.modelUnavailable("\(error)"))
                return
            }
        }

        guard let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [t, d]) else {
            onFailure?(.modelUnavailable("no compatible audio format"))
            return
        }

        let (stream, continuation) = AsyncStream<AnalyzerInput>.makeStream()
        inputContinuation = continuation
        transcriber = t
        detector = d

        let a = SpeechAnalyzer(inputSequence: stream, modules: [t, d])
        analyzer = a

        tasks.append(Task { [weak self] in await self?.consumeTranscripts(t) })
        tasks.append(Task { [weak self] in await self?.consumeDetection(d) })

        do { try await a.start(inputSequence: stream) } catch {
            onFailure?(.engineFailed("\(error)"))
            return
        }

        do { try startCapture(target: format) } catch {
            onFailure?(.engineFailed("\(error)"))
            return
        }
        running = true
    }

    func stop() async {
        guard running else { return }
        running = false
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        inputContinuation?.finish()
        inputContinuation = nil
        try? await analyzer?.finalizeAndFinishThroughEndOfInput()
        for t in tasks { t.cancel() }
        tasks = []
        analyzer = nil
        transcriber = nil
        detector = nil
    }

    // MARK: - capture

    private func startCapture(target: AVAudioFormat) throws {
        let input = engine.inputNode
        let native = input.outputFormat(forBus: 0)
        // The converter belongs to the audio thread, not to the main actor:
        // the tap runs on a realtime thread and must not hop actors per buffer.
        let converter = ConverterBox(from: native, to: target)
        let sink = inputContinuation

        input.installTap(onBus: 0, bufferSize: 2048, format: native) { [weak self] buffer, _ in
            let level = Self.rms(buffer)
            Task { @MainActor [weak self] in self?.onLevel?(level) }
            guard let converted = converter.convert(buffer, to: target) else { return }
            sink?.yield(AnalyzerInput(buffer: converted))
        }

        engine.prepare()
        try engine.start()
    }

    private nonisolated static func rms(_ buffer: AVAudioPCMBuffer) -> Double {
        guard let data = buffer.floatChannelData, buffer.frameLength > 0 else { return 0 }
        let n = Int(buffer.frameLength)
        var sum: Float = 0
        for i in 0..<n { let v = data[0][i]; sum += v * v }
        let rms = Double((sum / Float(n)).squareRoot())
        // Roughly -50 dBFS .. 0 dBFS mapped to 0..1, which is what a level
        // meter actually needs.
        let db = 20 * log10(max(rms, 1e-6))
        return min(1, max(0, (db + 50) / 50))
    }

    // MARK: - results

    private func consumeTranscripts(_ t: SpeechTranscriber) async {
        do {
            for try await result in t.results {
                let text = String(result.text.characters)
                guard !text.isEmpty else { continue }
                if utteranceStartedAt == 0 { utteranceStartedAt = Date().timeIntervalSince1970 }
                // A volatile result is a partial; anything else is final for
                // its range and is appended.
                if isVolatile(result) {
                    onPartial?(accumulated + text)
                } else {
                    accumulated += text
                    onPartial?(accumulated)
                }
            }
        } catch {
            onFailure?(.engineFailed("transcriber: \(error)"))
        }
    }

    private func isVolatile(_ result: SpeechTranscriber.Result) -> Bool {
        // A finalized result's `resultsFinalizationTime` has advanced past the
        // end of its own range; a volatile one has not.
        result.resultsFinalizationTime < result.range.end
    }

    private func consumeDetection(_ d: SpeechDetector) async {
        do {
            for try await result in d.results {
                if result.speechDetected && !speaking {
                    speaking = true
                    utteranceStartedAt = Date().timeIntervalSince1970
                    // Barge-in. The Python side already has `_on_speech_start`.
                    onSpeechOnset?()
                } else if !result.speechDetected && speaking {
                    speaking = false
                    // A silence cut is an honest end of turn.
                    flush(complete: true)
                }
            }
        } catch {
            onFailure?(.engineFailed("detector: \(error)"))
        }
    }

    /// Called on hotkey release: the user stopped talking on purpose.
    func endUtteranceFromRelease() {
        flush(complete: true)
    }

    /// Called on Esc: throw the buffer away without sending it anywhere.
    func discard() {
        accumulated = ""
        utteranceStartedAt = 0
        onPartial?("")
    }

    private func flush(complete: Bool) {
        let text = accumulated.trimmingCharacters(in: .whitespacesAndNewlines)
        accumulated = ""
        let started = utteranceStartedAt
        utteranceStartedAt = 0
        guard !text.isEmpty else { return }
        onUtterance?(text, 0.9, complete, started)
        onPartial?("")
    }
}

/// Owns an `AVAudioConverter` on the audio thread.
///
/// The tap runs on a realtime thread and must not hop actors per buffer, so
/// the converter cannot live on the main actor with the rest of the engine.
final class ConverterBox: @unchecked Sendable {
private let converter: AVAudioConverter?

init(from: AVAudioFormat, to: AVAudioFormat) {
    converter = AVAudioConverter(from: from, to: to)
}

func convert(_ buffer: AVAudioPCMBuffer,
             to target: AVAudioFormat) -> AVAudioPCMBuffer? {
    guard let converter else { return nil }
    if buffer.format == target { return buffer }
    let ratio = target.sampleRate / buffer.format.sampleRate
    let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 1024
    guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return nil }
    var supplied = false
    var error: NSError?
    converter.convert(to: out, error: &error) { _, status in
        if supplied { status.pointee = .noDataNow; return nil }
        supplied = true
        status.pointee = .haveData
        return buffer
    }
    return error == nil && out.frameLength > 0 ? out : nil
}
}
