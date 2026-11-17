import Foundation
import Testing
@testable import LocalModelCore

@Suite(.serialized)
struct GatewayClientTests {
    // MARK: Routes

    @Test func healthSendsNoKey() async throws {
        StubProtocol.reset { _ in .json(200, #"{"status": "ok", "memory": {"budgetTotalMb": 49152, "reservedMb": 0, "footprintMb": 0, "loadedModels": 0}}"#) }
        let r = try await Fixture.client().health()
        #expect(r.value.status == "ok")
        #expect(r.value.memory.budgetTotalMb == 49152)
        let seen = try #require(StubProtocol.requests.first)
        #expect(seen.url.absoluteString == "http://127.0.0.1:18990/health")
        #expect(seen.headers["Authorization"] == nil)
    }

    @Test func modelsCarryTheKeyClientNameAndRequestID() async throws {
        let key = try Fixture.keyFile("secret-" + String(repeating: "x", count: 40))
        StubProtocol.reset { _ in .json(200, #"{"models": [\#(Fixture.model), \#(Fixture.registered)], "memory": \#(Fixture.totals)}"#, requestID: "req_abc") }
        let r = try await Fixture.client(key).models()
        #expect(r.exchange.requestID == "req_abc")
        #expect(r.value.models.map(\.name) == ["qwen3.5-4b", "qwen3.5-27b"])
        #expect(r.value.models[0].state == .loaded)
        #expect(r.value.models[0].idleSecondsLeft == 557.5)
        #expect(r.value.models[1].footprintMb == nil)
        #expect(r.value.models[1].pull?.fraction == 4e9 / 16.08e9)
        #expect(r.value.memory.evictions == 2)
        let seen = try #require(StubProtocol.requests.first)
        #expect(seen.method == "GET")
        #expect(seen.url.path == "/api/models")
        #expect(seen.headers["Authorization"] == "Bearer secret-" + String(repeating: "x", count: 40))
        #expect(seen.headers["X-Client-Name"] == "modelbar")
    }

    @Test func everyManagementRoute() async throws {
        StubProtocol.reset { req in
            switch (req.httpMethod!, req.url!.path) {
            case ("GET", "/api/models/qwen3.5-4b"): .json(200, Fixture.model)
            case ("GET", "/api/status"): .json(200, #"{"memory": \#(Fixture.totals), "ledgerRows": 12, "refusals": {"unauthenticated": 1, "host": 0, "origin": 2}}"#)
            case ("POST", "/api/models/qwen3.5-4b/load"): .json(200, #"{"name": "qwen3.5-4b", "state": "loaded", "loadSeconds": 2.6}"#)
            case ("POST", "/api/models/qwen3.5-4b/unload"): .json(200, #"{"name": "qwen3.5-4b", "state": "unloading"}"#)
            case ("POST", "/api/models/qwen3.5-4b/pin"): .json(200, #"{"name": "qwen3.5-4b", "pinned": true}"#)
            case ("POST", "/api/models/qwen3.5-27b/pull"): .json(202, #"{"model": "qwen3.5-27b", "phase": "queued", "bytesDone": 0, "bytesTotal": 0, "ok": null, "message": null, "startedAt": 1.0, "finishedAt": null}"#)
            case ("GET", "/api/pulls"): .json(200, #"{"pulls": []}"#)
            case ("GET", "/api/requests"): .json(200, #"{"requests": [{"id": "req_1", "client": null, "model": "jev", "route": "POST /v1/chat/completions", "started_at": 1794900000.25, "queue_ms": 0, "load_ms": 1500, "duration_ms": 3300, "prompt_tokens": 120, "completion_tokens": null, "status": 200, "outcome": "ok", "error_kind": null}]}"#)
            case ("GET", "/api/logs"): .json(200, #"{"source": "gateway", "lines": ["a", "b"]}"#)
            case ("POST", "/api/shutdown"): .json(202, #"{"status": "stopping"}"#)
            case ("GET", "/v1/models"): .json(200, #"{"object": "list", "data": [{"id": "jev", "object": "model", "owned_by": "local"}]}"#)
            default: .json(404, #"{"error": {"message": "no route", "type": "not_found_error", "code": 404}}"#)
            }
        }
        let c = try Fixture.client()
        #expect(try await c.model("qwen3.5-4b").value.budgetMb == 6144)
        let status = try await c.status().value
        #expect(status.ledgerRows == 12 && status.refusals["origin"] == 2)
        #expect(try await c.load("qwen3.5-4b").value.loadSeconds == 2.6)
        #expect(try await c.unload("qwen3.5-4b").value.state == "unloading")
        #expect(try await c.pin("qwen3.5-4b", true).value.pinned)
        let pull = try await c.pull("qwen3.5-27b")
        #expect(pull.exchange.status == 202 && pull.value.phase == "queued" && pull.value.fraction == nil)
        #expect(try await c.pulls().value.pulls.isEmpty)
        let row = try #require(try await c.requests(limit: 5000).value.requests.first)
        #expect(row.completionTokens == nil && row.promptTokens == 120 && row.generationMs == 1800)
        #expect(try await c.logs(source: "gateway", lines: 50).value.lines == ["a", "b"])
        #expect(try await c.shutdown().value.status == "stopping")
        #expect(try await c.openAIModels().value.data.map(\.id) == ["jev"])

        let seen = StubProtocol.requests
        let pin = try #require(seen.first { $0.url.path.hasSuffix("/pin") })
        #expect(String(decoding: pin.body ?? Data(), as: UTF8.self) == #"{"pinned": true}"#)
        let requests = try #require(seen.first { $0.url.path == "/api/requests" })
        #expect(requests.url.query == "limit=1000")  // clamped to what the gateway serves
        let logs = try #require(seen.first { $0.url.path == "/api/logs" })
        #expect(logs.url.query == "source=gateway&lines=50")
        #expect(seen.allSatisfy { $0.url.host == "127.0.0.1" && $0.headers["Authorization"] != nil })
    }

    /// The 17 Nov additive fields, as `describe()` and `status()` now send them.
    @Test func newerGatewayFields() async throws {
        let model = #"""
        {"name": "jev", "displayName": "Jev", "state": "unloading", "phase": "stopping", "work": ["check-reading"],
         "serverKind": "jev-wrapper", "server": {"kind": "jev-wrapper", "maxTokens": null},
         "capabilities": {"imageInput": true, "jsonSchemaOutput": false, "boxes": true, "instructions": "ignored", "output": "scores"},
         "budgetMb": 8192, "footprintMb": 5300, "pinned": true, "pinnedBy": "user", "ttlSeconds": 600, "idleSecondsLeft": null,
         "concurrency": 1, "queueLimit": 8, "readyTimeoutSeconds": 600, "lastUsedSecondsAgo": 3.0, "loadSeconds": 3.4,
         "inFlight": 0, "waiting": 0,
         "weights": {"source": "original", "repo": "openbmb/MiniCPM-OCR-Jev", "revision": "a0ed278eb6aa1f333a711665ab610055a4a816ab",
                     "path": "~/Models/hf/x", "verifiedAt": "2026-11-17T12:00:00+0530", "reused": true, "sha256Pinned": true,
                     "filesVerified": 9, "approxSizeGb": 4.45, "download": "original",
                     "alternative": {"source": "mlx", "repo": "nativ-community/MiniCPM-OCR-Jev-bf16", "revision": "d874fb2b4a5966d6e32430da89e2f504d9527561", "approxSizeGb": 4.45, "commercialUse": "unread"}},
         "commercialUse": "allowed", "licence": {"id": "Apache-2.0", "readFrom": "README", "commercialUse": "allowed"},
         "lastError": {"at": "2026-11-17T12:01:00+0530", "kind": "worker_failed", "message": "exited"},
         "pull": {"model": "jev", "phase": "cancelled", "bytesDone": 1, "bytesTotal": 2, "currentFile": "model.safetensors", "ok": false, "message": null, "startedAt": 1.0, "finishedAt": 2.0}}
        """#
        StubProtocol.reset { req in
            switch req.url!.path {
            case "/api/models/jev": .json(200, model)
            case "/api/status": .json(200, #"{"memory": \#(Fixture.totals), "ledgerRows": 1, "refusals": {}, "version": "0.1.0", "startedAt": "2026-11-17T12:00:00+0530", "uptimeSeconds": 61, "port": 18900, "inFlightTotal": 0, "waitingTotal": 0, "registryPath": "~/src/tinybar/backend/registry.json", "defaults": {"ttlSeconds": 600}, "storeFreeGb": 143.2}"#)
            case "/api/requests": .json(200, #"{"requests": [], "next": null}"#)
            case "/api/logs": .json(200, #"{"source": "gateway", "lines": ["x"], "next": 2048, "rotated": false}"#)
            case "/api/models/jev/pull/cancel": .json(200, #"{"model": "jev", "phase": "queued", "bytesDone": 0, "bytesTotal": 0, "ok": null, "message": null, "startedAt": 1.0, "finishedAt": null}"#)
            default: .json(404, #"{"error": {"message": "no", "type": "x", "code": 404}}"#)
            }
        }
        let c = try Fixture.client()
        let m = try await c.model("jev").value
        #expect(m.phase == "stopping" && m.pinnedBy == "user" && m.licence?.id == "Apache-2.0")
        #expect(m.weights.filesVerified == 9 && m.weights.alternative?.commercialUse == "unread" && m.weights.reused == true)
        #expect(m.lastError?.kind == "worker_failed" && m.pull?.currentFile == "model.safetensors")
        #expect(m.idleSecondsLeft == nil)
        let s = try await c.status().value
        #expect(s.version == "0.1.0" && s.port == 18900 && s.storeFreeGb == 143.2 && s.defaults?["ttlSeconds"] == 600)
        #expect(try await c.requests(limit: 10, model: "jev", outcome: "error", before: 1794900000.25).value.next == nil)
        #expect(try await c.logs(source: "gateway", since: 1024).value.next == 2048)
        #expect(try await c.cancelPull("jev").value.phase == "queued")
        let seen = StubProtocol.requests
        #expect(seen.first { $0.url.path == "/api/requests" }?.url.query == "limit=10&model=jev&outcome=error&before=1794900000.25")
        #expect(seen.first { $0.url.path == "/api/logs" }?.url.query == "source=gateway&lines=200&since=1024")
        #expect(seen.first { $0.url.path.hasSuffix("/pull/cancel") }?.method == "POST")
    }

    /// Availability (17 Nov 2026): the switch, its fields, and a refused request for a model that is off.
    @Test func availability() async throws {
        StubProtocol.reset { req in
            switch (req.httpMethod!, req.url!.path) {
            case ("POST", "/api/models/qwen3.5-27b/available"): .json(200, #"{"name": "qwen3.5-27b", "available": false, "availableBy": "user"}"#)
            case ("GET", "/api/status"): .json(200, #"{"memory": \#(Fixture.totals), "ledgerRows": 1, "refusals": {}, "availableModels": 4, "startedBy": "launchd", "loginItem": "installed"}"#)
            case ("GET", "/api/models"):
                .json(200, #"{"models": [\#(Fixture.model.replacingOccurrences(of: "\"commercialUse\": \"allowed\"}", with: "\"commercialUse\": \"allowed\", \"available\": false, \"availableBy\": \"user\"}")), \#(Fixture.registered)], "memory": \#(Fixture.totals)}"#)
            default: .json(403, #"{"error": {"message": "model 'qwen3.5-27b' is turned off", "type": "permission_error", "code": "model_not_available"}}"#)
            }
        }
        let c = try Fixture.client()
        let r = try await c.setAvailable("qwen3.5-27b", false).value
        #expect(r.available == false && r.availableBy == "user")
        #expect(String(decoding: StubProtocol.requests.first?.body ?? Data(), as: UTF8.self) == #"{"available": false}"#)
        let s = try await c.status().value
        #expect(s.availableModels == 4 && s.startedByLaunchd && s.loginItem == "installed")
        let models = try await c.models().value.models
        #expect(models[0].isOff && models[0].availableBy == "user")
        #expect(models[1].available == nil && !models[1].isOff)  // an older gateway: no switch, counted on
        do {
            _ = try await c.load("qwen3.5-27b")
            Issue.record("expected 403")
        } catch let error as GatewayError {
            #expect(error.isModelNotAvailable && error.status == 403)
        }
        let x = try await c.chat(Data(#"{"model": "qwen3.5-27b", "messages": [{"role": "user", "content": "hi"}]}"#.utf8))
        #expect(x.errorEnvelope?.isModelNotAvailable == true)
        // An ordinary 403 is not mistaken for it.
        let other = try JSONDecoder().decode(ErrorEnvelope.self, from: Data(#"{"error": {"message": "Origin", "type": "permission_error", "code": 403}}"#.utf8))
        #expect(!other.isModelNotAvailable && other.error.code == 403)
    }

    // MARK: Errors

    @Test func unauthenticatedIsAKeyProblem() async throws {
        StubProtocol.reset { _ in .json(401, #"{"error": {"message": "Invalid API key", "type": "authentication_error", "code": 401}}"#, requestID: nil) }
        do {
            _ = try await Fixture.client().models()
            Issue.record("expected 401")
        } catch let error as GatewayError {
            #expect(error.status == 401)
            #expect(error.isKeyProblem)
            #expect(error.summary.contains("Invalid API key"))
        }
    }

    @Test func forbidden() async throws {
        StubProtocol.reset { _ in .json(403, #"{"error": {"message": "requests carrying an Origin header (browsers) are refused", "type": "permission_error", "code": 403}}"#) }
        await #expect(throws: GatewayError.self) { try await Fixture.client().status() }
        do { _ = try await Fixture.client().status() } catch let error as GatewayError {
            guard case let .http(f) = error else { Issue.record("not http"); return }
            #expect(f.status == 403 && f.type == "permission_error")
        }
    }

    @Test func unknownModelIs404WithItsMessage() async throws {
        StubProtocol.reset { _ in .json(404, #"{"error": {"message": "model 'nope' is not in the registry", "type": "not_found_error", "code": 404}}"#, requestID: "req_404") }
        do {
            _ = try await Fixture.client().load("nope")
            Issue.record("expected 404")
        } catch let error as GatewayError {
            guard case let .http(f) = error else { Issue.record("not http"); return }
            #expect(f.status == 404)
            #expect(f.message == "model 'nope' is not in the registry")
            #expect(f.requestID == "req_404")
        }
    }

    @Test func busyCarriesRetryAfter() async throws {
        StubProtocol.reset { _ in .json(503, #"{"error": {"message": "not enough memory budget", "type": "server_error", "code": 503}}"#, headers: ["Retry-After": "30"]) }
        do {
            _ = try await Fixture.client().load("qwen3.5-27b")
            Issue.record("expected 503")
        } catch let error as GatewayError {
            guard case let .http(f) = error else { Issue.record("not http"); return }
            #expect(f.status == 503 && f.retryAfter == 30)
            #expect(error.summary.contains("Retry after 30 s"))
        }
    }

    @Test func malformedBody() async throws {
        StubProtocol.reset { _ in .json(200, "<html>not the gateway</html>") }
        do {
            _ = try await Fixture.client().models()
            Issue.record("expected malformed")
        } catch let error as GatewayError {
            guard case let .malformed(status, id, _) = error else { Issue.record("not malformed: \(error)"); return }
            #expect(status == 200 && id == "req_test")
        }
        StubProtocol.reset { _ in .json(200, #"{"models": [{"name": "x"}], "memory": {}}"#) }
        do { _ = try await Fixture.client().models() } catch let error as GatewayError {
            guard case let .malformed(_, _, detail) = error else { Issue.record("not malformed"); return }
            #expect(detail.hasPrefix("missing"))
        }
    }

    @Test func notRunningTimedOutAndOtherTransportFailures() async throws {
        StubProtocol.reset { _ in .init(error: URLError(.cannotConnectToHost)) }
        await #expect(throws: GatewayError.notRunning) { try await Fixture.client().health() }
        StubProtocol.reset { _ in .init(error: URLError(.timedOut)) }
        await #expect(throws: GatewayError.timedOut) { try await Fixture.client().health() }
        StubProtocol.reset { _ in .init(error: URLError(.networkConnectionLost)) }
        do { _ = try await Fixture.client().health() } catch let error as GatewayError {
            guard case .transport = error else { Issue.record("not transport"); return }
        }
    }

    /// No stub: a real loopback port with nothing listening.
    @Test func serverNotRunningForReal() async throws {
        let port = try FreePort.find()
        let client = GatewayClient(address: GatewayAddress(port: port)!, keyFile: try Fixture.keyFile())
        await #expect(throws: GatewayError.notRunning) { try await client.health() }
        await #expect(throws: GatewayError.notRunning) { try await client.models() }
    }

    @Test func cancellation() async throws {
        StubProtocol.reset { _ in .init(hang: true) }
        let client = try Fixture.client()
        let task = Task { try await client.models() }
        try await Task.sleep(for: .milliseconds(100))
        task.cancel()
        do {
            _ = try await task.value
            Issue.record("expected cancellation")
        } catch let error as GatewayError {
            #expect(error == .cancelled)
        }
    }

    // MARK: The key

    @Test func keyProblemsStopTheCallBeforeAnythingIsSent() async throws {
        StubProtocol.reset { _ in .json(200, "{}") }
        let missing = KeyFile(url: URL(fileURLWithPath: "/nonexistent/\(UUID().uuidString)/api-key"))
        await #expect(throws: GatewayError.keyMissing(path: missing.url.path)) { try await Fixture.client(missing).models() }
        let open = try Fixture.keyFile(mode: 0o644)
        await #expect(throws: GatewayError.keyInsecure(path: open.url.path)) { try await Fixture.client(open).models() }
        let short = try Fixture.keyFile("short")
        await #expect(throws: GatewayError.keyUnreadable(path: short.url.path)) { try await Fixture.client(short).models() }
        #expect(StubProtocol.requests.isEmpty)
        // /health needs no key, so it works without one.
        StubProtocol.reset { _ in .json(200, #"{"status": "ok", "memory": {"budgetTotalMb": 1, "reservedMb": 0, "footprintMb": 0, "loadedModels": 0}}"#) }
        #expect(try await Fixture.client(missing).health().value.status == "ok")
    }

    @Test func keyFileThatIsALinkIsRefused() throws {
        let real = try Fixture.keyFile()
        let link = real.url.deletingLastPathComponent().appendingPathComponent("link")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: real.url)
        #expect(KeyFile(url: link).check() == .keyInsecure(path: link.path))
        #expect(real.check() == nil)
    }

    @Test func standardKeyPathMatchesTheBackend() {
        let home = URL(fileURLWithPath: "/Users/someone", isDirectory: true)
        #expect(KeyFile.standard(env: [:], home: home).url.path == "/Users/someone/Library/Application Support/TinybarLocal/api-key")
        #expect(KeyFile.standard(env: ["TINYBAR_LOCAL_HOME": "/tmp/state"], home: home).url.path == "/tmp/state/api-key")
    }

    // MARK: Loopback only

    @Test func onlyLoopbackAddresses() throws {
        #expect(try GatewayAddress(parsing: "http://127.0.0.1:18900").port == 18900)
        #expect(try GatewayAddress(parsing: " http://127.0.0.1:18990/ ").port == 18990)
        for bad in ["http://localhost:18900", "http://[::1]:18900", "https://127.0.0.1:18900", "http://10.0.0.2:18900",
                    "http://127.0.0.1", "http://127.0.0.1:80", "http://127.0.0.1:18900/v1", "http://u:p@127.0.0.1:18900",
                    "http://127.0.0.1.example.com:18900", "127.0.0.1:18900", ""] {
            #expect(throws: GatewayError.self, "\(bad)") { try GatewayAddress(parsing: bad) }
        }
        #expect(!GatewayAddress.isLoopback(URL(string: "http://example.com/health")))
    }

    @Test func aRedirectIsNotFollowed() async throws {
        StubProtocol.reset { _ in .init(status: 302, headers: ["Location": "http://example.com/steal"], chunks: []) }
        do {
            _ = try await Fixture.client().models()
            Issue.record("expected an error")
        } catch let error as GatewayError {
            #expect(error.status == 302)
        }
        #expect(StubProtocol.requests.allSatisfy { $0.url.host == "127.0.0.1" })
    }

    // MARK: Chat

    @Test func chatSendsExactlyTheBytesAndReturnsAnyStatus() async throws {
        let body = Data(#"{"model": "qwen3.5-4b",   "messages": [{"role": "user", "content": "Say hello."}]}"#.utf8)
        StubProtocol.reset { _ in .json(400, #"{"error": {"message": "unsupported request field(s): tools", "type": "invalid_request_error", "code": 400}}"#, requestID: "req_400") }
        let x = try await Fixture.client().chat(body)
        #expect(x.status == 400 && x.requestID == "req_400")
        #expect(x.errorEnvelope?.error.message.contains("tools") == true)
        #expect(StubProtocol.requests.first?.body == body)
        #expect(StubProtocol.requests.first?.url.path == "/v1/chat/completions")
    }

    @Test func streamYieldsEventsInOrder() async throws {
        let events = [
            #"data: {"id": "c1", "choices": [{"delta": {"role": "assistant", "content": "Hel"}}]}"#,
            #"data: {"id": "c1", "choices": [{"delta": {"content": "lo."}, "finish_reason": "stop"}]}"#,
            #"data: {"id": "c1", "choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12}}"#,
            "data: [DONE]",
        ]
        StubProtocol.reset { _ in
            .init(status: 200, headers: ["Content-Type": "text/event-stream", "X-Request-Id": "req_stream"],
                  chunks: events.map { Data(($0 + "\n\n").utf8) })
        }
        var text = "", usage: TokenUsage?, opened = false, done = false
        for try await e in try Fixture.client().chatStream(Data(#"{"stream": true}"#.utf8)) {
            switch e {
            case let .opened(status, id, _, _): opened = status == 200 && id == "req_stream"
            case let .event(_, chunk):
                text += chunk?.choices?.first?.delta?.content ?? ""
                usage = chunk?.usage ?? usage
            case .done: done = true
            case .whole: Issue.record("not a stream")
            }
        }
        #expect(opened && done)
        #expect(text == "Hello.")
        #expect(usage == TokenUsage(promptTokens: 9, completionTokens: 3, totalTokens: 12))
        #expect(StubProtocol.requests.first?.headers["Accept"] == "text/event-stream")
    }

    /// Cancelling the consumer while the gateway hasn't answered (a model loading) ends the stream at
    /// once, with nothing yielded, and the task sees it was cancelled.
    @Test func cancellingAStreamWhileItLoadsEndsIt() async throws {
        StubProtocol.reset { _ in .init(hang: true) }
        let client = try Fixture.client()
        let consumer = Task { () -> (Int, Bool) in
            var events = 0
            do { for try await _ in client.chatStream(Data(#"{"stream": true}"#.utf8)) { events += 1 } } catch {}
            return (events, Task.isCancelled)
        }
        try await Task.sleep(for: .milliseconds(150))
        consumer.cancel()
        let (events, cancelled) = await consumer.value
        #expect(events == 0 && cancelled)
    }

    @Test func aStreamWithoutDoneIsInterrupted() async throws {
        StubProtocol.reset { _ in
            .init(status: 200, headers: ["Content-Type": "text/event-stream"],
                  chunks: [Data((#"data: {"choices": [{"delta": {"content": "Hal"}}]}"# + "\n\n").utf8)])
        }
        var text = ""
        do {
            for try await e in try Fixture.client().chatStream(Data(#"{"stream": true}"#.utf8)) {
                if case let .event(_, chunk) = e { text += chunk?.choices?.first?.delta?.content ?? "" }
            }
            Issue.record("expected interrupted")
        } catch let error as GatewayError {
            #expect(error == .interrupted)
        }
        #expect(text == "Hal")  // what arrived is kept
    }

    @Test func streamRefusedComesBackWhole() async throws {
        StubProtocol.reset { _ in .json(503, #"{"error": {"message": "queue full", "type": "server_error", "code": 503}}"#, headers: ["Retry-After": "2"]) }
        var got: Exchange?
        for try await e in try Fixture.client().chatStream(Data("{}".utf8)) {
            if case let .whole(x) = e { got = x }
        }
        #expect(got?.status == 503 && got?.retryAfter == 2)
    }

    @Test func streamWithoutAKeyFails() async throws {
        let missing = KeyFile(url: URL(fileURLWithPath: "/nonexistent/api-key"))
        await #expect(throws: GatewayError.self) {
            for try await _ in try Fixture.client(missing).chatStream(Data("{}".utf8)) {}
        }
    }
}

enum FreePort {
    /// A loopback port nothing listens on: bound, read, then closed.
    static func find() throws -> Int {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        addr.sin_port = 0
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = withUnsafePointer(to: &addr) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, len) } }
        _ = withUnsafeMutablePointer(to: &addr) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(fd, $0, &len) } }
        return Int(UInt16(bigEndian: addr.sin_port))
    }
}
