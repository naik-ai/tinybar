import Foundation

/// The parts of the backend's `registry.json` the workspace shows that the gateway's API doesn't
/// carry (licence ids, pinned checksums, capabilities, defaults). Read-only, from the backend folder.
public struct RegistryFile: Decodable, Equatable, Sendable {
    public struct Defaults: Decodable, Equatable, Sendable {
        public var ttlSeconds: Int?
        public var concurrency: Int?
        public var queueLimit: Int?
        public var readyTimeoutSeconds: Int?
    }

    public struct Source: Decodable, Equatable, Sendable {
        public var repo: String
        public var revision: String
        public var sha256: [String: String]?
    }

    public struct Licence: Decodable, Equatable, Sendable {
        public var id: String?
        public var commercialUse: String
        public var readFrom: String?
    }

    public struct Capabilities: Decodable, Equatable, Sendable {
        public var imageInput: Bool?
        public var jsonSchemaOutput: Bool?
        public var boxes: Bool?
        public var instructions: String?
        public var output: String?
    }

    public struct Entry: Decodable, Equatable, Sendable {
        public struct Weights: Decodable, Equatable, Sendable {
            public var original: Source?
            public var mlx: Source?
            public var download: String?
            public var approxSizeGb: [String: Double]?
        }

        public struct Server: Decodable, Equatable, Sendable {
            public var kind: String
            public var maxTokens: Int?
        }

        public var name: String
        public var displayName: String?
        public var work: [String]
        public var weights: Weights
        public var server: Server
        public var capabilities: Capabilities?
        public var licence: [String: Licence]
        public var ttlSeconds: Int?
        public var needs: String?

        public func source(_ name: String) -> Source? {
            name == "original" ? weights.original : name == "mlx" ? weights.mlx : nil
        }
    }

    public var schemaVersion: Int?
    public var memoryBudgetTotalMb: Int
    public var defaults: Defaults
    public var workKinds: [String: String]?
    public var models: [Entry]

    public func entry(_ name: String) -> Entry? { models.first { $0.name == name } }

    public static func load(_ url: URL) throws -> RegistryFile {
        try JSONDecoder().decode(RegistryFile.self, from: Data(contentsOf: url))
    }
}
