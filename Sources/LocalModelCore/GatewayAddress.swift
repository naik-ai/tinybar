import Foundation

/// The gateway's address: plain http on 127.0.0.1 and a port, nothing else. `localhost` is refused
/// too, as it may resolve to ::1 where the gateway does not listen, and a name can be re-pointed.
public struct GatewayAddress: Equatable, Sendable, CustomStringConvertible {
    public static let loopback = "127.0.0.1"
    public static let defaultPort = 18900
    public static let `default` = GatewayAddress(port: defaultPort)!

    public let port: Int

    public init?(port: Int) {
        guard (1025...65535).contains(port) else { return nil }
        self.port = port
    }

    /// From what a user typed: `http://127.0.0.1:18900`, with or without a trailing slash.
    public init(parsing text: String) throws(GatewayError) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let c = URLComponents(string: trimmed), c.scheme?.lowercased() == "http" else {
            throw .invalidAddress("Use http://127.0.0.1:<port>.")
        }
        guard c.host == Self.loopback else {
            throw .invalidAddress("Only 127.0.0.1 is allowed, not \(c.host.map { "‘\($0)’" } ?? "an empty host").")
        }
        guard c.user == nil, c.password == nil, c.query == nil, c.fragment == nil, c.path.isEmpty || c.path == "/" else {
            throw .invalidAddress("Give the address only, with no path, user or query.")
        }
        guard let port = c.port, let address = GatewayAddress(port: port) else {
            throw .invalidAddress("Give a port from 1025 to 65535.")
        }
        self = address
    }

    public var baseURL: URL { URL(string: "http://\(Self.loopback):\(port)")! }
    public var description: String { "http://\(Self.loopback):\(port)" }

    /// Whether a URL is on this loopback host (redirects and any request built elsewhere).
    public static func isLoopback(_ url: URL?) -> Bool {
        guard let url, url.scheme?.lowercased() == "http" else { return false }
        return url.host == loopback
    }
}
