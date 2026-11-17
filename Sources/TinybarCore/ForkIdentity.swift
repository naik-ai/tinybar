import Foundation

/// Who this build is: Modelbar, a personal fork of Tinybar that runs beside an installed upstream Tinybar
/// and never shares its bundle, preferences, data, login item or updates (ADR 0015).
public enum ForkIdentity {
    public static let bundleIdentifier = "ai.naik.modelbar"
    public static let appName = "Modelbar"
    /// The credit the MIT licence and the owner ask for.
    public static let credit = "Based on Tinybar by Thilo Reintjes, MIT licence"
    /// The fork's own folder under ~/Library/Application Support. Not upstream's `Tinybar`, and not
    /// `TinybarLocal`, which the local model backend owns. (Builds before the rename used
    /// `TinybarLocalApp`; Modelbar starts clean and leaves that folder as it is.)
    public static let appSupportFolder = "Modelbar"

    /// Upstream's identity, named only to refuse it.
    public static let upstreamBundleIdentifier = "com.tinybar.app"
    public static let upstreamAppSupportFolder = "Tinybar"

    /// Upstream checks GitHub and installs through its Homebrew cask (ADR 0007). The fork does
    /// neither: an update would replace it with upstream Tinybar.
    public static let selfUpdates = false
    /// Upstream registers itself at login on first run. The fork never does on its own.
    public static let registersAtLoginOnFirstRun = false

    /// Upstream's data folder. The fork never opens anything inside it.
    public static var upstreamAppSupport: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent(upstreamAppSupportFolder, isDirectory: true)
    }

    /// Whether `url` lies in upstream's data folder (links resolved).
    public static func isUpstreamData(_ url: URL, upstream: URL = upstreamAppSupport) -> Bool {
        // A file not made yet doesn't resolve: resolve its folder, then add its name back.
        let resolved = FileManager.default.fileExists(atPath: url.path)
            ? url.resolvingSymlinksInPath()
            : url.deletingLastPathComponent().resolvingSymlinksInPath().appendingPathComponent(url.lastPathComponent)
        let path = resolved.standardizedFileURL.path
        let root = upstream.resolvingSymlinksInPath().standardizedFileURL.path
        return path == root || path.hasPrefix(root + "/")
    }
}
