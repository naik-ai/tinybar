import AppKit
import LocalModelCore
import Observation
import SwiftUI
import UniformTypeIdentifiers

/// Try's draft and its last exchange. Held only while the window is open; nothing typed or
/// returned here is ever saved.
@MainActor
@Observable
final class TryModel {
    enum Mode: String, CaseIterable { case form, raw }

    struct Picked: Equatable {
        var name: String
        var bytes: Int
        var dataURL: String
        var thumbnail: NSImage?
    }

    struct Result {
        var sent: Data
        var streamed: Bool
        var status: Int?
        var requestID: String?
        var headers: [String: String] = [:]
        var body = Data()
        /// A streamed answer: its text so far and its events as they came.
        var streamText = ""
        var events: [String] = []
        var secondsToHeaders: Double?
        var secondsTotal: Double?
        var usage: TokenUsage?
        var ledger: LedgerRow?
        var error: String?
        var finished = false
        var cancelled = false
        /// The stream ended without its `[DONE]`: what arrived is incomplete.
        var interrupted = false
        /// A streamed answer's finish reason, from its last chunk that gave one.
        var finishReason: String?

        var retryAfter: Int? { headers["retry-after"].flatMap { Int($0) } }
        var errorMessage: String? { (try? JSONDecoder().decode(ErrorEnvelope.self, from: body))?.error.message }
        var completion: ChatCompletion? { try? JSONDecoder().decode(ChatCompletion.self, from: body) }
    }

    var modelName = ""
    var mode = Mode.form
    var system = ""
    var user = "Describe this page in two sentences."
    var maxTokens = "256"
    var temperature = ""
    var useSchema = false
    var schema = """
    {
      "type": "object",
      "properties": {
        "title": {"type": "string"},
        "total": {"type": "number"}
      },
      "required": ["title", "total"]
    }
    """
    var stream = true
    var image: Picked?
    var blocks = """
    {"blocks": [
      {"block": 1, "text": "Invented Page", "box": [0.08, 0.05, 0.6, 0.09]},
      {"block": 2, "text": "Item one 10.00", "box": [0.08, 0.2, 0.7, 0.23]},
      {"block": 3, "text": "Total 10.00", "box": [0.08, 0.55, 0.4, 0.58]}
    ]}
    """
    var raw = """
    {
      "model": "qwen3.5-4b",
      "max_tokens": 64,
      "messages": [{"role": "user", "content": "Say hello in five words."}]
    }
    """
    var buildError: String?
    private(set) var result: Result?
    private(set) var task: Task<Void, Never>?
    var isRunning: Bool { task != nil }

    /// The request body for what is on screen: encoded once, and these bytes are what is shown and sent.
    func build(kind: String?) throws(ChatRequest.BuildError) -> Data {
        if mode == .raw { return try ChatRequest.raw(raw) }
        let max = Int(maxTokens.trimmingCharacters(in: .whitespaces))
        switch kind {
        case "nemotron-wrapper":
            return try ChatRequest.nemotron(model: modelName, imageDataURL: image?.dataURL, maxTokens: max)
        case "jev-wrapper":
            return try ChatRequest.jev(model: modelName, imageDataURL: image?.dataURL, blocks: blocks)
        default:
            return try ChatRequest.chat(model: modelName, system: system, user: user, imageDataURL: image?.dataURL,
                                        maxTokens: max, temperature: Double(temperature.trimmingCharacters(in: .whitespaces)),
                                        jsonSchema: useSchema ? schema : nil, stream: stream)
        }
    }

    func pickImage() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.png, .jpeg, .webP]
        panel.message = "Pick a page image to send. It goes inline as a data URL; it is not kept."
        guard panel.runModal() == .OK, let url = panel.url else { return }
        setImage(url)
    }

    func setImage(_ url: URL) {
        do {
            // Its size first: a file too large to send is never read into memory.
            let size = (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
            guard size <= ChatRequest.maxBodyBytes * 3 / 4 else { throw ChatRequest.BuildError.tooLarge(size * 4 / 3) }
            let data = try Data(contentsOf: url)
            image = Picked(name: url.lastPathComponent, bytes: data.count, dataURL: try ImageDataURL.make(data), thumbnail: NSImage(data: data))
            buildError = nil
        } catch let e as ChatRequest.BuildError {
            buildError = e.summary
        } catch {
            buildError = "Couldn't read \(url.lastPathComponent): \(error.localizedDescription)"
        }
    }

    func setImage(data: Data, name: String) {
        image = (try? ImageDataURL.make(data)).map { Picked(name: name, bytes: data.count, dataURL: $0, thumbnail: NSImage(data: data)) }
    }

    func send(_ store: LocalModelsStore, kind: String?) {
        guard task == nil else { return }
        let body: Data
        do {
            body = try build(kind: kind)
            buildError = nil
        } catch {
            buildError = error.summary
            return
        }
        let streamed = ChatRequest.wantsStream(body)
        result = Result(sent: body, streamed: streamed)
        let client = store.currentClient
        task = Task {
            let started = Date()
            if streamed {
                do {
                    for try await e in client.chatStream(body) {
                        switch e {
                        case let .opened(status, id, headers, toHeaders):
                            result?.status = status
                            result?.requestID = id
                            result?.headers = headers
                            result?.secondsToHeaders = toHeaders
                        case let .event(raw, chunk):
                            result?.events.append(raw)
                            result?.streamText += chunk?.choices?.first?.delta?.content ?? ""
                            if let u = chunk?.usage { result?.usage = u }
                            if let reason = chunk?.choices?.first?.finishReason { result?.finishReason = reason }
                        case .done:
                            result?.events.append("[DONE]")
                        case let .whole(x):
                            apply(x)
                        }
                    }
                } catch let e as GatewayError {
                    switch e {
                    case .cancelled: result?.cancelled = true
                    case .interrupted: result?.interrupted = true
                    default: result?.error = e.summary
                    }
                } catch {
                    result?.error = String(describing: error)
                }
                // A cancelled consumer ends the stream without an error: say so, whenever it came.
                if Task.isCancelled { result?.cancelled = true }
                result?.secondsTotal = Date().timeIntervalSince(started)
            } else {
                do {
                    apply(try await client.chat(body))
                } catch {
                    let e = error as? GatewayError ?? .transport(String(describing: error))
                    if e == .cancelled { result?.cancelled = true } else { result?.error = e.summary }
                    result?.secondsTotal = Date().timeIntervalSince(started)
                }
            }
            result?.finished = true
            task = nil
            // Timings from the ledger row with this answer's own X-Request-Id, attached only while
            // this answer is still the one shown: a later Send never gets an earlier row.
            if let id = result?.requestID, !(result?.cancelled ?? false) {
                let row = await store.ledgerRow(id)
                if let row, row.id == id, result?.requestID == id { result?.ledger = row }
            }
        }
    }

    private func apply(_ x: Exchange) {
        result?.status = x.status
        result?.requestID = x.requestID
        result?.headers = x.headers
        result?.body = x.body
        result?.secondsToHeaders = x.secondsToHeaders
        result?.secondsTotal = x.secondsTotal
        result?.usage = (try? JSONDecoder().decode(ChatCompletion.self, from: x.body))?.usage
    }

    /// Cancelling a request the model is already working on stops its worker; the next request
    /// loads it again (the gateway's rule).
    func cancel() {
        task?.cancel()
    }

    func clear() {
        guard task == nil else { return }
        result = nil
    }
}

struct TrySection: View {
    let ws: LocalModelsWorkspaceState
    @Bindable var tryout: TryModel
    let palette: StatsPalette

    var body: some View {
        let store = ws.store
        Group {
            if store.models.isEmpty && store.state != .running {
                GatewayRecovery(ws: ws, palette: palette)
            } else {
                // The form grows with the window up to a readable width; the answer takes the rest.
                GeometryReader { g in
                    HStack(alignment: .top, spacing: 20) {
                        form(store).frame(width: min(max(318, g.size.width * 0.38), 560))
                        ResultView(tryout: tryout, ws: ws, palette: palette)
                            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                    }
                }
                .onAppear {
                    if tryout.modelName.isEmpty || store.model(tryout.modelName) == nil {
                        tryout.modelName = store.models.first { $0.state == .loaded && $0.serverKind == "mlx-vlm" }?.name
                            ?? store.models.first { $0.isDownloaded }?.name ?? store.models.first?.name ?? ""
                    }
                }
            }
        }
        // A request keeps running when another section shows (its Cancel is in every section's
        // header); the window closing cancels it.
    }

    private func kind(_ store: LocalModelsStore) -> String? { store.model(tryout.modelName)?.serverKind }

    @ViewBuilder
    private func form(_ store: LocalModelsStore) -> some View {
        let kind = kind(store)
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Picker("Model", selection: $tryout.modelName) {
                    ForEach(store.models) { m in
                        Text("\(m.name)\(m.isOff ? " (off)" : m.isDownloaded ? "" : " (not downloaded)")").tag(m.name)
                            .selectionDisabled(m.isOff)
                    }
                }
                .labelsHidden()
                Picker("Mode", selection: $tryout.mode) {
                    Text("Form").tag(TryModel.Mode.form)
                    Text("Raw JSON").tag(TryModel.Mode.raw)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 130)
            }
            if let m = store.model(tryout.modelName) {
                Text(hint(m)).font(.system(size: 10.5)).foregroundStyle(palette.ink2).fixedSize(horizontal: false, vertical: true)
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    if tryout.mode == .raw {
                        label("Request body, sent exactly as typed")
                        editor($tryout.raw, height: 300)
                    } else if kind == "nemotron-wrapper" {
                        imagePicker(required: true)
                        label("Max tokens")
                        TextField("8192", text: $tryout.maxTokens).textFieldStyle(.roundedBorder).frame(width: 100)
                        note("Nemotron reads one page image with its fixed prompt; it follows no instructions, so no text is sent.")
                    } else if kind == "jev-wrapper" {
                        imagePicker(required: true)
                        label("Blocks: {\"blocks\": [{block, text, box}]}, boxes as page fractions")
                        editor($tryout.blocks, height: 150)
                        note("Jev scores each boxed block against its crop of the page. These blocks are invented; a reader such as nemotron-parse gives real ones.")
                    } else {
                        label("System")
                        editor($tryout.system, height: 44)
                        label("User")
                        editor($tryout.user, height: 70)
                        imagePicker(required: false)
                        HStack(spacing: 10) {
                            VStack(alignment: .leading, spacing: 3) {
                                label("Max tokens")
                                TextField("default", text: $tryout.maxTokens).textFieldStyle(.roundedBorder).frame(width: 90)
                            }
                            VStack(alignment: .leading, spacing: 3) {
                                label("Temperature")
                                TextField("default", text: $tryout.temperature).textFieldStyle(.roundedBorder).frame(width: 90)
                            }
                            Toggle("Stream", isOn: $tryout.stream).padding(.top, 14)
                        }
                        Toggle("Answer in JSON matching this schema", isOn: $tryout.useSchema)
                        if tryout.useSchema { editor($tryout.schema, height: 110) }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            if let error = tryout.buildError {
                Text(error).font(.system(size: 11)).foregroundStyle(Color(nsColor: .systemRed)).fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                if tryout.isRunning {
                    Button(role: .cancel) { tryout.cancel() } label: { Label("Cancel", systemImage: "xmark") }
                        .keyboardShortcut(.cancelAction)
                        .help("Stops the request. If the model already has it, the gateway stops that worker, and the next request reloads it.")
                    ProgressView().controlSize(.small)
                } else {
                    Button { tryout.send(store, kind: kind) } label: { Label("Send", systemImage: "paperplane.fill") }
                        .keyboardShortcut(.return, modifiers: .command)
                        .disabled(store.state != .running || tryout.modelName.isEmpty && tryout.mode == .form
                                  || tryout.mode == .form && store.model(tryout.modelName)?.isOff == true)
                        .help("Send (⌘↩)")
                }
                Spacer()
                Button { copyCurl(kind) } label: { Label("Copy as curl", systemImage: "terminal") }
                    .help("A curl command that reads the key from its file when it runs; the key itself is never copied")
            }
        }
        .controlSize(.small)
    }

    private func hint(_ m: ModelInfo) -> String {
        if m.isOff { return "Off: not served and takes no memory. Turn it on in Installed (or below) to try it." }
        let state = m.state == .loaded ? "Loaded." : m.isDownloaded ? "Not loaded: the first request loads it." : "Not downloaded: requests get 409 until it is pulled."
        switch m.serverKind {
        case "nemotron-wrapper": return "\(state) Page reader: one image in, {text, blocks} out."
        case "jev-wrapper": return "\(state) Reading scorer: one page image and its blocks in, {scores} out."
        default: return "\(state) Chat with text and images; JSON schema output\(m.work.contains("chat") ? "" : " where supported")."
        }
    }

    private func copyCurl(_ kind: String?) {
        do {
            let body = try tryout.build(kind: kind)
            let curl = CurlCommand.make(address: ws.settings.address, body: body, keyFile: ws.settings.keyFile,
                                        clientName: "curl", stream: ChatRequest.wantsStream(body))
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(curl, forType: .string)
            tryout.buildError = nil
        } catch {
            tryout.buildError = error.summary
        }
    }

    private func imagePicker(required: Bool) -> some View {
        HStack(spacing: 8) {
            if let img = tryout.image {
                if let t = img.thumbnail {
                    Image(nsImage: t).resizable().aspectRatio(contentMode: .fit).frame(width: 34, height: 44)
                        .background(palette.faint)
                }
                VStack(alignment: .leading, spacing: 1) {
                    Text(img.name).font(.system(size: 11.5, weight: .medium)).lineLimit(1)
                    Text(LMFormat.bytes(Int64(img.bytes))).font(.system(size: 10.5)).foregroundStyle(palette.ink2)
                }
                Spacer()
                Button("Remove") { tryout.image = nil }
            } else {
                Text(required ? "A page image is required." : "No image.").font(.system(size: 11.5)).foregroundStyle(palette.ink2)
                Spacer()
            }
            Button(tryout.image == nil ? "Pick image…" : "Change…") { tryout.pickImage() }
        }
        .padding(8)
        .background(RoundedRectangle(cornerRadius: 7, style: .continuous).fill(palette.faint))
    }

    private func editor(_ text: Binding<String>, height: CGFloat) -> some View {
        TextEditor(text: text)
            .font(.system(size: 11.5, design: .monospaced))
            .scrollContentBackground(.hidden)
            .padding(5)
            .frame(height: height)
            .background(RoundedRectangle(cornerRadius: 7, style: .continuous).fill(palette.faint))
            .autocorrectionDisabled()
    }

    private func label(_ text: String) -> some View {
        Text(text).font(.system(size: 11, weight: .semibold)).foregroundStyle(palette.ink2)
    }

    private func note(_ text: String) -> some View {
        Text(text).font(.system(size: 10.5)).foregroundStyle(palette.ink3).fixedSize(horizontal: false, vertical: true)
    }
}

private struct ResultView: View {
    let tryout: TryModel
    let ws: LocalModelsWorkspaceState
    let palette: StatsPalette
    @State private var tab = "answer"

    var body: some View {
        if let r = tryout.result {
            VStack(alignment: .leading, spacing: 10) {
                summary(r)
                if let name = notAvailableModel(r) {
                    HStack(spacing: 8) {
                        Image(systemName: "power").foregroundStyle(Color(nsColor: .systemOrange))
                        Text("\(name) is turned off, so the gateway didn't serve it.").font(.system(size: 12))
                        Button("Turn on") { Task { await ws.store.setAvailable(name, true) } }
                            .controlSize(.small)
                            .disabled(ws.store.busy[name] != nil || ws.store.model(name)?.isOff == false)
                        if ws.store.model(name)?.isOff == false {
                            Text("On now: Send again.").font(.system(size: 11)).foregroundStyle(palette.ink2)
                        }
                    }
                }
                timings(r)
                Picker("Show", selection: $tab) {
                    Text("Answer").tag("answer")
                    Text("Response JSON").tag("response")
                    Text("Request JSON").tag("request")
                }
                .pickerStyle(.segmented).labelsHidden().controlSize(.small)
                ScrollView {
                    Group {
                        switch tab {
                        case "response": mono(responseText(r))
                        case "request": mono(BodyDisplay.elidingImages(String(decoding: r.sent, as: UTF8.self)))
                        default: answer(r)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .topLeading)
                    .padding(10)
                }
                .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(palette.faint))
                HStack {
                    copyButton("Copy response", responseText(r))
                    copyButton("Copy request", String(decoding: r.sent, as: UTF8.self))
                    Spacer()
                    Button("Clear") { tryout.clear() }.disabled(tryout.isRunning)
                }
                .controlSize(.small)
            }
        } else {
            LMEmpty(icon: "paperplane", title: "Nothing sent yet",
                    message: "Compose a request and Send. You will see the exact JSON sent, the answer, its status, request id, timings and token counts. Nothing typed or returned here is saved.",
                    palette: palette)
        }
    }

    private func summary(_ r: TryModel.Result) -> some View {
        HStack(spacing: 10) {
            if r.cancelled {
                Text("Cancelled").font(.system(size: 18, weight: .bold)).foregroundStyle(palette.ink2)
            } else if r.interrupted {
                Text("Interrupted").font(.system(size: 18, weight: .bold)).foregroundStyle(Color(nsColor: .systemOrange))
            } else if let s = r.status {
                Text("\(s)").font(.system(size: 22, weight: .bold).width(.compressed)).foregroundStyle(LMFormat.statusColor(s))
            } else if r.error != nil {
                Text("Failed").font(.system(size: 18, weight: .bold)).foregroundStyle(Color(nsColor: .systemRed))
            } else {
                ProgressView().controlSize(.small)
                Text(r.streamed ? "Waiting for the stream…" : "Waiting for the answer…").foregroundStyle(palette.ink2)
            }
            VStack(alignment: .leading, spacing: 1) {
                if let id = r.requestID {
                    Text(id).font(.system(size: 11, design: .monospaced)).textSelection(.enabled)
                }
                Text(statusLine(r)).font(.system(size: 11)).foregroundStyle(palette.ink2).lineLimit(2).textSelection(.enabled)
                if let retry = r.retryAfter, !r.cancelled {
                    Text("Retry after \(retry) s").font(.system(size: 11, weight: .semibold)).foregroundStyle(Color(nsColor: .systemOrange))
                }
            }
            Spacer()
        }
    }

    /// The model a 403 `model_not_available` names: the one in the request sent.
    private func notAvailableModel(_ r: TryModel.Result) -> String? {
        guard r.status == 403, (try? JSONDecoder().decode(ErrorEnvelope.self, from: r.body))?.isModelNotAvailable == true else { return nil }
        return (try? JSONValue.parse(r.sent))?["model"]?.stringValue
    }

    private func statusLine(_ r: TryModel.Result) -> String {
        if r.cancelled { return "Cancelled. If the model had it, the gateway stopped that worker; the next request reloads it." }
        if r.interrupted { return "Interrupted: the answer is incomplete. Below is what arrived (\(r.events.count) events)." }
        if let e = r.error { return e }
        if let m = r.errorMessage { return m }
        if !r.finished { return r.streamed ? "Streaming: \(r.events.count) events so far" : "Sent" }
        let reason = r.finishReason ?? r.completion?.choices.first?.finishReason
        return [r.streamed ? "Streamed \(r.events.count) events" : nil, reason.map { "finish: \($0)" },
                r.headers["x-unshaped-blocks"].map { "blocks not scored (script): \($0)" }].compactMap { $0 }.joined(separator: " · ")
    }

    private func timings(_ r: TryModel.Result) -> some View {
        Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 2) {
            GridRow {
                head("This app")
                cell("to headers", r.secondsToHeaders.map(sec) ?? "—")
                cell("total", r.secondsTotal.map(sec) ?? "—")
                cell("", "")
            }
            GridRow {
                head("Gateway")
                cell("queue", LMFormat.ms(r.ledger?.queueMs))
                cell("load", LMFormat.ms(r.ledger?.loadMs))
                cell("generation", LMFormat.ms(r.ledger?.generationMs))
            }
            GridRow {
                head("Tokens")
                cell("in", LMFormat.tokens(r.usage?.promptTokens ?? r.ledger?.promptTokens))
                cell("out", LMFormat.tokens(r.usage?.completionTokens ?? r.ledger?.completionTokens))
                cell("", r.finished && r.ledger == nil && r.requestID != nil ? "ledger row not found" : "")
            }
        }
        .font(.system(size: 11.5)).monospacedDigit()
    }

    private func sec(_ s: Double) -> String { s < 10 ? String(format: "%.2f s", s) : String(format: "%.1f s", s) }
    private func head(_ t: String) -> some View { Text(t).font(.system(size: 11, weight: .semibold)).foregroundStyle(palette.ink2) }
    private func cell(_ label: String, _ value: String) -> some View {
        HStack(spacing: 4) {
            Text(label).foregroundStyle(palette.ink3)
            Text(value).foregroundStyle(value == "unknown" ? palette.ink3 : palette.ink)
        }
        .lineLimit(1)
        .fixedSize()
    }

    @ViewBuilder
    private func answer(_ r: TryModel.Result) -> some View {
        if r.streamed && !r.events.isEmpty {
            Text(r.streamText).font(.system(size: 12.5)).textSelection(.enabled)
        } else if let c = r.completion, let text = c.text {
            if let v = try? JSONValue.parse(text), let blocks = v["blocks"]?.arrayValue {
                table(["Class", "Box", "Text"], blocks.map { [$0["class"]?.stringValue ?? "—", box($0["box"]), $0["text"]?.stringValue ?? ""] },
                      caption: "\(blocks.count) blocks")
            } else if let v = try? JSONValue.parse(text), let scores = v["scores"]?.arrayValue {
                table(["Block", "Score"], scores.map { [$0["block"]?.numberValue.map { String(Int($0)) } ?? "—", $0["score"]?.numberValue.map { String(format: "%.4f", $0) } ?? "—"] },
                      caption: "\(scores.count) blocks scored: P(the text matches its crop)")
            } else if let pretty = (try? JSONValue.parse(text)).map({ String(decoding: $0.pretty(), as: UTF8.self) }) {
                mono(pretty)
            } else {
                Text(text).font(.system(size: 12.5)).textSelection(.enabled)
            }
        } else if r.status != nil {
            mono(responseText(r))
        } else {
            Text(r.cancelled ? "No answer: cancelled." : r.error ?? "Waiting…").font(.system(size: 12)).foregroundStyle(palette.ink2)
        }
    }

    private func box(_ v: JSONValue?) -> String {
        guard let a = v?.arrayValue?.compactMap(\.numberValue), a.count == 4 else { return "—" }
        return a.map { String(format: "%.2f", $0) }.joined(separator: ", ")
    }

    private func table(_ headers: [String], _ rows: [[String]], caption: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(caption).font(.system(size: 11, weight: .semibold)).foregroundStyle(palette.ink2)
            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 3) {
                GridRow { ForEach(headers, id: \.self) { Text($0).font(.system(size: 10.5, weight: .medium)).foregroundStyle(palette.ink3) } }
                ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                    GridRow { ForEach(Array(row.enumerated()), id: \.offset) { _, v in Text(v).font(.system(size: 11.5)).textSelection(.enabled) } }
                }
            }
            .monospacedDigit()
        }
    }

    private func responseText(_ r: TryModel.Result) -> String {
        if r.streamed, !r.events.isEmpty {
            return r.events.map { e in e == "[DONE]" ? "data: [DONE]" : "data: " + ((try? JSONValue.parse(e)).map { String(decoding: $0.pretty(), as: UTF8.self) } ?? e) }
                .joined(separator: "\n\n")
        }
        if r.body.isEmpty { return r.finished ? "(empty body)" : "" }
        return JSONValue.prettyText(r.body) ?? String(decoding: r.body, as: UTF8.self)
    }

    private func mono(_ text: String) -> some View {
        Text(text).font(.system(size: 11, design: .monospaced)).textSelection(.enabled)
    }

    private func copyButton(_ title: String, _ text: String) -> some View {
        Button(title) {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
        }
        .disabled(text.isEmpty)
    }
}
