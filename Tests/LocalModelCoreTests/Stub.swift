import Foundation
@testable import LocalModelCore

/// Answers URLSession requests in tests: one handler at a time (the suites using it are serialized),
/// every request recorded with its body.
final class StubProtocol: URLProtocol, @unchecked Sendable {
    struct Reply {
        var status = 200
        var headers: [String: String] = ["Content-Type": "application/json"]
        var chunks: [Data] = []
        var error: URLError?
        /// Never answers: for cancellation.
        var hang = false

        static func json(_ status: Int, _ body: String, headers: [String: String] = [:], requestID: String? = "req_test") -> Reply {
            var h = ["Content-Type": "application/json"].merging(headers) { $1 }
            if let requestID { h["X-Request-Id"] = requestID }
            return Reply(status: status, headers: h, chunks: [Data(body.utf8)])
        }
    }

    struct Seen {
        var method: String
        var url: URL
        var headers: [String: String]
        var body: Data?
    }

    nonisolated(unsafe) static var handler: (URLRequest) -> Reply = { _ in Reply(error: URLError(.cannotConnectToHost)) }
    nonisolated(unsafe) static var seen: [Seen] = []
    private static let lock = NSLock()

    static func reset(_ handler: @escaping (URLRequest) -> Reply) {
        lock.lock(); defer { lock.unlock() }
        self.handler = handler
        seen = []
    }

    static var requests: [Seen] {
        lock.lock(); defer { lock.unlock() }
        return seen
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        var body = request.httpBody
        if body == nil, let stream = request.httpBodyStream {
            body = Data()
            stream.open()
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let n = stream.read(&buffer, maxLength: buffer.count)
                if n <= 0 { break }
                body!.append(buffer, count: n)
            }
            stream.close()
        }
        Self.lock.lock()
        Self.seen.append(Seen(method: request.httpMethod ?? "GET", url: request.url!, headers: request.allHTTPHeaderFields ?? [:], body: body))
        let reply = Self.handler(request)
        Self.lock.unlock()
        if reply.hang { return }
        if let error = reply.error {
            client?.urlProtocol(self, didFailWithError: error)
            return
        }
        let response = HTTPURLResponse(url: request.url!, statusCode: reply.status, httpVersion: "HTTP/1.1", headerFields: reply.headers)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        for chunk in reply.chunks { client?.urlProtocol(self, didLoad: chunk) }
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

enum Fixture {
    /// A key file in a fresh folder, mode 0600 unless told otherwise.
    static func keyFile(_ key: String = String(repeating: "k", count: 43), mode: Int = 0o600) throws -> KeyFile {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("lmc-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent("api-key")
        try Data((key + "\n").utf8).write(to: url)
        try FileManager.default.setAttributes([.posixPermissions: mode], ofItemAtPath: url.path)
        return KeyFile(url: url)
    }

    static func client(_ key: KeyFile? = nil, port: Int = 18990) throws -> GatewayClient {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubProtocol.self]
        return GatewayClient(address: GatewayAddress(port: port)!, keyFile: try key ?? keyFile(), configuration: config)
    }

    static let model = #"""
    {"name": "qwen3.5-4b", "displayName": "Qwen3.5-4B (4-bit MLX), small vision chat", "state": "loaded",
     "work": ["check-reading", "extract-fields", "chat"], "serverKind": "mlx-vlm", "budgetMb": 6144,
     "footprintMb": 3851, "pinned": false, "ttlSeconds": 600, "concurrency": 1, "queueLimit": 8,
     "lastUsedSecondsAgo": 42.5, "loadSeconds": 2.6, "inFlight": 0, "waiting": 0,
     "weights": {"source": "mlx", "repo": "mlx-community/Qwen3.5-4B-MLX-4bit",
                 "revision": "32f3e8ecf65426fc3306969496342d504bfa13f3", "path": "/tmp/weights"},
     "commercialUse": "allowed"}
    """#

    static let registered = #"""
    {"name": "qwen3.5-27b", "displayName": "Qwen3.5-27B (4-bit MLX)", "state": "registered",
     "work": ["chat"], "serverKind": "mlx-vlm", "budgetMb": 22528, "footprintMb": null, "pinned": false,
     "ttlSeconds": 300, "concurrency": 1, "queueLimit": 8, "lastUsedSecondsAgo": null, "loadSeconds": null,
     "inFlight": 0, "waiting": 0,
     "weights": {"source": "mlx", "repo": "mlx-community/Qwen3.5-27B-4bit", "revision": "45797d2985a12c55e6473686e9ea91b95e959553", "path": null},
     "commercialUse": "allowed",
     "pull": {"model": "qwen3.5-27b", "phase": "downloading", "bytesDone": 4000000000, "bytesTotal": 16080000000,
              "ok": null, "message": null, "startedAt": 1794900000.5, "finishedAt": null}}
    """#

    static let totals = #"""
    {"budgetTotalMb": 49152, "reservedMb": 6144, "footprintMb": 3851, "loadedModels": 1, "startingModels": 0,
     "stoppingModels": 0, "evictions": 2, "restarts": 0, "abandoned": 1}
    """#
}
