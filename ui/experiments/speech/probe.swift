import Foundation
import Speech
import AVFoundation

struct Out: Codable { var wav: String; var text: String; var err: String?; var ms: Int }

@main
struct Probe {
    static func main() async {
        let args = Array(CommandLine.arguments.dropFirst())
        guard !args.isEmpty else { FileHandle.standardError.write("usage: probe <wav>...\n".data(using:.utf8)!); exit(2) }

        let locale = Locale(identifier: "en-US")
        let supported = await SpeechTranscriber.supportedLocale(equivalentTo: locale)
        FileHandle.standardError.write("isAvailable=\(SpeechTranscriber.isAvailable) supported=\(String(describing: supported))\n".data(using:.utf8)!)
        let installed = await SpeechTranscriber.installedLocales
        FileHandle.standardError.write("installed=\(installed.map{$0.identifier})\n".data(using:.utf8)!)

        let probe = SpeechTranscriber(locale: supported ?? locale, preset: .transcription)
        let status = await AssetInventory.status(forModules: [probe])
        FileHandle.standardError.write("assetStatus=\(status)\n".data(using:.utf8)!)
        if status != .installed {
            do {
                if let req = try await AssetInventory.assetInstallationRequest(supporting: [probe]) {
                    FileHandle.standardError.write("downloading model assets...\n".data(using:.utf8)!)
                    try await req.downloadAndInstall()
                    FileHandle.standardError.write("installed.\n".data(using:.utf8)!)
                }
            } catch {
                FileHandle.standardError.write("asset install failed: \(error)\n".data(using:.utf8)!)
            }
        }

        var outs: [Out] = []
        for path in args {
            let t0 = DispatchTime.now()
            var text = ""
            var err: String? = nil
            do {
                let transcriber = SpeechTranscriber(locale: supported ?? locale, preset: .transcription)
                let file = try AVAudioFile(forReading: URL(fileURLWithPath: path))
                let analyzer = SpeechAnalyzer(modules: [transcriber])
                let collector = Task { () -> String in
                    var acc = ""
                    for try await r in transcriber.results {
                        acc += String(r.text.characters)
                    }
                    return acc
                }
                _ = try await analyzer.analyzeSequence(from: file)
                try await analyzer.finalizeAndFinishThroughEndOfInput()
                text = try await collector.value
            } catch {
                err = "\(error)"
            }
            let ms = Int(Double(DispatchTime.now().uptimeNanoseconds - t0.uptimeNanoseconds) / 1e6)
            outs.append(Out(wav: path, text: text, err: err, ms: ms))
            FileHandle.standardError.write("\(path): \(err ?? text)\n".data(using:.utf8)!)
        }
        let data = try! JSONEncoder().encode(outs)
        FileHandle.standardOutput.write(data)
    }
}
