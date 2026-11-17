import Foundation

/// The gateway's key, read from its 0600 file at the moment of a call and never kept: not in
/// preferences, not in memory between calls, never logged or shown.
public struct KeyFile: Equatable, Sendable {
    public static let minimumLength = 32

    public let url: URL

    public init(url: URL) {
        self.url = url
    }

    /// `~/Library/Application Support/TinybarLocal/api-key`, or under `TINYBAR_LOCAL_HOME`, as the
    /// backend's `paths.key_file()` puts it.
    public static func standard(env: [String: String] = ProcessInfo.processInfo.environment,
                                home: URL = FileManager.default.homeDirectoryForCurrentUser) -> KeyFile {
        let folder: URL
        if let custom = env["TINYBAR_LOCAL_HOME"], !custom.isEmpty {
            folder = URL(fileURLWithPath: (custom as NSString).expandingTildeInPath, isDirectory: true)
        } else {
            folder = home.appendingPathComponent("Library/Application Support/TinybarLocal", isDirectory: true)
        }
        return KeyFile(url: folder.appendingPathComponent("api-key"))
    }

    public var exists: Bool { FileManager.default.fileExists(atPath: url.path) }

    /// The key, checked as the backend checks it: a regular file no one else can read, holding at
    /// least 32 characters. The caller puts it in one header and lets it go.
    public func read() throws(GatewayError) -> String {
        let path = url.path
        var info = stat()
        guard lstat(path, &info) == 0 else { throw .keyMissing(path: path) }
        // A link could point anywhere; the backend writes a plain file.
        guard (info.st_mode & S_IFMT) == S_IFREG else { throw .keyInsecure(path: path) }
        guard info.st_mode & 0o077 == 0, info.st_uid == getuid() else { throw .keyInsecure(path: path) }
        guard let data = FileManager.default.contents(atPath: path) else { throw .keyMissing(path: path) }
        let key = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        guard key.count >= Self.minimumLength, !key.contains(where: \.isNewline) else { throw .keyUnreadable(path: path) }
        return key
    }

    /// Whether the key can be read now, without returning it.
    public func check() -> GatewayError? {
        do {
            _ = try read()
            return nil
        } catch {
            return error
        }
    }
}
