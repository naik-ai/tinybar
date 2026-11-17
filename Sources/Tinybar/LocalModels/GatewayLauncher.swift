import Foundation
import LocalModelCore
import Observation
import TinybarCore

/// Starts and stops the gateway with the backend's own CLI (`tinybar-models serve` / `stop`). It
/// keeps the one process it started and never signals any other: not by port, not by name. A
/// gateway started elsewhere is stopped only through `tinybar-models stop`, which asks it over its
/// API, and only after the user confirms.
@MainActor
@Observable
final class GatewayLauncher {
    static let shared = GatewayLauncher()

    /// The `serve` process this app started, while it runs.
    private(set) var started: Process?
    private(set) var startedAt: Date?
    private(set) var isStopping = false
    /// The last thing Start or Stop said, for the Overview.
    private(set) var note: String?

    /// The child this app started is still running (whatever answers at any address).
    var childRunning: Bool { started?.isRunning == true }
    /// Where, as whom and when this app launched its child: ownership is judged against it.
    private(set) var record: LaunchRecord?

    /// Whether the gateway answering at `address` is the one this app started (its status says so).
    func owns(_ address: GatewayAddress, status: GatewayStatus?) -> Bool {
        GatewayOwnership.owns(record, childRunning: childRunning, address: address, status: status)
    }

    /// Within a minute and a half of Start, a gateway that doesn't answer yet is starting.
    func isStarting(at address: GatewayAddress) -> Bool {
        guard childRunning, record?.address == address, let startedAt else { return false }
        return Date().timeIntervalSince(startedAt) < 90
    }

    enum Failure: Error {
        case noBackend
        case notInstalled(String)
        case couldNotRun(String)
        case refused(String)

        var summary: String {
            switch self {
            case .noBackend: "Set the backend folder in Settings first."
            case let .notInstalled(path): "No \(path). Set up the backend once with `uv sync --directory backend`."
            case let .couldNotRun(why): "Couldn't run the backend: \(why)"
            case let .refused(why): why
            }
        }
    }

    private var log: URL { Paths.appSupport.appendingPathComponent("gateway-launch.log") }

    private func executable(_ settings: LocalModelsSettings) throws(Failure) -> URL {
        guard let folder = settings.backendFolder else { throw .noBackend }
        do {
            return try BackendFolder.cli(in: folder)
        } catch {
            throw .refused(error.summary)
        }
    }

    /// The environment the CLI gets: a plain PATH and home, the port, nothing else of this app's.
    private func environment(port: Int) -> [String: String] {
        let env = ProcessInfo.processInfo.environment
        var out = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": NSHomeDirectory(), "TINYBAR_MODELS_PORT": String(port)]
        for k in ["LANG", "TMPDIR", "USER", "LOGNAME"] { out[k] = env[k] }
        return out
    }

    /// `tinybar-models serve --port <port>`, detached: it keeps running when this window closes or
    /// the app quits. Its own output goes to gateway-launch.log in the app's folder.
    func start(_ settings: LocalModelsSettings) throws(Failure) {
        guard !childRunning else { throw .refused("This app already started a gateway (pid \(started?.processIdentifier ?? 0)); stop that one first.") }
        let cli = try executable(settings)
        let p = Process()
        p.executableURL = cli
        p.arguments = ["serve", "--port", String(settings.address.port)]
        p.currentDirectoryURL = settings.backendFolder
        p.environment = environment(port: settings.address.port)
        p.standardInput = FileHandle.nullDevice
        if !FileManager.default.fileExists(atPath: log.path) { FileManager.default.createFile(atPath: log.path, contents: nil) }
        if let out = try? FileHandle(forWritingTo: log) {
            out.seekToEndOfFile()
            out.write(Data("\n--- \(Date().formatted(.iso8601)) serve --port \(settings.address.port)\n".utf8))
            p.standardOutput = out
            p.standardError = out
        } else {
            p.standardOutput = FileHandle.nullDevice
            p.standardError = FileHandle.nullDevice
        }
        p.terminationHandler = { [weak self] proc in
            let code = proc.terminationStatus
            Task { @MainActor in
                guard let self, self.started === proc else { return }
                self.started = nil
                if !self.isStopping, code != 0, self.note?.hasPrefix("A gateway already") != true {
                    self.note = "tinybar-models serve exited with status \(code); its output: \(Paths.abbreviated(self.log))"
                }
            }
        }
        do {
            try p.run()
        } catch {
            throw .couldNotRun(error.localizedDescription)
        }
        started = p
        startedAt = Date()
        record = LaunchRecord(address: settings.address, pid: p.processIdentifier, startedAt: startedAt!)
        note = "Started tinybar-models serve (pid \(p.processIdentifier))."
    }

    /// A gateway already answered: Start succeeded without starting anything.
    func noteAlreadyRunning(_ address: some CustomStringConvertible) {
        note = "A gateway already runs at \(address); Start left it as it is."
    }

    /// After the gateway was asked to stop (over its API, at the address shown): wait for the process
    /// this app started, if any, and give it alone SIGTERM if it still runs 20 s later.
    func finishOwn(owned: Bool) async {
        // Only the child serving the address that was stopped, and only when it was known to be ours.
        guard owned, let ours = started else { return }
        isStopping = true
        defer { isStopping = false }
        for _ in 0..<100 where ours.isRunning { try? await Task.sleep(for: .milliseconds(200)) }
        if ours.isRunning {
            ours.terminate()
            note = "The gateway this app started did not stop within 20 s; it was sent SIGTERM (pid \(ours.processIdentifier))."
        }
    }

    func noteStopped(_ text: String) { note = text }

    /// Runs the validated backend CLI with these arguments (`login install`, `start`…) and waits.
    func run(_ settings: LocalModelsSettings, _ args: [String]) async -> (ok: Bool, output: String) {
        let cli: URL
        do { cli = try executable(settings) } catch { return (false, error.summary) }
        let (status, output) = await Self.run(cli, args, env: environment(port: settings.address.port), in: settings.backendFolder)
        note = output.isEmpty ? "tinybar-models \(args.joined(separator: " ")) exited with \(status)." : output
        return (status == 0, output)
    }

    private nonisolated static func run(_ exe: URL, _ args: [String], env: [String: String], in dir: URL?) async -> (Int32, String) {
        await withCheckedContinuation { continuation in
            let p = Process()
            p.executableURL = exe
            p.arguments = args
            p.environment = env
            p.currentDirectoryURL = dir
            let pipe = Pipe()
            p.standardOutput = pipe
            p.standardError = pipe
            p.standardInput = FileHandle.nullDevice
            p.terminationHandler = { proc in
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                let text = String(decoding: data.prefix(2000), as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
                continuation.resume(returning: (proc.terminationStatus, text))
            }
            do {
                try p.run()
            } catch {
                p.terminationHandler = nil
                continuation.resume(returning: (-1, error.localizedDescription))
            }
        }
    }
}
