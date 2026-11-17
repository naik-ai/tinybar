import Foundation
import LocalModelCore
import Observation

/// The Models workspace's preferences, in the fork's own defaults domain. Only places and an
/// interval: never the key, never anything typed or returned in Try.
@MainActor
@Observable
final class LocalModelsSettings {
    private let defaults: UserDefaults
    /// Off for snapshots, which show states without choosing them for the user.
    private let savesChanges: Bool

    private nonisolated enum Key {
        static let address = "localModels.address"
        static let backendFolder = "localModels.backendFolder"
        static let keyFile = "localModels.keyFile"
        static let pollSeconds = "localModels.pollSeconds"
        static let menuText = "localModels.menuText"
        static let healthSeconds = "localModels.healthSeconds"
    }

    static let pollRange = 2...60
    static let defaultPollSeconds = 5

    var address: GatewayAddress {
        didSet { if savesChanges { defaults.set(address.description, forKey: Key.address) } }
    }

    /// The backend's folder (holding `registry.json` and `.venv/bin/tinybar-models`); nil when not
    /// set and not found beside a development build.
    var backendFolder: URL? {
        didSet { if savesChanges { defaults.set(backendFolder?.path, forKey: Key.backendFolder) } }
    }

    var keyFile: KeyFile {
        didSet { if savesChanges { defaults.set(keyFile.url.path, forKey: Key.keyFile) } }
    }

    var pollSeconds: Int {
        didSet { if savesChanges { defaults.set(pollSeconds, forKey: Key.pollSeconds) } }
    }

    /// Loaded count and memory beside the menu-bar glyph ("2 · 9 GB"). Off by default.
    var menuText: Bool {
        didSet { if savesChanges { defaults.set(menuText, forKey: Key.menuText) } }
    }

    nonisolated static let healthRange = 5...300
    nonisolated static let defaultHealthSeconds = 15

    /// How often the menu bar asks `/health` while the popover is closed (60 s once it is stopped).
    var healthSeconds: Int {
        didSet { if savesChanges { defaults.set(healthSeconds, forKey: Key.healthSeconds) } }
    }

    nonisolated static func healthSeconds(_ defaults: UserDefaults = .standard) -> Int {
        let v = defaults.integer(forKey: Key.healthSeconds)
        return healthRange.contains(v) ? v : defaultHealthSeconds
    }

    nonisolated static func menuText(_ defaults: UserDefaults = .standard) -> Bool { defaults.bool(forKey: Key.menuText) }

    nonisolated static func address(_ defaults: UserDefaults = .standard) -> GatewayAddress {
        defaults.string(forKey: Key.address).flatMap { try? GatewayAddress(parsing: $0) } ?? .default
    }

    nonisolated static func keyFile(_ defaults: UserDefaults = .standard) -> KeyFile {
        defaults.string(forKey: Key.keyFile).map { KeyFile(url: URL(fileURLWithPath: $0)) } ?? .standard()
    }

    init(defaults: UserDefaults = .standard, savesChanges: Bool = true) {
        self.defaults = defaults
        self.savesChanges = savesChanges
        address = defaults.string(forKey: Key.address).flatMap { try? GatewayAddress(parsing: $0) } ?? .default
        backendFolder = defaults.string(forKey: Key.backendFolder).map { URL(fileURLWithPath: $0, isDirectory: true) }
            ?? Self.repositoryBackend()
        keyFile = defaults.string(forKey: Key.keyFile).map { KeyFile(url: URL(fileURLWithPath: $0)) } ?? .standard()
        let poll = defaults.integer(forKey: Key.pollSeconds)
        pollSeconds = Self.pollRange.contains(poll) ? poll : Self.defaultPollSeconds
        menuText = defaults.bool(forKey: Key.menuText)
        healthSeconds = Self.healthSeconds(defaults)
    }

    /// Back to what a fresh install would use.
    func reset() {
        address = .default
        backendFolder = Self.repositoryBackend()
        keyFile = .standard()
        pollSeconds = Self.defaultPollSeconds
    }

    var cli: URL? { backendFolder?.appendingPathComponent(".venv/bin/tinybar-models") }
    var registryFile: URL? { backendFolder?.appendingPathComponent("registry.json") }

    /// A development build runs from inside the repository (`build/Modelbar.app` or
    /// `.build/…/Tinybar`): its `backend/` folder, when it has a registry.
    nonisolated static func repositoryBackend(from places: [URL] = [Bundle.main.bundleURL, Bundle.main.executableURL].compactMap { $0 }) -> URL? {
        for start in places {
            var dir = start.deletingLastPathComponent()
            for _ in 0..<8 {
                let backend = dir.appendingPathComponent("backend", isDirectory: true)
                if FileManager.default.fileExists(atPath: backend.appendingPathComponent("registry.json").path),
                   FileManager.default.fileExists(atPath: dir.appendingPathComponent("Package.swift").path) {
                    return backend
                }
                let parent = dir.deletingLastPathComponent()
                if parent.path == dir.path { break }
                dir = parent
            }
        }
        return nil
    }
}
