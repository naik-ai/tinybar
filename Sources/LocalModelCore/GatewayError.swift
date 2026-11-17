import Foundation

/// Everything a gateway call can end in, short of success. Messages never hold the key.
public enum GatewayError: Error, Equatable, Sendable {
    /// The configured address is not http://127.0.0.1:<port>.
    case invalidAddress(String)
    /// The key file isn't there (the gateway writes it on its first start).
    case keyMissing(path: String)
    /// The key file is readable by group or others, or not a file; it is not used.
    case keyInsecure(path: String)
    /// The key file holds no usable key (under 32 characters).
    case keyUnreadable(path: String)
    /// Nothing listens at the address.
    case notRunning
    /// No answer within the time allowed.
    case timedOut
    /// The caller cancelled.
    case cancelled
    /// A redirect, or any URL off the loopback host: refused before anything is sent.
    case refusedHost(String)
    /// The gateway answered with an error status.
    case http(HTTPFailure)
    /// The gateway answered 2xx with a body this client can't read.
    case malformed(status: Int, requestID: String?, detail: String)
    /// Any other transport failure.
    case transport(String)
    /// A streamed answer ended without its `[DONE]`: what arrived is incomplete.
    case interrupted

    public struct HTTPFailure: Equatable, Sendable {
        public var status: Int
        public var message: String
        public var type: String?
        public var requestID: String?
        /// Seconds, from Retry-After.
        public var retryAfter: Int?
        public var body: Data

        public init(status: Int, message: String, type: String?, requestID: String?, retryAfter: Int?, body: Data) {
            self.status = status
            self.message = message
            self.type = type
            self.requestID = requestID
            self.retryAfter = retryAfter
            self.body = body
        }
    }

    /// One line for the workspace.
    public var summary: String {
        switch self {
        case let .invalidAddress(why): "Address not allowed. \(why)"
        case let .keyMissing(path): "No key file at \(path). The gateway writes it on its first start."
        case let .keyInsecure(path): "The key file \(path) can be read by other users; run chmod 600 on it."
        case let .keyUnreadable(path): "The key file \(path) holds no usable key."
        case .notRunning: "The gateway is not running."
        case .timedOut: "The gateway did not answer in time."
        case .cancelled: "Cancelled."
        case let .refusedHost(url): "Refused to call \(url): only 127.0.0.1 is allowed."
        case let .http(f): Self.describe(f)
        case let .malformed(status, _, detail): "HTTP \(status) with a body this app can't read: \(detail)"
        case let .transport(detail): "Couldn't reach the gateway: \(detail)"
        case .interrupted: "Interrupted: the answer is incomplete."
        }
    }

    public var requestID: String? {
        switch self {
        case let .http(f): f.requestID
        case let .malformed(_, id, _): id
        default: nil
        }
    }

    public var status: Int? {
        switch self {
        case let .http(f): f.status
        case let .malformed(status, _, _): status
        default: nil
        }
    }

    /// A 403 because the model is switched off.
    public var isModelNotAvailable: Bool {
        if case let .http(f) = self, f.status == 403,
           let e = try? JSONDecoder().decode(ErrorEnvelope.self, from: f.body) { return e.isModelNotAvailable }
        return false
    }

    public var isKeyProblem: Bool {
        switch self {
        case .keyMissing, .keyInsecure, .keyUnreadable: true
        case let .http(f): f.status == 401
        default: false
        }
    }

    private static func describe(_ f: HTTPFailure) -> String {
        let what: String = switch f.status {
        case 400: "Bad request"
        case 401: "Key rejected"
        case 403: "Refused"
        case 404: "Not found"
        case 409: "Conflict"
        case 413: "Too large"
        case 502: "Model failed"
        case 503: "Busy"
        case 504: "Model timed out"
        default: "HTTP \(f.status)"
        }
        let retry = f.retryAfter.map { " Retry after \($0) s." } ?? ""
        return "\(what) (\(f.status)): \(f.message)\(retry)"
    }
}
