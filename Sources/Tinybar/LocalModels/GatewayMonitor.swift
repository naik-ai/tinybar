import AppKit
import LocalModelCore
import Observation

/// What the menu bar shows about the gateway. In the background it knows only what `/health`
/// says (no key: four numbers); while the popover is open it also learns about requests in flight.
@MainActor
@Observable
final class GatewayMonitor {
    enum Status: Equatable {
        case unknown, notRunning, running, unreachable, keyMissing
    }

    private(set) var status = Status.unknown
    private(set) var health: Health?
    /// Requests in flight, from the last keyed read (the popover); nil when not known.
    private(set) var inFlight: Int?
    private(set) var checks = 0

    private var timer: Timer?
    private var asleep = false
    private var client: GatewayClient?
    private var clientAddress: GatewayAddress?
    private var inFlightAt: Date?

    /// Starts the background check: now, then every few seconds by the setting (60 s when stopped).
    func start() {
        let ws = NSWorkspace.shared.notificationCenter
        ws.addObserver(forName: NSWorkspace.willSleepNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.asleep = true
                self?.timer?.invalidate()
            }
        }
        ws.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.asleep = false
                self?.check()
            }
        }
        check()
    }

    /// One `/health` call, then the next one scheduled.
    func check() {
        guard !asleep else { return }
        Task {
            await probe()
            schedule()
        }
    }

    private func schedule() {
        timer?.invalidate()
        guard !asleep else { return }
        let seconds = status == .running || status == .keyMissing
            ? Double(LocalModelsSettings.healthSeconds()) : 60
        let t = Timer(timeInterval: seconds, repeats: false) { [weak self] _ in
            MainActor.assumeIsolated { self?.check() }
        }
        t.tolerance = seconds / 5  // let macOS coalesce wakeups
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    private func probe() async {
        let address = LocalModelsSettings.address()
        if client == nil || clientAddress != address {
            client = GatewayClient(address: address, keyFile: LocalModelsSettings.keyFile(), clientName: "modelbar-menu")
            clientAddress = address
        }
        checks += 1
        do {
            health = try await client!.health().value
            // The key file is checked on disk only: nothing keyed is sent in the background.
            status = LocalModelsSettings.keyFile().check() == nil ? .running : .keyMissing
        } catch {
            health = nil
            status = error == .notRunning ? .notRunning : .unreachable
        }
        // What the popover learnt about requests in flight goes stale after a minute.
        if let at = inFlightAt, Date().timeIntervalSince(at) > 60 { inFlight = nil }
    }

    /// The popover's keyed read: the state and requests in flight, until the next `/health`.
    func observe(_ store: LocalModelsStore) {
        switch store.state {
        case .running: status = .running
        case .notRunning: status = .notRunning
        case .keyProblem: status = .keyMissing
        case .unreachable: status = .unreachable
        default: break
        }
        if let h = store.health { health = h }
        if !store.models.isEmpty {
            inFlight = store.inFlight
            inFlightAt = Date()
        }
    }

    /// The glyph's state, from what is known.
    var glyph: GatewayGlyph.State {
        switch status {
        case .unknown, .notRunning: return .stopped
        case .unreachable, .keyMissing: return .error
        case .running:
            guard let m = health?.memory, m.loadedModels > 0 else { return .idle(busy: (inFlight ?? 0) > 0) }
            return .loaded(fraction: Double(m.reservedMb) / Double(max(m.budgetTotalMb, 1)), busy: (inFlight ?? 0) > 0)
        }
    }

    /// "2 · 9 GB": loaded models and their measured memory, for the optional text.
    var text: String? {
        guard status == .running, let m = health?.memory, m.loadedModels > 0 else { return nil }
        return "\(m.loadedModels) · \(LMFormat.mb(m.footprintMb).replacingOccurrences(of: ".0 GB", with: " GB"))"
    }

    /// The state in words, for VoiceOver and the tooltip.
    var spoken: String {
        let name = ForkIdentity.appName
        switch status {
        case .unknown: return "\(name): checking the model gateway"
        case .notRunning: return "\(name): the model gateway is not running"
        case .unreachable: return "\(name): the model gateway can't be reached"
        case .keyMissing: return "\(name): the model gateway runs, but its key can't be used"
        case .running:
            guard let m = health?.memory else { return "\(name): the model gateway is running" }
            let loaded = m.loadedModels == 0 ? "no model loaded" : m.loadedModels == 1 ? "1 model loaded" : "\(m.loadedModels) models loaded"
            let busy = (inFlight ?? 0) > 0 ? ", \(inFlight!) request\(inFlight == 1 ? "" : "s") in flight" : ""
            return "\(name): gateway running, \(loaded), \(LMFormat.mb(m.reservedMb)) of \(LMFormat.mb(m.budgetTotalMb)) budget held\(busy)"
        }
    }
}

import TinybarCore

/// Modelbar's menu-bar mark: a template glyph (monochrome; the menu bar colours it), unlike
/// Tinybar's coloured limit ring. Stopped: a faint dashed ring. Running, idle: a ring. Models loaded:
/// the ring fills like a pie with the share of the memory budget held. A request in flight: a dot at
/// the top right. An error: a slash through the ring.
enum GatewayGlyph {
    enum State: Equatable {
        case stopped
        case idle(busy: Bool)
        case loaded(fraction: Double, busy: Bool)
        case error
    }

    static let size = NSSize(width: 18, height: 18)

    static func image(_ state: State) -> NSImage {
        let image = NSImage(size: size, flipped: false) { rect in
            let ring = NSRect(x: 2.5, y: 2.5, width: 13, height: 13)
            let center = NSPoint(x: ring.midX, y: ring.midY)
            NSColor.black.set()
            switch state {
            case .stopped:
                NSColor.black.withAlphaComponent(0.55).set()
                let path = NSBezierPath(ovalIn: ring)
                path.lineWidth = 1.5
                path.setLineDash([2.2, 2.2], count: 2, phase: 0)
                path.stroke()
            case let .idle(busy), let .loaded(_, busy):
                let path = NSBezierPath(ovalIn: ring)
                path.lineWidth = 1.6
                path.stroke()
                if case let .loaded(fraction, _) = state {
                    // The held share as a pie inside the ring, from 12 o'clock; never too small to see.
                    let f = min(max(fraction, 0.12), 1)
                    let pie = NSBezierPath()
                    pie.move(to: center)
                    pie.appendArc(withCenter: center, radius: 4.6, startAngle: 90, endAngle: 90 - 360 * f, clockwise: true)
                    pie.close()
                    pie.fill()
                }
                if busy {
                    // Activity: a dot cut out of the ring at the top right.
                    let dot = NSRect(x: rect.maxX - 6, y: rect.maxY - 6, width: 5, height: 5)
                    NSGraphicsContext.current?.compositingOperation = .clear
                    NSBezierPath(ovalIn: dot.insetBy(dx: -1.2, dy: -1.2)).fill()
                    NSGraphicsContext.current?.compositingOperation = .sourceOver
                    NSColor.black.set()
                    NSBezierPath(ovalIn: dot).fill()
                }
            case .error:
                let path = NSBezierPath(ovalIn: ring)
                path.lineWidth = 1.6
                path.stroke()
                let slash = NSBezierPath()
                slash.move(to: NSPoint(x: ring.minX + 0.5, y: ring.minY + 0.5))
                slash.line(to: NSPoint(x: ring.maxX - 0.5, y: ring.maxY - 0.5))
                slash.lineWidth = 2
                slash.lineCapStyle = .round
                slash.stroke()
            }
            return true
        }
        image.isTemplate = true
        return image
    }
}
