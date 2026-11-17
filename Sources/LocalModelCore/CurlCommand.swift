import Foundation

/// A request as a curl command to paste into a terminal. The key never appears: the command reads
/// it from the key file when it runs.
public enum CurlCommand {
    public static func make(address: GatewayAddress, path: String = "/v1/chat/completions", body: Data,
                            keyFile: KeyFile, clientName: String = "curl", stream: Bool = false) -> String {
        let keyPath = shellQuote(keyFile.url.path)
        var lines = [
            "curl -sS\(stream ? "N" : "") \(address)\(path)",
            "  -H \"Authorization: Bearer $(cat \(keyPath))\"",
            "  -H 'Content-Type: application/json'",
            "  -H \(shellQuote("X-Client-Name: \(clientName)"))",
        ]
        let text = String(decoding: body, as: UTF8.self)
        // A here-document keeps the body byte for byte, quotes and all; a unique end marker can't
        // appear inside it.
        var marker = "JSON"
        while text.components(separatedBy: .newlines).contains(marker) { marker += "_END" }
        lines.append("  --data-binary @- <<'\(marker)'")
        return lines.joined(separator: " \\\n") + "\n" + text + (text.hasSuffix("\n") ? "" : "\n") + marker + "\n"
    }

    /// Single quotes, with any single quote inside closed, escaped and reopened.
    public static func shellQuote(_ s: String) -> String {
        "'" + s.replacingOccurrences(of: "'", with: #"'\''"#) + "'"
    }
}
