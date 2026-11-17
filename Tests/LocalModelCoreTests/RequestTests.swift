import Foundation
import Testing
@testable import LocalModelCore

private let png = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0, 0, 0, 13])
private let pngURL = try! ImageDataURL.make(png)

@Test func chatBodyWithSchemaAndStream() throws {
    let data = try ChatRequest.chat(model: "qwen3.5-4b", system: "Be brief.", user: "List two colours.", imageDataURL: pngURL,
                                    maxTokens: 64, temperature: 0.2, jsonSchema: #"{"type": "object"}"#, stream: true)
    let v = try JSONValue.parse(data)
    #expect(v["model"] == .string("qwen3.5-4b"))
    #expect(v["max_tokens"] == .number(64))
    #expect(String(decoding: data, as: UTF8.self).contains(#""max_tokens" : 64"#))  // an integer, not 64.0
    #expect(v["stream"] == .bool(true))
    #expect(v["stream_options"]?["include_usage"] == .bool(true))
    #expect(v["response_format"]?["type"] == .string("json_schema"))
    #expect(v["response_format"]?["json_schema"]?["schema"] == .object(["type": .string("object")]))
    let messages = try #require(v["messages"]?.arrayValue)
    #expect(messages.count == 2)
    #expect(messages[1]["content"]?.arrayValue?.first?["image_url"]?["url"] == .string(pngURL))
    #expect(ChatRequest.wantsStream(data))
}

@Test func chatBodyRefusals() {
    #expect(throws: ChatRequest.BuildError.noModel) {
        try ChatRequest.chat(model: "", system: "", user: "x", imageDataURL: nil, maxTokens: nil, temperature: nil, jsonSchema: nil, stream: false)
    }
    #expect(throws: ChatRequest.BuildError.emptyMessage) {
        try ChatRequest.chat(model: "m", system: "s", user: "  ", imageDataURL: nil, maxTokens: nil, temperature: nil, jsonSchema: nil, stream: false)
    }
    #expect(throws: ChatRequest.BuildError.badSchema("it isn't JSON")) {
        try ChatRequest.chat(model: "m", system: "", user: "x", imageDataURL: nil, maxTokens: nil, temperature: nil, jsonSchema: "{", stream: false)
    }
}

@Test func nemotronTakesOneImageAndNoText() throws {
    #expect(throws: ChatRequest.BuildError.needsImage) { try ChatRequest.nemotron(model: "nemotron-parse", imageDataURL: nil, maxTokens: nil) }
    let v = try JSONValue.parse(ChatRequest.nemotron(model: "nemotron-parse", imageDataURL: pngURL, maxTokens: 8192))
    let parts = try #require(v["messages"]?.arrayValue?.first?["content"]?.arrayValue)
    #expect(parts.count == 1 && parts[0]["type"] == .string("image_url"))
}

@Test func jevBlocksAreCheckedAsTheWorkerChecksThem() throws {
    let good = #"{"blocks": [{"block": 1, "text": "Total: 120.00", "box": [0.05, 0.55, 0.21, 0.57]}, {"block": 2, "text": "Thanks", "box": null}]}"#
    let v = try JSONValue.parse(ChatRequest.jev(model: "jev", imageDataURL: pngURL, blocks: good))
    let parts = try #require(v["messages"]?.arrayValue?.first?["content"]?.arrayValue)
    #expect(parts.map { $0["type"] } == [.string("image_url"), .string("text")])
    let inner = try JSONValue.parse(try #require(parts[1]["text"]?.stringValue))
    #expect(inner["blocks"]?.arrayValue?.count == 2)
    for bad in [#"{"blocks": []}"#, #"{"blocks": [{"block": 1}]}"#, #"{"blocks": [{"block": 1, "text": "a"}, {"block": 1, "text": "b"}]}"#,
                #"{"blocks": [{"block": 1.5, "text": "a"}]}"#, #"{"blocks": [{"block": 1, "text": "a", "box": [0.5, 0.1, 0.2, 0.3]}]}"#,
                #"{"blocks": [{"block": 1, "text": "a", "box": [0, 0, 1]}]}"#, "[1]", "nope"] {
        #expect(throws: ChatRequest.BuildError.self, "\(bad)") { try ChatRequest.jev(model: "jev", imageDataURL: pngURL, blocks: bad) }
    }
    #expect(throws: ChatRequest.BuildError.needsImage) { try ChatRequest.jev(model: "jev", imageDataURL: nil, blocks: good) }
}

@Test func rawSendsExactlyWhatIsTyped() throws {
    let typed = "{ \"model\":\"qwen3.5-4b\" ,\n  \"messages\": [] }"
    #expect(try ChatRequest.raw(typed) == Data(typed.utf8))
    #expect(throws: ChatRequest.BuildError.self) { try ChatRequest.raw("{\"model\": ") }
}

@Test func imagesByTheirBytes() throws {
    #expect(ImageDataURL.mediaType(png) == "image/png")
    #expect(ImageDataURL.mediaType(Data([0xFF, 0xD8, 0xFF, 0xE0])) == "image/jpeg")
    #expect(ImageDataURL.mediaType(Data("RIFF\u{0}\u{0}\u{0}\u{0}WEBP".utf8)) == "image/webp")
    #expect(ImageDataURL.mediaType(Data("GIF89a".utf8)) == nil)
    #expect(throws: ChatRequest.BuildError.self) { try ImageDataURL.make(Data("%PDF-1.7".utf8)) }
    #expect(pngURL.hasPrefix("data:image/png;base64,"))
}

@Test func longImagesAreElidedOnlyForDisplay() {
    let url = "data:image/png;base64," + String(repeating: "A", count: 5000)
    let shown = BodyDisplay.elidingImages(#"{"url": "\#(url)"}"#)
    #expect(shown.count < 200)
    #expect(shown.contains("5,000 base64 characters, sent in full"))
    #expect(BodyDisplay.elidingImages("short") == "short")
}

@Test func curlNeverHoldsTheKey() throws {
    let key = try Fixture.keyFile("never-shown-" + String(repeating: "z", count: 40))
    let body = Data(#"{"model": "jev", "note": "it's"}"#.utf8)
    let curl = CurlCommand.make(address: GatewayAddress(port: 18900)!, body: body, keyFile: key)
    #expect(!curl.contains("never-shown"))
    #expect(curl.contains("Authorization: Bearer $(cat '\(key.url.path)')"))
    #expect(curl.hasPrefix("curl -sS http://127.0.0.1:18900/v1/chat/completions"))
    #expect(curl.contains(#""note": "it's""#))  // the body as typed, inside a quoted here-document
    // Run it through the shell's parser to be sure it is one valid command (nothing is executed).
    let check = Process()
    check.executableURL = URL(fileURLWithPath: "/bin/bash")
    check.arguments = ["-n", "-c", curl]
    try check.run()
    check.waitUntilExit()
    #expect(check.terminationStatus == 0)
}

@Test func registryFileReadsTheBackendRegistry() throws {
    let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .appendingPathComponent("backend/registry.json")
    guard FileManager.default.fileExists(atPath: url.path) else { return }
    let r = try RegistryFile.load(url)
    #expect(r.memoryBudgetTotalMb > 0)
    #expect(r.defaults.ttlSeconds != nil)
    let jev = try #require(r.entry("jev"))
    #expect(jev.server.kind == "jev-wrapper")
    #expect(jev.licence["original"]?.commercialUse == "allowed")
}

@Test func ledgerRowsKeepUnknownsUnknown() throws {
    let row = try JSONDecoder().decode(LedgerRow.self, from: Data(#"{"id": "req_1", "client": "x", "model": null, "route": "other", "started_at": 5, "queue_ms": null, "load_ms": null, "duration_ms": null, "prompt_tokens": null, "completion_tokens": null, "status": 499, "outcome": "cancelled", "error_kind": "client_disconnected"}"#.utf8))
    #expect(row.promptTokens == nil && row.completionTokens == nil && row.generationMs == nil)
    #expect(row.started == Date(timeIntervalSince1970: 5))
}

@Test func unknownStatesDoNotFailTheList() throws {
    let m = try JSONDecoder().decode([ModelState].self, from: Data(#"["loaded", "hibernating"]"#.utf8))
    #expect(m == [.loaded, .unknown])
}

@Test func conflictShowsTheGatewaysMessage() {
    let e = GatewayError.http(.init(status: 409, message: "model 'x' is registered but not downloaded", type: nil, requestID: nil, retryAfter: nil, body: Data()))
    #expect(e.summary == "Conflict (409): model 'x' is registered but not downloaded")
}

@Test func routeNamesAreShort() {
    #expect(LedgerRow.routeName("POST /v1/chat/completions") == "chat")
    #expect(LedgerRow.routeName("POST /api/models/{name}/load") == "load")
    #expect(LedgerRow.routeName("POST /api/models/{name}/pull/cancel") == "pull cancel")
    #expect(LedgerRow.routeName("GET /api/status") == "status")
    #expect(LedgerRow.routeName("other") == "other")
}

@Test func backendFolderIsCheckedBeforeAnythingRuns() throws {
    let fm = FileManager.default
    let root = fm.temporaryDirectory.appendingPathComponent("backend-\(UUID().uuidString)", isDirectory: true)
    defer { try? fm.removeItem(at: root) }
    let bin = root.appendingPathComponent(".venv/bin", isDirectory: true)
    try fm.createDirectory(at: bin, withIntermediateDirectories: true)
    let cli = bin.appendingPathComponent("tinybar-models")
    #expect(throws: BackendFolder.Problem.self) { try BackendFolder.cli(in: root) }  // no pyproject.toml
    try Data().write(to: root.appendingPathComponent("pyproject.toml"))
    #expect(throws: BackendFolder.Problem.self) { try BackendFolder.cli(in: root) }  // not set up
    try Data("#!/bin/sh\n".utf8).write(to: cli)
    try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: cli.path)
    try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: bin.path)
    #expect(try BackendFolder.cli(in: root).lastPathComponent == "tinybar-models")
    try fm.setAttributes([.posixPermissions: 0o777], ofItemAtPath: cli.path)
    #expect(throws: BackendFolder.Problem.self) { try BackendFolder.cli(in: root) }  // world-writable
    try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: cli.path)
    #expect(throws: BackendFolder.Problem.self) { try BackendFolder.cli(in: root, uid: getuid() + 1) }  // not this user's
    // A link leading out of the folder is refused.
    let outside = fm.temporaryDirectory.appendingPathComponent("outside-\(UUID().uuidString)")
    try Data("#!/bin/sh\n".utf8).write(to: outside)
    defer { try? fm.removeItem(at: outside) }
    try fm.removeItem(at: cli)
    try fm.createSymbolicLink(at: cli, withDestinationURL: outside)
    #expect(throws: BackendFolder.Problem.outside(cli.path)) { try BackendFolder.cli(in: root) }
}

@Test func notAvailableRowsAreRecognised() throws {
    func row(_ outcome: String, _ kind: String?) throws -> LedgerRow {
        try JSONDecoder().decode(LedgerRow.self, from: Data(#"{"id": "r", "route": "POST /v1/chat/completions", "started_at": 1, "status": 403, "outcome": "\#(outcome)", "error_kind": \#(kind.map { "\"\($0)\"" } ?? "null")}"#.utf8))
    }
    #expect(try row("not_available", nil).isNotAvailable)
    #expect(try row("refused", "model_not_available").isNotAvailable)
    #expect(try !row("refused", "licence_not_allowed").isNotAvailable)
}

@Test func ownershipIsBoundToAddressAndStartTime() throws {
    let a = GatewayAddress(port: 18990)!, b = GatewayAddress(port: 18991)!
    let launched = try #require(GatewayOwnership.parseDate("2026-11-17T13:59:40+0530"))
    let record = LaunchRecord(address: a, pid: 4242, startedAt: launched)
    func status(_ started: String?, port: Int? = 18990, by: String? = "manual") -> GatewayStatus {
        GatewayStatus(memory: MemoryTotals(budgetTotalMb: 1, reservedMb: 0, footprintMb: 0, loadedModels: 0), ledgerRows: 0, refusals: [:],
                      startedAt: started, port: port, startedBy: by)
    }
    #expect(GatewayOwnership.owns(record, childRunning: true, address: a, status: status("2026-11-17T13:59:41+0530")))
    // Another address, a child that exited, launchd, another port, another start, no start time: not owned.
    #expect(!GatewayOwnership.owns(record, childRunning: true, address: b, status: status("2026-11-17T13:59:41+0530", port: 18991)))
    #expect(!GatewayOwnership.owns(record, childRunning: false, address: a, status: status("2026-11-17T13:59:41+0530")))
    #expect(!GatewayOwnership.owns(record, childRunning: true, address: a, status: status("2026-11-17T13:59:41+0530", by: "launchd")))
    #expect(!GatewayOwnership.owns(record, childRunning: true, address: a, status: status("2026-11-17T13:59:41+0530", port: 18900)))
    #expect(!GatewayOwnership.owns(record, childRunning: true, address: a, status: status("2026-11-17T15:00:00+0530")))
    #expect(!GatewayOwnership.owns(record, childRunning: true, address: a, status: status(nil)))
    #expect(!GatewayOwnership.owns(nil, childRunning: true, address: a, status: status("2026-11-17T13:59:41+0530")))
}

@Test func newLedgerOutcomes() throws {
    let row = try JSONDecoder().decode(LedgerRow.self, from: Data(#"{"id": "r", "route": "POST /v1/chat/completions", "started_at": 1, "status": null, "outcome": "in_flight", "phase": "generating", "elapsed_ms": 1200}"#.utf8))
    #expect(row.isInFlight && row.phase == "generating" && row.elapsedMs == 1200 && row.outcomeLabel == "running: generating")
    let pull = try JSONDecoder().decode(PullProgress.self, from: Data(#"{"model": "m", "phase": "cancelling", "bytesDone": 1, "bytesTotal": 2, "startedAt": 1, "finishedAt": null}"#.utf8))
    #expect(pull.isCancelling && !pull.isFinished)
}
