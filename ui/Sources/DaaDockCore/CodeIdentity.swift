import Foundation
import Security

/// How this build is signed, as the system sees it.
public struct SigningIdentity: Sendable, Equatable, Codable {
    /// The code directory hash. Under an ad-hoc signature this IS the
    /// designated requirement, so it changes on every rebuild.
    public var cdhash: String
    public var teamID: String?
    public var isAdHoc: Bool
    public var isSigned: Bool
    public var bundleID: String?

    public init(cdhash: String, teamID: String? = nil, isAdHoc: Bool,
                isSigned: Bool = true, bundleID: String? = nil) {
        self.cdhash = cdhash
        self.teamID = teamID
        self.isAdHoc = isAdHoc
        self.isSigned = isSigned
        self.bundleID = bundleID
    }

    /// True when TCC grants will survive a rebuild.
    ///
    /// A certificate-based designated requirement pins `identifier` +
    /// `anchor apple generic` + `subject.OU`, none of which change when you
    /// rebuild. An ad-hoc DR is the cdhash, which changes when one string in
    /// the source changes.
    public var grantsSurviveRebuild: Bool { isSigned && !isAdHoc && teamID != nil }
}

/// What changed since the last launch, and therefore what macOS has forgotten.
public enum IdentityVerdict: Sendable, Equatable {
    /// Nothing stored yet.
    case firstRun(SigningIdentity)
    /// Same code, stable grants.
    case unchanged(SigningIdentity)
    /// The cdhash moved under an ad-hoc signature. Every TCC grant this app
    /// held is gone, and macOS will prompt again — or, worse, silently fail
    /// for the permissions that have no prompt.
    case rebuiltAdHoc(previous: String, current: SigningIdentity)
    /// The cdhash moved but the signature is certificate-based, so the DR is
    /// stable and grants carry. Informational only.
    case rebuiltStable(previous: String, current: SigningIdentity)
    /// We moved from a stable identity to an ad-hoc one (or lost signing).
    case downgraded(previous: SigningIdentity, current: SigningIdentity)
    /// Could not read our own signature at all.
    case unknown(String)

    /// True when the user must be told something, loudly, before they wonder
    /// why the microphone stopped working.
    public var demandsExplanation: Bool {
        switch self {
        case .unchanged: return false
        case .firstRun(let id): return !id.grantsSurviveRebuild
        default: return true
        }
    }
}

/// The comparison, with no filesystem and no Security framework in it, so the
/// interesting half is testable on a machine with zero signing identities —
/// which is exactly the machine this was written on.
public enum CodeIdentityCheck {

    public static func verdict(current: SigningIdentity, previous: SigningIdentity?) -> IdentityVerdict {
        guard let previous else { return .firstRun(current) }
        if previous.cdhash == current.cdhash { return .unchanged(current) }
        if previous.grantsSurviveRebuild && !current.grantsSurviveRebuild {
            return .downgraded(previous: previous, current: current)
        }
        if current.grantsSurviveRebuild {
            return .rebuiltStable(previous: previous.cdhash, current: current)
        }
        return .rebuiltAdHoc(previous: previous.cdhash, current: current)
    }

    /// The words the user sees. Written out in full rather than assembled from
    /// fragments, because the whole point of this feature is that the failure
    /// explains itself instead of being mysterious.
    public static func explanation(_ verdict: IdentityVerdict) -> IdentityNotice? {
        switch verdict {
        case .unchanged:
            return nil

        case .firstRun(let id) where !id.grantsSurviveRebuild:
            return IdentityNotice(
                severity: .warning,
                title: "This build is ad-hoc signed, so its permissions are temporary.",
                body: """
                macOS remembers privacy permissions against a signature, and an ad-hoc \
                signature's identity is the hash of the build itself. Change one line, \
                rebuild, and macOS sees a different app.

                Anything you grant daa now — Microphone, Speech Recognition, \
                Accessibility, Automation — will be forgotten the next time this app is \
                rebuilt. That is expected, not a bug, and it stops as soon as builds are \
                signed with an Apple Development certificate.
                """,
                remedy: "Sign with an Apple Development certificate (Apple Developer Program, $99/yr). Nothing in the code has to change.",
                cdhash: id.cdhash)

        case .firstRun:
            return nil

        case .rebuiltAdHoc(let previous, let current):
            return IdentityNotice(
                severity: .loud,
                title: "daa was rebuilt. macOS has forgotten its permissions.",
                body: """
                This app's code signature changed since it last ran:

                    was   \(short(previous))
                    now   \(short(current.cdhash))

                This build is ad-hoc signed, which means its signature IS that hash. To \
                macOS this is a brand-new app that happens to have the same name, so \
                every privacy permission you granted the previous build is gone.

                You will be asked for the microphone again. Accessibility and Automation \
                have no prompt at all — they will simply not work until you re-add daa in \
                System Settings, and daa cannot tell you which one failed until it tries.
                """,
                remedy: "Re-grant in System Settings › Privacy & Security, or sign builds with an Apple Development certificate so this stops happening.",
                cdhash: current.cdhash)

        case .rebuiltStable(_, let current):
            return IdentityNotice(
                severity: .info,
                title: "daa was updated. Permissions carried over.",
                body: "The build changed but the signing certificate did not, so macOS still recognises this as the same app.",
                remedy: nil,
                cdhash: current.cdhash)

        case .downgraded(let previous, let current):
            return IdentityNotice(
                severity: .loud,
                title: "daa lost its stable signing identity.",
                body: """
                The previous build was signed with a certificate\
                \(previous.teamID.map { " (team \($0))" } ?? ""), so macOS kept its \
                permissions across rebuilds. This build is \
                \(current.isSigned ? "ad-hoc signed" : "unsigned").

                Every privacy permission is gone, and will be gone again on the next \
                rebuild. This is almost always an accidentally dropped `codesign` flag.
                """,
                remedy: "Check the signing step in ui/tools/make-app.sh and set DAA_SIGN_IDENTITY.",
                cdhash: current.cdhash)

        case .unknown(let why):
            return IdentityNotice(
                severity: .loud,
                title: "daa cannot read its own code signature.",
                body: """
                \(why)

                Without a readable signature there is no way to tell whether macOS still \
                associates this build with the permissions you granted earlier. Treat \
                every permission as unknown until this is fixed.
                """,
                remedy: "Rebuild and re-sign the app bundle.",
                cdhash: "?")
        }
    }

    static func short(_ hash: String) -> String {
        hash.count > 16 ? String(hash.prefix(16)) + "…" : hash
    }
}

public struct IdentityNotice: Sendable, Equatable {
    public enum Severity: Sendable, Equatable { case info, warning, loud }
    public var severity: Severity
    public var title: String
    public var body: String
    public var remedy: String?
    public var cdhash: String
}

// MARK: - Reading the real thing

public struct IdentityReadError: Error, Equatable, Sendable {
    public let message: String
    public init(_ message: String) { self.message = message }
}

public enum CodeIdentityReader {

    /// Ask the system what it thinks this process is.
    public static func readSelf() -> Result<SigningIdentity, IdentityReadError> {
        var codeRef: SecCode?
        let selfStatus = SecCodeCopySelf(SecCSFlags(), &codeRef)
        guard selfStatus == errSecSuccess, let code = codeRef else {
            return .failure(IdentityReadError("SecCodeCopySelf failed (OSStatus \(selfStatus))"))
        }
        var staticRef: SecStaticCode?
        let staticStatus = SecCodeCopyStaticCode(code, SecCSFlags(), &staticRef)
        guard staticStatus == errSecSuccess, let staticCode = staticRef else {
            return .failure(IdentityReadError("SecCodeCopyStaticCode failed (OSStatus \(staticStatus))"))
        }
        var infoRef: CFDictionary?
        let flags = SecCSFlags(rawValue: kSecCSSigningInformation | kSecCSRequirementInformation)
        let infoStatus = SecCodeCopySigningInformation(staticCode, flags, &infoRef)
        guard infoStatus == errSecSuccess, let info = infoRef as? [String: Any] else {
            return .failure(IdentityReadError("SecCodeCopySigningInformation failed (OSStatus \(infoStatus)) — the app is probably unsigned"))
        }

        guard let unique = info[kSecCodeInfoUnique as String] as? Data else {
            return .failure(IdentityReadError("no code directory hash in the signature — unsigned build"))
        }
        let cdhash = unique.map { String(format: "%02x", $0) }.joined()

        // kSecCodeSignatureAdhoc == 0x0002 in <Security/CSCommon.h>.
        let signFlags = (info[kSecCodeInfoFlags as String] as? UInt32) ?? 0
        let adhoc = (signFlags & 0x0002) != 0
        let team = info[kSecCodeInfoTeamIdentifier as String] as? String

        return .success(SigningIdentity(
            cdhash: cdhash,
            teamID: team,
            // A signature with no team identifier and no certificate chain is
            // ad-hoc whether or not the flag says so; belt and braces, because
            // getting this wrong means telling the user their grants are safe
            // when they are not.
            isAdHoc: adhoc || team == nil,
            isSigned: true,
            bundleID: info[kSecCodeInfoIdentifier as String] as? String))
    }
}

/// Persists the last-seen identity OUTSIDE the app bundle.
///
/// Writing anything into a signed bundle breaks its seal, so this lives in
/// Application Support next to the rest of daa's state.
public struct IdentityStore: Sendable {
    public let url: URL

    public init(url: URL) { self.url = url }

    public static func defaultURL(bundleID: String = "ai.daa.dock") -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Application Support")
        return base.appendingPathComponent(bundleID, isDirectory: true)
            .appendingPathComponent("identity.json")
    }

    public func load() -> SigningIdentity? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(SigningIdentity.self, from: data)
    }

    public func save(_ id: SigningIdentity) {
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        guard let data = try? JSONEncoder().encode(id) else { return }
        try? data.write(to: url, options: .atomic)
    }

    /// Read, compare, store, and return what to tell the user. Called once at
    /// launch, before anything asks for a permission.
    public func checkAndRecord(current: Result<SigningIdentity, IdentityReadError>) -> IdentityVerdict {
        switch current {
        case .failure(let why):
            return .unknown(why.message)
        case .success(let id):
            let verdict = CodeIdentityCheck.verdict(current: id, previous: load())
            save(id)
            return verdict
        }
    }
}
