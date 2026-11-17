// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Tinybar",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "Tinybar", targets: ["Tinybar"]),
        .executable(name: "tinybar-cli", targets: ["tinybar-cli"]),
    ],
    targets: [
        .target(
            name: "TinybarCore",
            linkerSettings: [.linkedLibrary("sqlite3")]),
        // The local model gateway's wire types and client: Foundation only, no UI (ADR 0016).
        .target(name: "LocalModelCore"),
        .executableTarget(
            name: "Tinybar",
            dependencies: ["TinybarCore", "LocalModelCore"]),
        .executableTarget(
            name: "tinybar-cli",
            dependencies: ["TinybarCore"]),
        .testTarget(
            name: "TinybarCoreTests",
            dependencies: ["TinybarCore"]),
        .testTarget(
            name: "LocalModelCoreTests",
            dependencies: ["LocalModelCore"]),
    ])
