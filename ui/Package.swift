// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "DaaDock",
    platforms: [.macOS("26.0")],
    products: [
        .executable(name: "daadock", targets: ["DaaDock"]),
        .library(name: "DaaDockCore", targets: ["DaaDockCore"]),
        .executable(name: "daadock-tests", targets: ["DaaDockTests"]),
        .executable(name: "daadock-snapshots", targets: ["DaaDockSnapshots"]),
    ],
    targets: [
        // Everything with a decision in it lives here, with no AppKit import,
        // so `swift test` covers it on a machine with no signing identity,
        // no microphone grant and no Xcode.
        .target(name: "DaaDockCore"),
        // Every view, and the app delegate. A library so that the offscreen
        // snapshot renderer can draw the very same views the app shows.
        .target(name: "DaaDockUI", dependencies: ["DaaDockCore"]),
        .executableTarget(name: "DaaDock", dependencies: ["DaaDockUI"]),
        // `make snapshots`: renders every screen to PNG offscreen. No window is
        // ever put on screen, nothing is captured, and no TCC permission is
        // involved -- each view draws itself into a bitmap.
        .executableTarget(name: "DaaDockSnapshots", dependencies: ["DaaDockUI", "DaaDockCore"]),
        // Command Line Tools ship no XCTest and no swift-testing, so `swift
        // test` cannot run on a machine without Xcode. The suite is an
        // ordinary executable instead: `swift run daadock-tests`.
        .target(name: "MicroTest"),
        .executableTarget(name: "DaaDockTests", dependencies: ["DaaDockCore", "MicroTest"]),
    ]
)
