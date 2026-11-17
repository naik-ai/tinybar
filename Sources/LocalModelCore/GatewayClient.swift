import Foundation

/// What came back for one call: the decoded value with the status, the request id the gateway
/// gave it (`X-Request-Id`, the key into its ledger) and the body exactly as it arrived.
public struct GatewayResponse<Value: Sendable>: Sendable {
    public var value: Value
    public var exchange: Exchange
}

/// One HTTP exchange as it happened, for a body this client doesn't interpret (chat answers).
public struct Exchange: Sendable, Equatable {
    public var status: Int
    public var requestID: String?
    /// Lowercased names.
    public var headers: [String: String]
    public var body: Data
    /// Seconds from sending to the response headers, and to the last byte.
    public var secondsToHeaders: Double
    public var secondsTotal: Double

    public var retryAfter: Int? { headers["retry-after"].flatMap { Int($0.trimmingCharacters(in: .whitespaces)) } }
    public var isSuccess: Bool { (200..<300).contains(status) }

    public init(status: Int, requestID: String?, headers: [String: String], body: Data, secondsToHeaders: Double, secondsTotal: Double) {
        self.status = status
        self.requestID = requestID
        self.headers = headers
        self.body = body
        self.secondsToHeaders = secondsToHeaders
        self.secondsTotal = secondsTotal
    }

    /// The gateway's error envelope, when the body is one.
    public var errorEnvelope: ErrorEnvelope? { try? JSONDecoder().decode(ErrorEnvelope.self, from: body) }
}

/// What a streamed chat request yields, in order.
public enum StreamEvent: Sendable, Equatable {
    /// The answer started: its status and headers.
    case opened(status: Int, requestID: String?, headers: [String: String], secondsToHeaders: Double)
    /// One `data:` event: its text as sent and, when it is one, the chunk it holds.
    case event(raw: String, chunk: ChatChunk?)
    /// `data: [DONE]`.
    case done
    /// No stream: an error status, or a model that always answers with one JSON body.
    case whole(Exchange)
}

/// The local model gateway's client: loopback only, the key read from its file for each call and
/// never kept, no cookies, no cache, no proxy, no redirects. Every route the gateway serves.
public final class GatewayClient: Sendable {
    public let address: GatewayAddress
    public let keyFile: KeyFile
    /// Sent as `X-Client-Name`, so the ledger says who called.
    public let clientName: String
    private let session: URLSession

    public enum Timeout {
        public static let management: TimeInterval = 15
        /// A load waits until the worker is ready (the registry allows 600 s).
        public static let load: TimeInterval = 900
        /// Between bytes of a chat answer; the gateway itself gives a worker an hour.
        public static let chat: TimeInterval = 3600
    }

    public init(address: GatewayAddress, keyFile: KeyFile, clientName: String = "modelbar",
                configuration: URLSessionConfiguration = .ephemeral) {
        self.address = address
        self.keyFile = keyFile
        self.clientName = clientName
        let config = configuration.copy() as! URLSessionConfiguration
        config.urlCache = nil
        config.requestCachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        config.httpCookieStorage = nil
        config.httpShouldSetCookies = false
        config.urlCredentialStorage = nil
        config.connectionProxyDictionary = [:]
        config.waitsForConnectivity = false
        config.timeoutIntervalForResource = Timeout.chat
        session = URLSession(configuration: config, delegate: NoRedirects(), delegateQueue: nil)
    }

    deinit {
        session.finishTasksAndInvalidate()
    }

    // MARK: Routes

    /// `GET /health`: open, numbers only. No key is sent.
    public func health() async throws(GatewayError) -> GatewayResponse<Health> {
        try await decode(request("GET", "/health", authorized: false, timeout: 5))
    }

    public func models() async throws(GatewayError) -> GatewayResponse<ModelsResponse> {
        try await decode(request("GET", "/api/models"))
    }

    public func model(_ name: String) async throws(GatewayError) -> GatewayResponse<ModelInfo> {
        try await decode(request("GET", "/api/models/\(Self.segment(name))"))
    }

    public func status() async throws(GatewayError) -> GatewayResponse<GatewayStatus> {
        try await decode(request("GET", "/api/status"))
    }

    /// Starts the model's worker and waits until it is ready.
    public func load(_ name: String) async throws(GatewayError) -> GatewayResponse<LoadResult> {
        try await decode(request("POST", "/api/models/\(Self.segment(name))/load", body: Data("{}".utf8), timeout: Timeout.load))
    }

    /// Stops the worker once its requests finish.
    public func unload(_ name: String) async throws(GatewayError) -> GatewayResponse<UnloadResult> {
        try await decode(request("POST", "/api/models/\(Self.segment(name))/unload", body: Data("{}".utf8), timeout: 60))
    }

    public func pin(_ name: String, _ pinned: Bool) async throws(GatewayError) -> GatewayResponse<PinResult> {
        try await decode(request("POST", "/api/models/\(Self.segment(name))/pin", body: Data(#"{"pinned": \#(pinned)}"#.utf8)))
    }

    /// Switches a model on or off. Off: not served (403 `model_not_available`), unloaded once its
    /// in-flight work ends, and it wins over a pin.
    public func setAvailable(_ name: String, _ available: Bool) async throws(GatewayError) -> GatewayResponse<AvailableResult> {
        try await decode(request("POST", "/api/models/\(Self.segment(name))/available", body: Data(#"{"available": \#(available)}"#.utf8)))
    }

    /// Verifies or downloads the pinned weights (202); progress through `pulls()` or `model(_:)`.
    public func pull(_ name: String) async throws(GatewayError) -> GatewayResponse<PullProgress> {
        try await decode(request("POST", "/api/models/\(Self.segment(name))/pull", body: Data("{}".utf8)))
    }

    /// Stops a running pull (409 when none runs; newer gateways).
    public func cancelPull(_ name: String) async throws(GatewayError) -> GatewayResponse<PullProgress> {
        try await decode(request("POST", "/api/models/\(Self.segment(name))/pull/cancel", body: Data("{}".utf8)))
    }

    public func pulls() async throws(GatewayError) -> GatewayResponse<PullsResponse> {
        try await decode(request("GET", "/api/pulls"))
    }

    /// Recent ledger rows, newest first. The gateway serves 1 to 1,000. `model`, `outcome` and the
    /// `before` cursor filter on the gateway (newer gateways; older ones ignore them).
    public func requests(limit: Int = 100, model: String? = nil, outcome: String? = nil,
                         before: Double? = nil) async throws(GatewayError) -> GatewayResponse<RequestsResponse> {
        var q = [URLQueryItem(name: "limit", value: String(min(max(limit, 1), 1000)))]
        if let model { q.append(URLQueryItem(name: "model", value: model)) }
        if let outcome { q.append(URLQueryItem(name: "outcome", value: outcome)) }
        if let before { q.append(URLQueryItem(name: "before", value: String(before))) }
        return try await decode(request("GET", "/api/requests", query: q))
    }

    /// The tail of `gateway` or a model's log (at most 1,000 lines), or with `since` (an earlier
    /// `next`) only what was written after it.
    public func logs(source: String, lines: Int = 200, since: Int? = nil) async throws(GatewayError) -> GatewayResponse<LogsResponse> {
        var q = [URLQueryItem(name: "source", value: source), URLQueryItem(name: "lines", value: String(min(max(lines, 1), 1000)))]
        if let since { q.append(URLQueryItem(name: "since", value: String(since))) }
        return try await decode(request("GET", "/api/logs", query: q))
    }

    /// Stops every worker, then the gateway (202).
    public func shutdown() async throws(GatewayError) -> GatewayResponse<ShutdownResult> {
        try await decode(request("POST", "/api/shutdown", body: Data("{}".utf8)))
    }

    /// `GET /v1/models`: the downloaded entries, OpenAI's shape.
    public func openAIModels() async throws(GatewayError) -> GatewayResponse<OpenAIModelList> {
        try await decode(request("GET", "/v1/models"))
    }

    /// `POST /v1/chat/completions` with exactly these bytes. Any HTTP status comes back as an
    /// exchange; only a call that got no answer throws.
    public func chat(_ body: Data) async throws(GatewayError) -> Exchange {
        try await exchange(request("POST", "/v1/chat/completions", body: body, timeout: Timeout.chat))
    }

    /// `POST /v1/chat/completions` for a `stream: true` body, read as server-sent events as they
    /// arrive. Cancelling the consuming task cancels the request, and the gateway stops the worker.
    public func chatStream(_ body: Data) -> AsyncThrowingStream<StreamEvent, Error> {
        let built: URLRequest
        do {
            var r = try request("POST", "/v1/chat/completions", body: body, timeout: Timeout.chat)
            r.setValue("text/event-stream", forHTTPHeaderField: "Accept")
            built = r
        } catch {
            return AsyncThrowingStream { $0.finish(throwing: error) }
        }
        // The stream holds the client, so its session outlives the call that made the stream.
        return AsyncThrowingStream { continuation in
            let task = Task {
                let started = Date()
                do {
                    let (bytes, response) = try await self.session.bytes(for: built)
                    let http = try Self.http(response)
                    let headers = Self.headers(http)
                    let toHeaders = Date().timeIntervalSince(started)
                    let isEvents = headers["content-type"]?.contains("text/event-stream") == true
                    guard (200..<300).contains(http.statusCode), isEvents else {
                        var body = Data()
                        for try await byte in bytes { body.append(byte) }
                        continuation.yield(.whole(Exchange(
                            status: http.statusCode, requestID: headers["x-request-id"], headers: headers, body: body,
                            secondsToHeaders: toHeaders, secondsTotal: Date().timeIntervalSince(started))))
                        continuation.finish()
                        return
                    }
                    continuation.yield(.opened(status: http.statusCode, requestID: headers["x-request-id"], headers: headers,
                                               secondsToHeaders: toHeaders))
                    var done = false
                    for try await line in bytes.lines {
                        guard line.hasPrefix("data:") else { continue }
                        let payload = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
                        if payload == "[DONE]" {
                            done = true
                            continuation.yield(.done)
                            continue
                        }
                        let chunk = try? JSONDecoder().decode(ChatChunk.self, from: Data(payload.utf8))
                        continuation.yield(.event(raw: payload, chunk: chunk))
                    }
                    // A stream that just stops is not a complete answer.
                    if Task.isCancelled { throw CancellationError() }
                    if done { continuation.finish() } else { continuation.finish(throwing: GatewayError.interrupted) }
                } catch {
                    continuation.finish(throwing: Self.map(error))
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    // MARK: Requests

    /// A request to this gateway. Only 127.0.0.1 is ever addressed; the key is read now and goes
    /// into the Authorization header only.
    public func request(_ method: String, _ path: String, query: [URLQueryItem] = [], body: Data? = nil,
                        authorized: Bool = true, timeout: TimeInterval = Timeout.management) throws(GatewayError) -> URLRequest {
        var c = URLComponents(url: address.baseURL, resolvingAgainstBaseURL: false)!
        c.percentEncodedPath = path
        c.queryItems = query.isEmpty ? nil : query
        guard let url = c.url, GatewayAddress.isLoopback(url) else { throw .refusedHost(c.string ?? path) }
        var r = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData, timeoutInterval: timeout)
        r.httpMethod = method
        r.setValue("application/json", forHTTPHeaderField: "Accept")
        r.setValue(clientName, forHTTPHeaderField: "X-Client-Name")
        if let body {
            r.httpBody = body
            r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        if authorized {
            r.setValue("Bearer \(try keyFile.read())", forHTTPHeaderField: "Authorization")
        }
        return r
    }

    private func exchange(_ request: URLRequest) async throws(GatewayError) -> Exchange {
        guard GatewayAddress.isLoopback(request.url) else { throw .refusedHost(request.url?.absoluteString ?? "") }
        let started = Date()
        do {
            let (data, response) = try await session.data(for: request)
            let http = try Self.http(response)
            let headers = Self.headers(http)
            let total = Date().timeIntervalSince(started)
            return Exchange(status: http.statusCode, requestID: headers["x-request-id"], headers: headers, body: data,
                            secondsToHeaders: total, secondsTotal: total)
        } catch {
            throw Self.map(error)
        }
    }

    private func decode<T: Decodable & Sendable>(_ request: URLRequest) async throws(GatewayError) -> GatewayResponse<T> {
        let x = try await exchange(request)
        guard x.isSuccess else { throw .http(Self.failure(x)) }
        do {
            let value = try JSONDecoder().decode(T.self, from: x.body)
            return GatewayResponse(value: value, exchange: x)
        } catch {
            throw .malformed(status: x.status, requestID: x.requestID, detail: Self.decodingDetail(error))
        }
    }

    public static func failure(_ x: Exchange) -> GatewayError.HTTPFailure {
        let envelope = x.errorEnvelope
        let fallback = String(decoding: x.body.prefix(200), as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        return .init(status: x.status, message: envelope?.error.message ?? (fallback.isEmpty ? "no message" : fallback),
                     type: envelope?.error.type, requestID: x.requestID, retryAfter: x.retryAfter, body: x.body)
    }

    // MARK: Helpers

    private static func http(_ response: URLResponse) throws -> HTTPURLResponse {
        guard let http = response as? HTTPURLResponse else { throw GatewayError.transport("not an HTTP answer") }
        return http
    }

    private static func headers(_ http: HTTPURLResponse) -> [String: String] {
        var out: [String: String] = [:]
        for (k, v) in http.allHeaderFields {
            if let k = k as? String, let v = v as? String { out[k.lowercased()] = v }
        }
        return out
    }

    static func map(_ error: any Error) -> GatewayError {
        if let e = error as? GatewayError { return e }
        if error is CancellationError { return .cancelled }
        if let u = error as? URLError {
            switch u.code {
            case .cancelled: return .cancelled
            case .timedOut: return .timedOut
            case .cannotConnectToHost, .cannotFindHost, .dnsLookupFailed: return .notRunning
            default: return .transport(u.localizedDescription)
            }
        }
        return .transport(String(describing: error))
    }

    private static func decodingDetail(_ error: any Error) -> String {
        switch error {
        case let DecodingError.keyNotFound(key, ctx): "missing \(path(ctx.codingPath + [key]))"
        case let DecodingError.typeMismatch(_, ctx), let DecodingError.valueNotFound(_, ctx): "unexpected value at \(path(ctx.codingPath))"
        case DecodingError.dataCorrupted: "not JSON"
        default: "unreadable"
        }
    }

    private static func path(_ keys: [any CodingKey]) -> String {
        keys.map { $0.intValue.map(String.init) ?? $0.stringValue }.joined(separator: ".")
    }

    static func segment(_ name: String) -> String {
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "-._~")
        return name.addingPercentEncoding(withAllowedCharacters: allowed) ?? name
    }
}

/// Never follows a redirect: the answer is whatever the gateway itself said.
private final class NoRedirects: NSObject, URLSessionTaskDelegate, Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest) async -> URLRequest? {
        nil
    }
}
