import Foundation

/// Builds chat-completions bodies in the shapes the gateway's gate and each worker accept
/// (`wire.check_chat_body`, `nemotron_server.py`, `jev_server.py`). The body is encoded once,
/// pretty-printed with sorted keys, and those exact bytes are what is shown and sent.
public enum ChatRequest {
    /// The gateway refuses bodies over 25 MB.
    public static let maxBodyBytes = 25 * 1024 * 1024

    public enum BuildError: Error, Equatable, Sendable {
        case noModel
        case emptyMessage
        case needsImage
        case badSchema(String)
        case badBlocks(String)
        case badImage(String)
        case tooLarge(Int)
        case notJSON(String)

        public var summary: String {
            switch self {
            case .noModel: "Choose a model."
            case .emptyMessage: "Write a message or pick an image."
            case .needsImage: "This model reads one page image: pick one."
            case let .badSchema(why): "The JSON schema isn't usable: \(why)"
            case let .badBlocks(why): "The blocks aren't usable: \(why)"
            case let .badImage(why): why
            case let .tooLarge(bytes): "The request is \(bytes / 1_048_576) MB; the gateway takes at most 25 MB."
            case let .notJSON(why): "Not JSON: \(why)"
            }
        }
    }

    /// A generic chat request for the mlx-vlm models: system and user text, an optional image, and
    /// optional max tokens, temperature, JSON schema output and streaming.
    public static func chat(model: String, system: String, user: String, imageDataURL: String?, maxTokens: Int?,
                            temperature: Double?, jsonSchema: String?, stream: Bool) throws(BuildError) -> Data {
        guard !model.isEmpty else { throw .noModel }
        var messages: [JSONValue] = []
        let sys = system.trimmingCharacters(in: .whitespacesAndNewlines)
        if !sys.isEmpty { messages.append(.object(["role": .string("system"), "content": .string(system)])) }
        let text = user.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty || imageDataURL != nil else { throw .emptyMessage }
        if let imageDataURL {
            var parts: [JSONValue] = [.object(["type": .string("image_url"), "image_url": .object(["url": .string(imageDataURL)])])]
            if !text.isEmpty { parts.append(.object(["type": .string("text"), "text": .string(user)])) }
            messages.append(.object(["role": .string("user"), "content": .array(parts)]))
        } else {
            messages.append(.object(["role": .string("user"), "content": .string(user)]))
        }
        var body: [String: JSONValue] = ["model": .string(model), "messages": .array(messages)]
        if let maxTokens { body["max_tokens"] = .number(Double(maxTokens)) }
        if let temperature { body["temperature"] = .number(temperature) }
        if let jsonSchema, !jsonSchema.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            let schema: JSONValue
            do { schema = try JSONValue.parse(jsonSchema) } catch { throw .badSchema("it isn't JSON") }
            guard case .object = schema else { throw .badSchema("it must be a JSON object") }
            body["response_format"] = .object([
                "type": .string("json_schema"),
                "json_schema": .object(["name": .string("answer"), "schema": schema, "strict": .bool(true)]),
            ])
        }
        if stream {
            body["stream"] = .bool(true)
            // Asks for the usage chunk at the end, so token counts are known.
            body["stream_options"] = .object(["include_usage": .bool(true)])
        }
        return try sized(JSONValue.object(body).pretty())
    }

    /// Nemotron-3.5-Parse: exactly one page image; text is ignored by the model, so none is sent.
    public static func nemotron(model: String, imageDataURL: String?, maxTokens: Int?) throws(BuildError) -> Data {
        guard !model.isEmpty else { throw .noModel }
        guard let imageDataURL else { throw .needsImage }
        var body: [String: JSONValue] = [
            "model": .string(model),
            "messages": .array([.object(["role": .string("user"), "content": .array([
                .object(["type": .string("image_url"), "image_url": .object(["url": .string(imageDataURL)])]),
            ])])]),
        ]
        if let maxTokens { body["max_tokens"] = .number(Double(maxTokens)) }
        return try sized(JSONValue.object(body).pretty())
    }

    /// The Jev scorer: one page image and one text part holding `{"blocks": [{block, text, box}]}`,
    /// checked as `jev_server.parse_blocks` checks it.
    public static func jev(model: String, imageDataURL: String?, blocks: String) throws(BuildError) -> Data {
        guard !model.isEmpty else { throw .noModel }
        guard let imageDataURL else { throw .needsImage }
        let parsed: JSONValue
        do { parsed = try JSONValue.parse(blocks) } catch { throw .badBlocks("they aren't JSON") }
        try checkBlocks(parsed)
        // The text part is the blocks object as compact JSON, as a reader's output would be.
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        let text = String(decoding: (try? encoder.encode(parsed)) ?? Data(), as: UTF8.self)
        let body: JSONValue = .object([
            "model": .string(model),
            "messages": .array([.object(["role": .string("user"), "content": .array([
                .object(["type": .string("image_url"), "image_url": .object(["url": .string(imageDataURL)])]),
                .object(["type": .string("text"), "text": .string(text)]),
            ])])]),
        ])
        return try sized(body.pretty())
    }

    /// Exactly the bytes typed, after checking they are JSON. Never re-encoded.
    public static func raw(_ text: String) throws(BuildError) -> Data {
        let data = Data(text.utf8)
        do { _ = try JSONValue.parse(data) } catch { throw .notJSON("check the brackets and quotes") }
        return try sized(data)
    }

    /// Whether a body asks for a stream.
    public static func wantsStream(_ body: Data) -> Bool {
        (try? JSONValue.parse(body))?["stream"] == .bool(true)
    }

    static func checkBlocks(_ value: JSONValue) throws(BuildError) {
        guard let blocks = value["blocks"]?.arrayValue, !blocks.isEmpty else { throw .badBlocks(#"give {"blocks": [...]} with at least one block"#) }
        var seen = Set<Double>()
        for b in blocks {
            guard let n = b["block"]?.numberValue, n.rounded() == n, seen.insert(n).inserted else {
                throw .badBlocks("each block needs a unique whole number \"block\"")
            }
            guard b["text"]?.stringValue != nil else { throw .badBlocks("block \(Int(n)): \"text\" must be a string") }
            switch b["box"] {
            case nil, .null?: continue
            case let .array(box)?:
                let v = box.compactMap(\.numberValue)
                guard v.count == 4, box.count == 4, 0 <= v[0], v[0] < v[2], v[2] <= 1, 0 <= v[1], v[1] < v[3], v[3] <= 1 else {
                    throw .badBlocks("block \(Int(n)): \"box\" must be [x1, y1, x2, y2] page fractions with x1 < x2 and y1 < y2")
                }
            default:
                throw .badBlocks("block \(Int(n)): \"box\" must be [x1, y1, x2, y2] or null")
            }
        }
    }

    private static func sized(_ data: Data) throws(BuildError) -> Data {
        guard data.count <= maxBodyBytes else { throw .tooLarge(data.count) }
        return data
    }
}

/// An image file as the data URL the gate accepts: PNG, JPEG or WebP by their first bytes.
public enum ImageDataURL {
    public static func make(_ data: Data) throws(ChatRequest.BuildError) -> String {
        guard let type = mediaType(data) else { throw .badImage("Only PNG, JPEG or WebP images can be sent.") }
        guard data.count <= ChatRequest.maxBodyBytes * 3 / 4 else { throw .tooLarge(data.count * 4 / 3) }
        return "data:\(type);base64," + data.base64EncodedString()
    }

    public static func mediaType(_ data: Data) -> String? {
        let b = [UInt8](data.prefix(12))
        if b.starts(with: [0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) { return "image/png" }
        if b.starts(with: [0xFF, 0xD8, 0xFF]) { return "image/jpeg" }
        if b.count == 12, b.starts(with: Array("RIFF".utf8)), Array(b[8..<12]) == Array("WEBP".utf8) { return "image/webp" }
        return nil
    }
}

/// For showing a body: long data URLs cut to a marker, so a page image doesn't fill the view. The
/// bytes sent are untouched.
public enum BodyDisplay {
    public static func elidingImages(_ text: String, keep: Int = 48) -> String {
        guard let regex = try? NSRegularExpression(pattern: #"data:image/[a-z]+;base64,([A-Za-z0-9+/=]{200,})"#) else { return text }
        let ns = text as NSString
        var out = text
        for m in regex.matches(in: text, range: NSRange(location: 0, length: ns.length)).reversed() {
            let full = ns.substring(with: m.range)
            let payload = ns.substring(with: m.range(at: 1))
            let head = String(full.prefix(full.count - payload.count + keep))
            let replacement = "\(head)…[\(payload.count.formatted()) base64 characters, sent in full]"
            if let r = Range(m.range, in: out) { out.replaceSubrange(r, with: replacement) }
        }
        return out
    }
}
