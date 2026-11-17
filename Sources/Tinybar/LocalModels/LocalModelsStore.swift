import Foundation
import LocalModelCore
import Observation

/// Where the gateway stands, as the workspace last saw it.
enum GatewayState: Equatable {
    /// Not asked yet.
    case unknown
    case notRunning
    /// This app started it and it hasn't answered yet.
    case starting
    case running
    /// Something answered, or nothing did in time, but not as the gateway does.
    case unreachable(String)
    /// It runs, but the key file can't be used or the key was rejected.
    case keyProblem(String)

    var label: String {
        switch self {
        case .unknown: "Checking…"
        case .notRunning: "Not running"
        case .starting: "Starting"
        case .running: "Running"
        case .unreachable: "Unreachable"
        case .keyProblem: "Key missing"
        }
    }
}

/// The workspace's view of the gateway: built when the Models workspace opens, released with it.
/// It asks the gateway only when a visible section polls or the user acts.
@MainActor
@Observable
final class LocalModelsStore {
    let settings: LocalModelsSettings
    let launcher: GatewayLauncher

    private(set) var state = GatewayState.unknown
    private(set) var health: Health?
    private(set) var models: [ModelInfo] = []
    private(set) var memory: MemoryTotals?
    private(set) var status: GatewayStatus?
    private(set) var requests: [LedgerRow] = []
    private(set) var log: LogsResponse?
    /// Why the last log read failed, shown in Logs.
    private(set) var logError: String?
    private(set) var registry: RegistryFile?
    private(set) var registryError: String?
    /// When the models list was last read, and whether the latest try failed (so it is stale).
    private(set) var modelsUpdated: Date?
    private(set) var requestsUpdated: Date?
    private(set) var logUpdated: Date?
    private(set) var lastError: (message: String, at: Date)?
    /// Per model: an action under way, and the last one that failed.
    private(set) var busy: [String: String] = [:]
    private(set) var actionErrors: [String: String] = [:]
    private(set) var isChecking = false

    private var client: GatewayClient
    private var clientKey: String

    init(settings: LocalModelsSettings, launcher: GatewayLauncher = .shared) {
        self.settings = settings
        self.launcher = launcher
        client = GatewayClient(address: settings.address, keyFile: settings.keyFile)
        clientKey = "\(settings.address)|\(settings.keyFile.url.path)"
        loadRegistry()
    }

    /// A client for the current address and key file; a new one when either changed.
    var currentClient: GatewayClient {
        let key = "\(settings.address)|\(settings.keyFile.url.path)"
        if key != clientKey {
            // Another gateway: nothing read from the last one is shown as if it were this one's.
            if !clientKey.hasPrefix("\(settings.address)|") {
                models = []
                memory = nil
                status = nil
                health = nil
                requests = []
                log = nil
                logError = nil
                modelsUpdated = nil
                requestsUpdated = nil
                logUpdated = nil
            }
            client = GatewayClient(address: settings.address, keyFile: settings.keyFile)
            clientKey = key
            state = .unknown
        }
        return client
    }

    var isRunning: Bool { state == .running }
    var inFlight: Int { models.reduce(0) { $0 + $1.inFlight } }
    var waiting: Int { models.reduce(0) { $0 + $1.waiting } }
    var loaded: [ModelInfo] { models.filter { $0.state == .loaded || $0.state == .unloading || $0.state == .starting } }
    var pullsUnderway: Bool { models.contains { $0.pull.map { !$0.isFinished } ?? false } }

    func model(_ name: String) -> ModelInfo? { models.first { $0.name == name } }

    func loadRegistry() {
        guard let url = settings.registryFile else {
            registry = nil
            registryError = "No backend folder set."
            return
        }
        do {
            registry = try RegistryFile.load(url)
            registryError = nil
        } catch {
            registry = nil
            registryError = "Couldn't read \(url.path): \(error.localizedDescription)"
        }
    }

    // MARK: Reading

    /// `/health` first (no key), then the key file, then what the section needs.
    @discardableResult
    func checkGateway() async -> Bool {
        let client = currentClient
        isChecking = true
        defer { isChecking = false }
        do {
            health = try await client.health().value
        } catch {
            health = nil
            switch error {
            case .notRunning: state = launcher.isStarting(at: settings.address) ? .starting : .notRunning
            case .timedOut: state = .unreachable("No answer at \(settings.address) within 5 s.")
            case .cancelled: return false
            default: state = .unreachable(error.summary)
            }
            if state != .starting { fail(error.summary) }
            return false
        }
        if let problem = settings.keyFile.check() {
            state = .keyProblem(problem.summary)
            fail(problem.summary)
            return false
        }
        state = .running
        return true
    }

    func refreshModels() async {
        guard await checkGateway() else { return }
        do {
            let r = try await currentClient.models()
            models = r.value.models
            memory = r.value.memory
            modelsUpdated = Date()
            status = try? await currentClient.status().value
        } catch {
            handle(error)
        }
    }

    /// The popover's read: `/health`, then `/api/models` and `/api/status`, then the last 5 requests.
    func refreshForMenu() async {
        await refreshModels()
        guard state == .running else { return }
        if let rows = try? await currentClient.requests(limit: 5).value.requests {
            requests = rows
            requestsUpdated = Date()
        }
    }

    /// The latest rows; `model` and `outcome` filter on the gateway (older gateways ignore them, and
    /// the section filters again).
    func refreshRequests(limit: Int = 300, model: String? = nil, outcome: String? = nil) async {
        guard await checkGateway() else { return }
        do {
            requests = try await currentClient.requests(limit: limit, model: model, outcome: outcome).value.requests
            requestsUpdated = Date()
            if models.isEmpty { models = (try? await currentClient.models().value.models) ?? [] }
        } catch {
            handle(error)
        }
    }

    /// The tail; with `follow`, only what was written since the last read (when the gateway gives
    /// an offset), appended, keeping the latest 2,000 lines.
    func refreshLog(source: String, lines: Int, follow: Bool = false) async {
        guard await checkGateway() else { return }
        do {
            if follow, let current = log, current.source == source, let since = current.next {
                let more = try await currentClient.logs(source: source, lines: lines, since: since).value
                var merged = more.rotated == true ? more.lines : current.lines + more.lines
                if merged.count > 2000 { merged.removeFirst(merged.count - 2000) }
                log = LogsResponse(source: source, lines: merged, next: more.next ?? since, rotated: more.rotated)
            } else {
                log = try await currentClient.logs(source: source, lines: lines).value
            }
            logUpdated = Date()
            logError = nil
        } catch {
            if error != .cancelled { logError = error.summary }
            handle(error)
        }
    }

    /// A ledger row by request id, for Try's timings. The gateway writes it as the answer ends.
    func ledgerRow(_ id: String) async -> LedgerRow? {
        for attempt in 0..<4 {
            if attempt > 0 { try? await Task.sleep(for: .milliseconds(350)) }
            if let row = try? await currentClient.requests(limit: 50).value.requests.first(where: { $0.id == id }) { return row }
        }
        return nil
    }

    private func handle(_ error: GatewayError) {
        if case .cancelled = error { return }
        if error.isKeyProblem { state = .keyProblem(error.summary) } else if error == .notRunning { state = .notRunning }
        fail(error.summary)
    }

    private func fail(_ message: String) {
        lastError = (message, Date())
    }

    // MARK: Acting

    func load(_ name: String) async { await act(name, "Loading") { try await $0.load(name).value.state } }
    func unload(_ name: String) async { await act(name, "Unloading") { try await $0.unload(name).value.state } }
    func pin(_ name: String, _ on: Bool) async { await act(name, on ? "Pinning" : "Unpinning") { String(try await $0.pin(name, on).value.pinned) } }
    /// `tinybar-models login install` or `remove`, then the gateway's own word on it.
    func setLoginItem(_ on: Bool) async -> String {
        let (ok, output) = await launcher.run(settings, ["login", on ? "install" : "remove"])
        if !ok { fail(output.isEmpty ? "tinybar-models login failed" : output) }
        await refreshModels()
        return output
    }

    func setAvailable(_ name: String, _ on: Bool) async {
        await act(name, on ? "Turning on" : "Turning off") { try await $0.setAvailable(name, on).value.state ?? "" }
    }
    var availableCount: Int? {
        models.contains { $0.available != nil } ? models.filter { !$0.isOff }.count : status?.availableModels
    }
    func pull(_ name: String) async { await act(name, "Starting pull") { try await $0.pull(name).value.phase } }
    func cancelPull(_ name: String) async { await act(name, "Cancelling pull") { try await $0.cancelPull(name).value.phase } }

    private func act(_ name: String, _ doing: String, _ call: (GatewayClient) async throws -> String) async {
        guard busy[name] == nil else { return }
        busy[name] = doing
        actionErrors[name] = nil
        do {
            _ = try await call(currentClient)
        } catch {
            let e = error as? GatewayError ?? .transport(String(describing: error))
            if e != .cancelled {
                actionErrors[name] = e.summary
                fail("\(name): \(e.summary)")
            }
        }
        busy[name] = nil
        await refreshModels()
    }

    // MARK: Start and stop

    /// Start runs `serve`. A gateway that already answers is success: nothing is started (the
    /// backend allows one gateway per state folder, and a second `serve` exits 1).
    func start() async {
        if await checkGateway() {
            launcher.noteAlreadyRunning(settings.address)
            await refreshModels()
            return
        }
        // Installed at login: launchd owns the gateway, so Start asks it (`tinybar-models start`).
        let viaLaunchd = status?.loginItem == "installed" || status?.startedByLaunchd == true
        if viaLaunchd {
            state = .starting
            let (ok, output) = await launcher.run(settings, ["start"])
            if !ok { fail(output.isEmpty ? "tinybar-models start failed" : output) }
        } else {
            do {
                try launcher.start(settings)
                state = .starting
            } catch {
                fail(error.summary)
                return
            }
        }
        // Ask until it answers, for up to a minute; one more time after `serve` exits.
        for _ in 0..<60 {
            try? await Task.sleep(for: .seconds(1))
            if Task.isCancelled { return }
            if await checkGateway() { break }
            if viaLaunchd {
                if state == .notRunning { state = .starting }
                continue
            }
            if !launcher.childRunning {
                if await checkGateway() { launcher.noteAlreadyRunning(settings.address) }
                break
            }
        }
        await refreshModels()
    }

    /// Stop asks the gateway at the address shown to stop (`POST /api/shutdown`): its workers, then
    /// itself. Only a process this app started may then get SIGTERM, if it lingers.
    /// Whether the gateway shown is the one this app started (address, port, start time agree).
    var ownsGateway: Bool { launcher.owns(settings.address, status: status) }

    func stop() async {
        // Judged before the shutdown, against the gateway at this address: after it, nothing answers.
        let owned = ownsGateway
        do {
            _ = try await currentClient.shutdown()
            launcher.noteStopped("Asked the gateway at \(settings.address) to stop.")
        } catch {
            fail(error.summary)
            launcher.noteStopped("Stop failed: \(error.summary)")
            return
        }
        for _ in 0..<60 {
            try? await Task.sleep(for: .milliseconds(500))
            if !(await checkGateway()) { break }
        }
        await launcher.finishOwn(owned: owned)
        // The models list stays as last read, shown as stale: nothing is assumed about it.
        _ = await checkGateway()
    }

    /// For the snapshot tooling: show a state without asking anyone.
    func showForSnapshot(state: GatewayState) { self.state = state }
}
