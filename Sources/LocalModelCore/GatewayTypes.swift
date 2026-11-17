import Foundation

// The gateway's wire, as backend/tinybar_models/gateway.py, workers.py, ledger.py and store.py
// write it (17 November 2026). Field names are the gateway's own; anything it may leave out or
// send as null is optional here, and an unknown count stays nil, never 0.

/// `GET /health`, the one route without a key: numbers only.
public struct Health: Codable, Equatable, Sendable {
    public struct Memory: Codable, Equatable, Sendable {
        public var budgetTotalMb: Int
        public var reservedMb: Int
        public var footprintMb: Int
        public var loadedModels: Int
    }

    public var status: String
    public var memory: Memory
}

/// `Manager.totals()`: budgets held against the total, the measured footprint and counters.
public struct MemoryTotals: Codable, Equatable, Sendable {
    public var budgetTotalMb: Int
    public var reservedMb: Int
    public var footprintMb: Int
    public var loadedModels: Int
    public var startingModels: Int?
    public var stoppingModels: Int?
    public var evictions: Int?
    public var restarts: Int?
    public var abandoned: Int?
    public var inFlightTotal: Int?
    public var waitingTotal: Int?

    public init(budgetTotalMb: Int, reservedMb: Int, footprintMb: Int, loadedModels: Int, startingModels: Int? = nil,
                stoppingModels: Int? = nil, evictions: Int? = nil, restarts: Int? = nil, abandoned: Int? = nil) {
        self.budgetTotalMb = budgetTotalMb
        self.reservedMb = reservedMb
        self.footprintMb = footprintMb
        self.loadedModels = loadedModels
        self.startingModels = startingModels
        self.stoppingModels = stoppingModels
        self.evictions = evictions
        self.restarts = restarts
        self.abandoned = abandoned
    }
}

/// `GET /api/status`.
public struct GatewayStatus: Codable, Equatable, Sendable {
    public var memory: MemoryTotals
    public var ledgerRows: Int
    /// Requests refused before the key or at the door: `unauthenticated`, `host`, `origin`.
    public var refusals: [String: Int]
    // Added 17 Nov 2026; absent from older gateways and the stand-in.
    public var version: String?
    public var startedAt: String?
    public var uptimeSeconds: Double?
    public var port: Int?
    public var inFlightTotal: Int?
    public var waitingTotal: Int?
    /// `~`-relative.
    public var registryPath: String?
    public var defaults: [String: Int]?
    public var storeFreeGb: Double?
    // Availability and login start (owner's request, 17 Nov 2026).
    public var availableModels: Int?
    /// `launchd` or `manual`.
    public var startedBy: String?
    /// `installed` or `not installed`.
    public var loginItem: String?

    public var startedByLaunchd: Bool { startedBy == "launchd" }

    public init(memory: MemoryTotals, ledgerRows: Int, refusals: [String: Int], startedAt: String? = nil, port: Int? = nil,
                startedBy: String? = nil) {
        self.memory = memory
        self.ledgerRows = ledgerRows
        self.refusals = refusals
        self.startedAt = startedAt
        self.port = port
        self.startedBy = startedBy
    }
}

/// A model's lifecycle as the gateway reports it.
public enum ModelState: String, Codable, Sendable, CaseIterable {
    case registered, downloaded, starting, loaded, unloading
    /// A state this client doesn't know yet: shown as such, never failing the whole list.
    case unknown

    public init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = ModelState(rawValue: raw) ?? .unknown
    }

    /// What the workspace calls it: a starting worker is loading its model.
    public var label: String {
        switch self {
        case .registered: "Registered"
        case .downloaded: "Downloaded"
        case .starting: "Loading"
        case .loaded: "Loaded"
        case .unloading: "Unloading"
        case .unknown: "Unknown"
        }
    }
}

/// `GET /api/pulls` and `pull` inside a model: `store.Progress.to_dict()`.
public struct PullProgress: Codable, Equatable, Sendable {
    public var model: String
    /// queued, searching, verifying, downloading, cancelling, done, failed, cancelled.
    public var phase: String
    public var bytesDone: Int64
    public var bytesTotal: Int64
    public var ok: Bool?
    public var message: String?
    public var startedAt: Double
    public var finishedAt: Double?

    /// The file being verified or downloaded (newer gateways).
    public var currentFile: String?

    public var isFinished: Bool { finishedAt != nil }
    /// Asked to stop, not stopped yet: no new pull until it has.
    public var isCancelling: Bool { phase == "cancelling" && finishedAt == nil }
    public var fraction: Double? { bytesTotal > 0 ? min(Double(bytesDone) / Double(bytesTotal), 1) : nil }
}

/// One registry entry with its live state: `Manager.describe()`.
public struct ModelInfo: Codable, Equatable, Sendable, Identifiable {
    public struct Weights: Codable, Equatable, Sendable {
        /// `original` or `mlx`: which pinned source is (or would be) served.
        public var source: String
        public var repo: String
        public var revision: String
        /// The verified folder (`~`-relative on newer gateways); nil until a pull verified it.
        public var path: String?
        public var verifiedAt: String?
        /// Whether the pull found a copy already on disk rather than downloading.
        public var reused: Bool?
        public var sha256Pinned: Bool?
        public var filesVerified: Int?
        public var approxSizeGb: Double?
        /// Which source a pull downloads.
        public var download: String?
        public var alternative: Alternative?
    }

    public struct Alternative: Codable, Equatable, Sendable {
        public var source: String
        public var repo: String
        public var revision: String
        public var approxSizeGb: Double?
        public var commercialUse: String?
    }

    public struct Server: Codable, Equatable, Sendable {
        public var kind: String
        public var maxTokens: Int?
    }

    public struct Capabilities: Codable, Equatable, Sendable {
        public var imageInput: Bool?
        public var jsonSchemaOutput: Bool?
        public var boxes: Bool?
        public var instructions: String?
        public var output: String?
    }

    public struct Licence: Codable, Equatable, Sendable {
        public var id: String?
        public var readFrom: String?
        public var commercialUse: String?
    }

    public struct LastError: Codable, Equatable, Sendable {
        public var at: String?
        public var kind: String?
        public var message: String
    }

    public var name: String
    public var displayName: String
    public var state: ModelState
    public var work: [String]
    public var serverKind: String
    public var budgetMb: Int
    public var footprintMb: Int?
    public var pinned: Bool
    public var ttlSeconds: Int
    public var concurrency: Int
    public var queueLimit: Int
    public var lastUsedSecondsAgo: Double?
    public var loadSeconds: Double?
    public var inFlight: Int
    public var waiting: Int
    public var weights: Weights
    /// `allowed`, `not_allowed` or `unread`: only `allowed` is served.
    public var commercialUse: String
    public var pull: PullProgress?
    // Added 17 Nov 2026; absent from older gateways and the stand-in.
    /// `stopping` while a stopped worker's process still exits (state then says `unloading`).
    public var phase: String?
    public var server: Server?
    public var capabilities: Capabilities?
    public var licence: Licence?
    public var readyTimeoutSeconds: Int?
    /// The gateway's own count, when it sends one.
    public var idleSecondsLeftReported: Double?
    /// `registry` or `user`.
    public var pinnedBy: String?
    public var lastError: LastError?
    /// Whether the gateway serves it at all; nil from a gateway older than the switch.
    public var available: Bool?
    /// `registry default` or `user`.
    public var availableBy: String?

    /// Off: not served, takes no memory. Absent (older gateway) counts as on.
    public var isOff: Bool { available == false }

    enum CodingKeys: String, CodingKey {
        case name, displayName, state, work, serverKind, budgetMb, footprintMb, pinned, ttlSeconds, concurrency, queueLimit
        case lastUsedSecondsAgo, loadSeconds, inFlight, waiting, weights, commercialUse, pull
        case phase, server, capabilities, licence, readyTimeoutSeconds, pinnedBy, lastError, available, availableBy
        case idleSecondsLeftReported = "idleSecondsLeft"
    }

    public var id: String { name }
    public var isDownloaded: Bool { state != .registered }
    public var isIdle: Bool { inFlight == 0 && waiting == 0 }

    /// Seconds until an idle unpinned loaded model is unloaded, as of the reading; nil when it won't
    /// be. The gateway's own figure when it sends one.
    public var idleSecondsLeft: Double? {
        if let reported = idleSecondsLeftReported { return reported }
        guard state == .loaded, !pinned, isIdle, let ago = lastUsedSecondsAgo else { return nil }
        return max(Double(ttlSeconds) - ago, 0)
    }
}

/// `GET /api/models`.
public struct ModelsResponse: Codable, Equatable, Sendable {
    public var models: [ModelInfo]
    public var memory: MemoryTotals
}

/// `POST /api/models/<name>/load`.
public struct LoadResult: Codable, Equatable, Sendable {
    public var name: String
    public var state: String
    public var loadSeconds: Double?
}

/// `POST /api/models/<name>/unload`: `unloaded`, `unloading` or `not loaded`.
public struct UnloadResult: Codable, Equatable, Sendable {
    public var name: String
    public var state: String
}

/// `POST /api/models/<name>/available`. Every field optional: the answer's shape isn't fixed yet.
public struct AvailableResult: Codable, Equatable, Sendable {
    public var name: String?
    public var available: Bool?
    public var availableBy: String?
    public var state: String?
}

/// `POST /api/models/<name>/pin`.
public struct PinResult: Codable, Equatable, Sendable {
    public var name: String
    public var pinned: Bool
}

/// `GET /api/pulls`.
public struct PullsResponse: Codable, Equatable, Sendable {
    public var pulls: [PullProgress]
}

/// One ledger row (`ledger.py`): never any content. Snake case, as the ledger's columns.
public struct LedgerRow: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    public var client: String?
    public var model: String?
    public var route: String
    /// Seconds since 1970.
    public var startedAt: Double
    public var queueMs: Int?
    public var loadMs: Int?
    public var durationMs: Int?
    public var promptTokens: Int?
    public var completionTokens: Int?
    public var status: Int?
    /// ok, error, refused, cancelled, not_available; and from newer gateways in_flight, interrupted, partial.
    public var outcome: String
    public var errorKind: String?
    /// While in flight: queued, loading or generating.
    public var phase: String?
    /// While in flight: milliseconds so far.
    public var elapsedMs: Int?

    enum CodingKeys: String, CodingKey {
        case id, client, model, route, status, outcome, phase
        case startedAt = "started_at", queueMs = "queue_ms", loadMs = "load_ms", durationMs = "duration_ms"
        case promptTokens = "prompt_tokens", completionTokens = "completion_tokens", errorKind = "error_kind"
        case elapsedMs = "elapsed_ms"
    }

    public var isInFlight: Bool { outcome == "in_flight" }

    /// The outcome in a word for the table.
    public var outcomeLabel: String {
        switch outcome {
        case "in_flight": phase.map { "running: \($0)" } ?? "running"
        case "interrupted": "interrupted"
        case "partial": "partial"
        case "not_available": "off"
        default: isNotAvailable ? "off" : outcome
        }
    }

    public var started: Date { Date(timeIntervalSince1970: startedAt) }

    /// Refused because the model was switched off (an outcome or an error kind, by gateway version).
    public var isNotAvailable: Bool {
        outcome == "not_available" || errorKind == "model_not_available" || errorKind == "not_available"
    }

    /// The route in a word or two: "chat", "load", "status".
    public var routeName: String { Self.routeName(route) }

    public static func routeName(_ route: String) -> String {
        switch route {
        case "POST /v1/chat/completions": return "chat"
        case "GET /v1/models": return "models (OpenAI)"
        case "GET /api/models": return "models"
        case "GET /api/models/{name}": return "model"
        case "GET /api/status": return "status"
        case "GET /api/pulls": return "pulls"
        case "GET /api/requests": return "requests"
        case "GET /api/logs": return "logs"
        case "POST /api/shutdown": return "shutdown"
        case "POST /api/models/{name}/pull/cancel": return "pull cancel"
        default:
            let prefix = "POST /api/models/{name}/"
            if route.hasPrefix(prefix) { return String(route.dropFirst(prefix.count)) }
            return route
        }
    }

    /// Time spent generating: the total less waiting and loading. nil when unknown, and for a
    /// request that never reached a worker (no queue or load time recorded).
    public var generationMs: Int? {
        guard let d = durationMs, queueMs != nil || loadMs != nil else { return nil }
        return max(d - (queueMs ?? 0) - (loadMs ?? 0), 0)
    }
}

/// `GET /api/requests?limit=N`, newest first.
public struct RequestsResponse: Codable, Equatable, Sendable {
    public var requests: [LedgerRow]
    /// The cursor for the next (older) page: pass it as `before` (newer gateways).
    public var next: Double?
}

/// `GET /api/logs?source=…&lines=N`.
public struct LogsResponse: Codable, Equatable, Sendable {
    public var source: String
    public var lines: [String]
    /// A byte offset: pass it as `since` to get only what was written after (newer gateways).
    public var next: Int?
    /// With `since`: the file was rotated, and reading started over.
    public var rotated: Bool?

    public init(source: String, lines: [String], next: Int? = nil, rotated: Bool? = nil) {
        self.source = source
        self.lines = lines
        self.next = next
        self.rotated = rotated
    }
}

/// `POST /api/shutdown` (202).
public struct ShutdownResult: Codable, Equatable, Sendable {
    public var status: String
}

/// `GET /v1/models`: downloaded entries only, OpenAI's list shape.
public struct OpenAIModelList: Codable, Equatable, Sendable {
    public struct Entry: Codable, Equatable, Sendable {
        public var id: String
        public var object: String
        public var ownedBy: String?

        enum CodingKeys: String, CodingKey {
            case id, object
            case ownedBy = "owned_by"
        }
    }

    public var object: String
    public var data: [Entry]
}

/// The OpenAI error envelope every failure uses: `{"error": {message, type, code}}`.
public struct ErrorEnvelope: Codable, Equatable, Sendable {
    public struct Body: Codable, Equatable, Sendable {
        public var message: String
        public var type: String?
        public var code: Int?
        /// A code given as a word (`model_not_available`) rather than the status number.
        public var codeName: String?

        enum CodingKeys: String, CodingKey { case message, type, code }

        public init(message: String, type: String?, code: Int?, codeName: String? = nil) {
            self.message = message
            self.type = type
            self.code = code
            self.codeName = codeName
        }

        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            message = try c.decode(String.self, forKey: .message)
            type = try c.decodeIfPresent(String.self, forKey: .type)
            // The gateway sends the status number; OpenAI itself, and newer codes, send words.
            code = (try? c.decodeIfPresent(Int.self, forKey: .code)) ?? nil
            codeName = (try? c.decodeIfPresent(String.self, forKey: .code)) ?? nil
        }

        public func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(message, forKey: .message)
            try c.encodeIfPresent(type, forKey: .type)
            if let codeName { try c.encode(codeName, forKey: .code) } else { try c.encodeIfPresent(code, forKey: .code) }
        }
    }

    /// The model was switched off: not served until it is turned on.
    public var isModelNotAvailable: Bool {
        error.codeName == "model_not_available" || error.type == "model_not_available"
    }

    public var error: Body
}

/// Token counts a model reported. nil is unknown: never shown as 0.
public struct TokenUsage: Codable, Equatable, Sendable {
    public var promptTokens: Int?
    public var completionTokens: Int?
    public var totalTokens: Int?

    enum CodingKeys: String, CodingKey {
        case promptTokens = "prompt_tokens", completionTokens = "completion_tokens", totalTokens = "total_tokens"
    }

    public init(promptTokens: Int?, completionTokens: Int?, totalTokens: Int?) {
        self.promptTokens = promptTokens
        self.completionTokens = completionTokens
        self.totalTokens = totalTokens
    }
}

/// A chat completion, read only for what the workspace shows; the raw body is kept as it came.
public struct ChatCompletion: Codable, Equatable, Sendable {
    public struct Choice: Codable, Equatable, Sendable {
        public struct Message: Codable, Equatable, Sendable {
            public var role: String?
            public var content: String?
        }

        public var index: Int?
        public var message: Message?
        public var finishReason: String?

        enum CodingKeys: String, CodingKey {
            case index, message
            case finishReason = "finish_reason"
        }
    }

    public var id: String?
    public var model: String?
    public var systemFingerprint: String?
    public var choices: [Choice]
    public var usage: TokenUsage?

    enum CodingKeys: String, CodingKey {
        case id, model, choices, usage
        case systemFingerprint = "system_fingerprint"
    }

    public var text: String? { choices.first?.message?.content }
}

/// One server-sent event of a streamed completion.
public struct ChatChunk: Codable, Equatable, Sendable {
    public struct Choice: Codable, Equatable, Sendable {
        public struct Delta: Codable, Equatable, Sendable {
            public var role: String?
            public var content: String?
        }

        public var delta: Delta?
        public var finishReason: String?

        enum CodingKeys: String, CodingKey {
            case delta
            case finishReason = "finish_reason"
        }
    }

    public var id: String?
    public var choices: [Choice]?
    public var usage: TokenUsage?
}
