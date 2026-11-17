import AppKit
import LocalModelCore
import SwiftUI

/// Developer aid: `Tinybar --snapshot out.png --local [--address URL] [--key-file PATH] [--backend DIR]`
/// renders the Stats window's Local models page, every section and its states, in light and dark,
/// against whatever answers at the address (the stand-in in Tests/StandIn, never the real gateway in
/// development). Settings given here are used for the run only, never saved. Try's requests are
/// invented, and its image is drawn here.
@MainActor
enum LocalModelsSnapshot {
    static func run(model: AppModel, out: URL, args: [String]) async -> Never {
        func value(_ flag: String) -> String? {
            args.firstIndex(of: flag).flatMap { $0 + 1 < args.count ? args[$0 + 1] : nil }
        }
        // Isolated by construction: the stand-in's address and key unless told otherwise, never the
        // real gateway (its port or its key file) without both `--real` and MODELBAR_SNAPSHOT_REAL=1.
        let real = args.contains("--real") && ProcessInfo.processInfo.environment["MODELBAR_SNAPSHOT_REAL"] == "1"
        if args.contains("--real"), !real { fail("--real needs MODELBAR_SNAPSHOT_REAL=1 as well; nothing was asked.") }
        let settings = LocalModelsSettings(defaults: UserDefaults(suiteName: "modelbar-snapshot-\(UUID().uuidString)")!, savesChanges: false)
        let standInKey = URL(fileURLWithPath: ProcessInfo.processInfo.environment["TMPDIR"] ?? NSTemporaryDirectory())
            .appendingPathComponent("tinybar-standin/api-key")
        settings.address = real ? .default : GatewayAddress(port: 18990)!
        settings.keyFile = real ? .standard() : KeyFile(url: standInKey)
        settings.backendFolder = real ? nil : settings.backendFolder
        if let a = value("--address") {
            do { settings.address = try GatewayAddress(parsing: a) } catch { fail("--address: \(error.summary)") }
        }
        if let k = value("--key-file") { settings.keyFile = KeyFile(url: URL(fileURLWithPath: k)) }
        if let b = value("--backend"), !real { settings.backendFolder = URL(fileURLWithPath: b, isDirectory: true) }
        let realKey = KeyFile.standard().url.resolvingSymlinksInPath().path
        if !real, settings.address.port == GatewayAddress.defaultPort || settings.keyFile.url.resolvingSymlinksInPath().path == realKey {
            fail("Refusing: \(settings.address) or that key file is the real gateway's. Snapshots use the stand-in unless --real and MODELBAR_SNAPSHOT_REAL=1 are both given.")
        }
        print("Snapshots will ask \(real ? "the REAL gateway (read-only routes and three small invented Try requests)" : "the stand-in") at \(settings.address), key file \(settings.keyFile.url.path)")
        let only = value("--only")

        // `--size 1440x900`: the window's content size, to check how the page fills a larger window.
        let size: NSSize? = value("--size").flatMap { text in
            let wh = text.split(separator: "x").compactMap { Double($0) }
            return wh.count == 2 ? NSSize(width: wh[0], height: wh[1]) : nil
        }
        let suffixSize = size.map { "-\(Int($0.width))x\(Int($0.height))" } ?? ""
        let page = Self.page()

        var written: [String] = []
        let notOnReal: Set = ["models-pulls", "try-schema", "try-busy-503", "start-1-not-running", "start-2-started", "start-3-stopped"]
        func shoot(_ name: String, _ section: LocalSection, live: Bool = true, prepare: (LocalModelsWorkspaceState) async -> Void = { _ in }) async {
            if let only, !name.hasPrefix(only) { return }
            if real, notOnReal.contains(name) { return }
            SnapshotDefaults.show(["windowPage": WindowPage(section).rawValue])
            let ws = LocalModelsWorkspaceState(settings: settings, section: section, savesSection: false)
            await prepare(ws)
            let presence = WindowPresence(visible: live)
            let host = NSHostingView(rootView: StatsView(model: model, presence: presence, localState: ws))
            let window = NSWindow(contentRect: NSRect(origin: .zero, size: size ?? host.fittingSize),
                                  styleMask: [.borderless], backing: .buffered, defer: false)
            window.contentView = host
            if let size { host.frame = NSRect(origin: .zero, size: size) }
            SnapshotDefaults.placeOffscreen(window)
            for (appearance, suffix) in [(NSAppearance.Name.aqua, ""), (.darkAqua, ".dark")] {
                window.appearance = NSAppearance(named: appearance)
                try? await Task.sleep(for: .milliseconds(live ? 1400 : 500))
                host.layoutSubtreeIfNeeded()
                guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { fail("Couldn't render \(name).") }
                host.cacheDisplay(in: host.bounds, to: rep)
                let url = URL(fileURLWithPath: out.deletingPathExtension().path + ".local-\(name)\(suffixSize)\(suffix).png")
                guard let png = rep.representation(using: .png, properties: [:]) else { fail("Couldn't encode \(name).") }
                do { try png.write(to: url) } catch { fail("Couldn't write \(url.path).") }
                written.append(url.path)
            }
            presence.visible = false
            window.orderOut(nil)
        }

        func waitFor(_ ws: LocalModelsWorkspaceState, seconds: Double = 25, _ done: () -> Bool) async {
            let end = Date().addingTimeInterval(seconds)
            while !done(), Date() < end { try? await Task.sleep(for: .milliseconds(200)) }
        }

        func send(_ ws: LocalModelsWorkspaceState, _ setup: (TryModel) -> Void) async {
            await ws.store.refreshModels()
            setup(ws.tryout)
            ws.tryout.send(ws.store, kind: ws.store.model(ws.tryout.modelName)?.serverKind)
            await waitFor(ws) { ws.tryout.result?.finished == true && !ws.tryout.isRunning }
            if ws.tryout.result?.requestID != nil { await waitFor(ws, seconds: 3) { ws.tryout.result?.ledger != nil } }
        }

        // `--exercise-start`: nothing answers yet; Start runs the backend folder's CLI (the stand-in's
        // shim in development), and the run ends with Stop.
        let exercise = args.contains("--exercise-start") && !real
        if exercise {
            await shoot("start-1-not-running", .overview, live: false) { await $0.store.checkGateway() }
            await shoot("start-2-started", .overview) { await $0.store.start() }
        }
        // The menu-bar popover, in each state.
        let monitor = GatewayMonitor()
        func shootPopover(_ name: String, prepare: (LocalModelsWorkspaceState) async -> Void) async {
            if let only, !("popover-" + name).hasPrefix(only) { return }
            let ws = LocalModelsWorkspaceState(settings: settings, section: .overview, savesSection: false)
            await prepare(ws)
            let host = NSHostingView(rootView: ModelbarPopover(app: model, monitor: monitor, preset: ws, refreshes: false)
                .background(Color(nsColor: .windowBackgroundColor)))
            let window = NSWindow(contentRect: NSRect(origin: .zero, size: host.fittingSize), styleMask: [.borderless], backing: .buffered, defer: false)
            window.contentView = host
            SnapshotDefaults.placeOffscreen(window)
            for (appearance, suffix) in [(NSAppearance.Name.aqua, ""), (.darkAqua, ".dark")] {
                window.appearance = NSAppearance(named: appearance)
                try? await Task.sleep(for: .milliseconds(500))
                window.setContentSize(host.fittingSize)
                host.layoutSubtreeIfNeeded()
                guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { fail("Couldn't render the popover.") }
                host.cacheDisplay(in: host.bounds, to: rep)
                let url = URL(fileURLWithPath: out.deletingPathExtension().path + ".popover-\(name)\(suffix).png")
                guard let png = rep.representation(using: .png, properties: [:]) else { fail("Couldn't encode \(name).") }
                do { try png.write(to: url) } catch { fail("Couldn't write \(url.path).") }
                written.append(url.path)
            }
            window.orderOut(nil)
        }
        await shootPopover("running") { await $0.store.refreshForMenu() }
        if !real {
            // Every model off, then back on: the stand-in keeps the change only while it runs.
            await shootPopover("all-off") { ws in
                await ws.store.refreshForMenu()
                for m in ws.store.models where m.available == true { await ws.store.setAvailable(m.name, false) }
                await ws.store.refreshForMenu()
            }
            let restore = LocalModelsWorkspaceState(settings: settings, section: .overview, savesSection: false)
            await restore.store.refreshForMenu()
            for m in restore.store.models where m.isOff && m.name != "qwen3.5-27b" { await restore.store.setAvailable(m.name, true) }
        }
        let deadPort = GatewayAddress(port: Self.freePort())!
        let saved = (settings.address, settings.keyFile)
        await shootPopover("not-running") { ws in
            ws.settings.address = deadPort
            await ws.store.refreshForMenu()
        }
        settings.address = saved.0
        await shootPopover("key-missing") { ws in
            ws.settings.keyFile = KeyFile(url: URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("no-such-dir/api-key"))
            await ws.store.refreshForMenu()
        }
        settings.keyFile = saved.1
        await shootPopover("unreachable") { ws in
            ws.store.showForSnapshot(state: .unreachable("No answer at \(ws.settings.address) within 5 s."))
        }

        await shoot("overview", .overview) { await $0.store.refreshModels() }
        await shoot("models", .models) { await $0.store.refreshModels() }
        await shoot("models-details", .models) { ws in
            await ws.store.refreshModels()
            ws.details = "jev"
        }
        // A pull under way and one that fails (the stand-in fails jev's after 3 s).
        await shoot("models-pulls", .models) { ws in
            await ws.store.refreshModels()
            await ws.store.pull("qwen3.5-27b")
            await ws.store.pull("jev")
            try? await Task.sleep(for: .seconds(4.5))
            await ws.store.refreshModels()
        }
        if !real {
            // A pull asked to stop that hasn't stopped yet.
            await shoot("models-pull-cancelling", .models) { ws in
                await ws.store.refreshModels()
                await ws.store.pull("minicpm-ocr")
                try? await Task.sleep(for: .seconds(1.5))
                await ws.store.cancelPull("minicpm-ocr")
            }
            // A Try request still streaming, seen from Activity: first in the list, with its Cancel.
            await shoot("activity-in-flight", .activity) { ws in
                await ws.store.refreshModels()
                ws.tryout.modelName = "qwen3.5-4b"
                ws.tryout.user = "Stream a few invented words."
                ws.tryout.stream = true
                ws.tryout.send(ws.store, kind: "mlx-vlm")
                try? await Task.sleep(for: .seconds(1))
                await ws.store.refreshRequests()
            }
        }
        await shoot("try-empty", .tryout) { await $0.store.refreshModels() }
        await shoot("try-stream", .tryout) { ws in
            await send(ws) { t in
                t.modelName = "qwen3.5-4b"
                t.system = "Answer in plain sentences."
                t.user = real ? "Say hello in five words." : "Say what this stand-in gateway is for."
                t.stream = true
                t.maxTokens = real ? "32" : "128"
            }
        }
        await shoot("try-schema", .tryout) { ws in
            await send(ws) { t in
                t.modelName = "qwen3.5-4b"
                t.user = "Read the title and total from this invented page."
                t.setImage(data: page, name: "invented-page.png")
                t.stream = false
                t.useSchema = true
            }
        }
        await shoot("try-nemotron", .tryout) { ws in
            await send(ws) { t in
                t.modelName = "nemotron-parse"
                t.setImage(data: page, name: "invented-page.png")
                t.maxTokens = "8192"
            }
        }
        await shoot("try-jev", .tryout) { ws in
            await send(ws) { t in
                t.modelName = "jev"
                t.setImage(data: page, name: "invented-page.png")
            }
        }
        await shoot("try-busy-503", .tryout) { ws in
            await send(ws) { t in
                t.modelName = "minicpm-ocr"
                t.user = "Are you busy?"
                t.stream = false
            }
        }
        await shoot("try-raw-404", .tryout) { ws in
            await send(ws) { t in
                t.mode = .raw
                t.raw = "{\n  \"model\": \"no-such-model\",\n  \"messages\": [{\"role\": \"user\", \"content\": \"hello\"}]\n}"
            }
        }
        await shoot("activity", .activity) { await $0.store.refreshRequests() }
        await shoot("logs", .logs) { await $0.store.refreshLog(source: "gateway", lines: 200) }
        await shoot("settings", .settings) { await $0.store.checkGateway() }

        // States: a gateway that isn't there, a stale view, a missing key.
        let address = settings.address, key = settings.keyFile
        let dead = GatewayAddress(port: Self.freePort())!
        await shoot("overview-not-running", .overview, live: false) { ws in
            ws.settings.address = dead
            await ws.store.checkGateway()
        }
        settings.address = address
        await shoot("models-stale", .models, live: false) { ws in
            await ws.store.refreshModels()
            ws.settings.address = dead
            await ws.store.refreshModels()
        }
        settings.address = address
        await shoot("overview-key-missing", .overview, live: false) { ws in
            ws.settings.keyFile = KeyFile(url: URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("no-such-dir/api-key"))
            await ws.store.refreshModels()
        }
        settings.keyFile = key
        await shoot("activity-not-running", .activity, live: false) { ws in
            ws.settings.address = dead
            await ws.store.refreshRequests()
        }
        settings.address = address

        if exercise {
            await shoot("start-3-stopped", .overview, live: false) { ws in
                await ws.store.refreshModels()
                await ws.store.stop()
            }
            print("launcher: \(GatewayLauncher.shared.note ?? "")")
        }
        print(written.joined(separator: "\n"))
        exit(0)
    }

    /// An invented page: a title, one line and a total, nothing from any real document.
    static func page() -> Data {
        let size = NSSize(width: 850, height: 1100)
        let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width), pixelsHigh: Int(size.height), bitsPerSample: 8,
                                   samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
        NSColor.white.setFill()
        NSRect(origin: .zero, size: size).fill()
        let lines: [(String, CGFloat, CGFloat)] = [("Invented Page", 40, 1010), ("Item one 10.00", 24, 860), ("Total 10.00", 26, 480)]
        for (text, points, y) in lines {
            (text as NSString).draw(at: NSPoint(x: 70, y: y), withAttributes: [.font: NSFont.systemFont(ofSize: points, weight: .semibold), .foregroundColor: NSColor.black])
        }
        NSGraphicsContext.restoreGraphicsState()
        return rep.representation(using: .png, properties: [:])!
    }

    static func freePort() -> Int {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = withUnsafePointer(to: &addr) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, len) } }
        _ = withUnsafeMutablePointer(to: &addr) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(fd, $0, &len) } }
        let port = Int(UInt16(bigEndian: addr.sin_port))
        return port > 1024 ? port : 18999
    }

    private static func fail(_ message: String) -> Never {
        FileHandle.standardError.write(Data((message + "\n").utf8))
        exit(1)
    }
}
