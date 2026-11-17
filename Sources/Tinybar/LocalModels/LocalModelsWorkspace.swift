import AppKit
import LocalModelCore
import Observation
import SwiftUI
import TinybarCore

/// The Models workspace's sections, in its sidebar.
enum LocalSection: String, CaseIterable, Identifiable {
    case overview, models, tryout = "try", activity, logs, settings

    var id: String { rawValue }

    var title: String {
        switch self {
        case .overview: "Overview"
        case .models: "Installed"
        case .tryout: "Try"
        case .activity: "Activity"
        case .logs: "Logs"
        case .settings: "Settings"
        }
    }

    var icon: String {
        switch self {
        case .overview: "gauge.with.dots.needle.33percent"
        case .models: "shippingbox"
        case .tryout: "paperplane"
        case .activity: "list.bullet.rectangle"
        case .logs: "text.alignleft"
        case .settings: "gearshape"
        }
    }

    var subtitle: String {
        switch self {
        case .overview: "The gateway, its memory and what is loaded"
        case .models: "Every registry entry: load, unload, pin, pull"
        case .tryout: "Send a request through the gateway's own API"
        case .activity: "Recent requests from the gateway's ledger"
        case .logs: "The gateway's and each model's log"
        case .settings: "Where the backend is and how to reach it"
        }
    }
}

/// Everything the workspace holds while its window is open: the store, the section, Try's draft
/// (never saved) and which model's details are open. Dropped with the window.
@MainActor
@Observable
final class LocalModelsWorkspaceState {
    let settings: LocalModelsSettings
    let store: LocalModelsStore
    let tryout = TryModel()
    var section: LocalSection {
        didSet { if savesSection { UserDefaults.standard.set(section.rawValue, forKey: "localModels.section") } }
    }
    var details: String?
    /// The app, for the switches in Settings that are the app's (subscription tracking).
    weak var app: AppModel?
    /// Activity's filters, asked of the gateway on each poll.
    var activityModel = ""
    var activityOutcome = "all"
    private let savesSection: Bool

    init(settings: LocalModelsSettings = LocalModelsSettings(), section: LocalSection? = nil, savesSection: Bool = true) {
        self.settings = settings
        store = LocalModelsStore(settings: settings)
        self.savesSection = savesSection
        self.section = section ?? UserDefaults.standard.string(forKey: "localModels.section").flatMap(LocalSection.init) ?? .overview
    }
}

/// Whether the window showing the workspace can be seen: no polling while it is hidden, covered
/// or in the Dock.
@MainActor
@Observable
final class WindowPresence {
    var visible: Bool
    init(visible: Bool = true) { self.visible = visible }
}

/// The Stats window's Local models page: holds the workspace's state for as long as it shows.
struct LocalModelsPage: View {
    let model: AppModel
    let presence: WindowPresence
    var preset: LocalModelsWorkspaceState?
    /// The section the window's tab bar shows.
    let section: LocalSection
    /// A section chosen inside the workspace ("Settings" in a recovery state): the tab bar follows.
    let choose: (LocalSection) -> Void
    @State private var state: LocalModelsWorkspaceState?

    var body: some View {
        Group {
            if let ws = preset ?? state {
                LocalModelsWorkspace(ws: ws, presence: presence)
                    .onChange(of: ws.section) { _, new in if new != section { choose(new) } }
                    .onChange(of: section, initial: true) { _, new in if ws.section != new { ws.section = new } }
            } else {
                Color.clear
            }
        }
        .onAppear {
            if preset == nil, state == nil { state = LocalModelsWorkspaceState(section: section, savesSection: false) }
            (preset ?? state)?.app = model
        }
    }
}

struct LocalModelsWorkspace: View {
    @Bindable var ws: LocalModelsWorkspaceState
    let presence: WindowPresence
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        let palette = StatsPalette(scheme)
        // The window's tab bar chooses the section; no second navigation here.
        content(palette)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            // Closing the window ends a Try request in flight: nothing keeps running unseen.
            .onDisappear { ws.tryout.cancel() }
        // At least the Stats card's size; it takes whatever the window gives it beyond that.
        .frame(minWidth: StatsCard.size.width, maxWidth: .infinity, minHeight: StatsCard.size.height, maxHeight: .infinity)
        .background(palette.canvas)
        .foregroundStyle(palette.ink)
        // One poll loop for the section in view, only while the window can be seen.
        .task(id: pollID) { await poll() }
    }

    @ViewBuilder
    private func content(_ palette: StatsPalette) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHeader(section: ws.section, store: ws.store, palette: palette)
            if ws.tryout.isRunning, ws.section != .tryout {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("A Try request to \(ws.tryout.modelName) is running.").font(.system(size: 11.5))
                    Spacer()
                    Button("Show") { ws.section = .tryout }
                    Button("Cancel") { ws.tryout.cancel() }
                }
                .controlSize(.small)
                .padding(.horizontal, 10).padding(.vertical, 5)
                .background(RoundedRectangle(cornerRadius: 7, style: .continuous).fill(palette.faint))
                .padding(.bottom, 10)
            }
            Group {
                switch ws.section {
                case .overview: OverviewSection(ws: ws, palette: palette)
                case .models: ModelsSection(ws: ws, palette: palette)
                case .tryout: TrySection(ws: ws, tryout: ws.tryout, palette: palette)
                case .activity: ActivitySection(ws: ws, palette: palette)
                case .logs: LogsSection(ws: ws, presence: presence, palette: palette)
                case .settings: LMSettingsSection(ws: ws, palette: palette)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
        .padding(.horizontal, 26)
        .padding(.top, 18)
        .padding(.bottom, 20)
    }

    private var pollID: String {
        let s = ws.settings
        let fast = ws.store.pullsUnderway || ws.store.state == .starting || ws.store.models.contains { $0.state == .starting || $0.state == .unloading }
        return "\(ws.section.rawValue)|\(ws.activityModel)|\(ws.activityOutcome)|\(presence.visible)|\(s.pollSeconds)|\(s.address)|\(s.keyFile.url.path)|\(fast)"
    }

    private func poll() async {
        guard presence.visible else { return }
        let store = ws.store
        while !Task.isCancelled {
            switch ws.section {
            case .overview, .models, .tryout: await store.refreshModels()
            case .activity:
                await store.refreshRequests(model: ws.activityModel.isEmpty ? nil : ws.activityModel,
                                            // The gateway knows ok/error/refused/cancelled; "off" is matched here.
                                            outcome: ["all", "not_available", "refused"].contains(ws.activityOutcome) ? nil : ws.activityOutcome)
            // Logs poll in their own loop, only while following; Settings ask once.
            case .logs:
                await store.checkGateway()
                return
            case .settings:
                await store.refreshModels()  // its status carries the login item
                return
            }
            let fast = store.pullsUnderway || store.state == .starting || store.models.contains { $0.state == .starting || $0.state == .unloading }
            try? await Task.sleep(for: .seconds(fast ? 1 : ws.settings.pollSeconds))
        }
    }
}

/// A section's title, what it is for, and how fresh its data is.
private struct SectionHeader: View {
    let section: LocalSection
    let store: LocalModelsStore
    let palette: StatsPalette

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 2) {
                Text(section.title).font(.system(size: 20, weight: .bold))
                Text(section.subtitle).font(.system(size: 12)).foregroundStyle(palette.ink2)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 3) {
                HStack(spacing: 8) {
                    GatewayChip(state: store.state, palette: palette)
                    Text(store.settings.address.description)
                        .font(.system(size: 10.5, design: .monospaced)).foregroundStyle(palette.ink3).textSelection(.enabled)
                }
                if let updated = updated {
                    Text("Read \(LMFormat.clock(updated))")
                        .font(.system(size: 11))
                        .foregroundStyle(palette.ink3)
                        .monospacedDigit()
                }
            }
        }
        .padding(.bottom, 14)
    }

    private var updated: Date? {
        switch section {
        case .overview, .models, .tryout: store.modelsUpdated
        case .activity: store.requestsUpdated
        case .logs: store.logUpdated
        case .settings: nil
        }
    }
}

// MARK: Shared pieces

struct GatewayChip: View {
    let state: GatewayState
    let palette: StatsPalette

    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(color).frame(width: 7, height: 7)
                .background(Circle().fill(color.opacity(0.25)).frame(width: 13, height: 13))
                .frame(width: 13, height: 13)
            Text(state.label).font(.system(size: 11.5, weight: .semibold))
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Gateway: \(state.label)")
    }

    var color: Color {
        switch state {
        case .running: Color(nsColor: .systemGreen)
        case .starting, .keyProblem: Color(nsColor: .systemOrange)
        case .unreachable: Color(nsColor: .systemRed)
        case .notRunning, .unknown: palette.ink3
        }
    }
}

extension ModelInfo {
    /// The state in the owner's words once the gateway has the switch: Off, On · idle, On · loaded.
    var availabilityLabel: String? {
        guard let available else { return nil }
        if !available { return state == .unloading ? "Off · unloading" : "Off" }
        switch state {
        case .loaded: return "On · loaded"
        case .starting: return "On · loading"
        case .unloading: return "On · unloading"
        case .downloaded: return "On · idle"
        case .registered: return "On · not downloaded"
        case .unknown: return "On"
        }
    }

    var availabilityHelp: String {
        switch available {
        case false?: "Off: not served, and it takes no memory. Turn it on to load, pin or try it."
        case true?: state == .loaded ? "On and loaded: it holds its memory budget until it idles out." : "On: it loads on the first request that names it."
        case nil: "This gateway has no on/off switch."
        }
    }
}

struct StateBadge: View {
    let state: ModelState
    let palette: StatsPalette
    /// The model, when its on/off state should be shown instead of the bare state.
    var model: ModelInfo?

    var body: some View {
        HStack(spacing: 5) {
            if model?.isOff == true {
                Circle().strokeBorder(palette.ink3, lineWidth: 1.2).frame(width: 7, height: 7)
            } else if state == .starting || state == .unloading {
                ProgressView().controlSize(.mini).scaleEffect(0.7).frame(width: 10, height: 10)
            } else {
                Circle().fill(color).frame(width: 6, height: 6)
            }
            Text(model?.availabilityLabel ?? state.label).font(.system(size: 11.5, weight: .medium))
        }
        .foregroundStyle(state == .registered || model?.isOff == true ? palette.ink2 : palette.ink)
        .help(model?.availabilityHelp ?? "")
    }

    private var color: Color {
        switch state {
        case .loaded: Color(nsColor: .systemGreen)
        case .downloaded: palette.ink2
        case .registered, .unknown: palette.ink3.opacity(0.6)
        case .starting, .unloading: Color(nsColor: .systemOrange)
        }
    }
}

/// Memory as two layers against a total: what is measured inside what is held.
struct MemoryBar: View {
    let used: Int?
    let held: Int
    let total: Int
    let palette: StatsPalette
    var height: CGFloat = 8

    var body: some View {
        GeometryReader { g in
            let w = g.size.width
            let t = CGFloat(max(total, 1))
            ZStack(alignment: .leading) {
                Capsule().fill(palette.faint)
                Capsule().fill(palette.ink.opacity(0.16)).frame(width: max(held > 0 ? 4 : 0, w * min(CGFloat(held) / t, 1)))
                if let used, used > 0 {
                    Capsule().fill(palette.claude).frame(width: max(4, w * min(CGFloat(used) / t, 1)))
                }
            }
        }
        .frame(height: height)
        .accessibilityElement()
        .accessibilityLabel("Memory: \(used.map(LMFormat.mb) ?? "unknown") measured, \(LMFormat.mb(held)) held, of \(LMFormat.mb(total))")
    }
}

/// A designed empty, error or recovery state: what is true, and what to do about it.
struct LMEmpty<Actions: View>: View {
    let icon: String
    let title: String
    let message: String
    let palette: StatsPalette
    @ViewBuilder var actions: Actions

    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: icon).font(.system(size: 26, weight: .light)).foregroundStyle(palette.ink3)
            Text(title).font(.system(size: 16, weight: .semibold))
            Text(message)
                .font(.system(size: 12.5))
                .foregroundStyle(palette.ink2)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 440)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            HStack(spacing: 8) { actions }.padding(.top, 4)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

extension LMEmpty where Actions == EmptyView {
    init(icon: String, title: String, message: String, palette: StatsPalette) {
        self.init(icon: icon, title: title, message: message, palette: palette) { EmptyView() }
    }
}

/// Shown above a section whose data is older than the latest failed read.
struct LMStaleBanner: View {
    let store: LocalModelsStore
    let updated: Date?
    let palette: StatsPalette

    var body: some View {
        if let updated, store.state != .running, store.state != .unknown {
            HStack(spacing: 8) {
                Image(systemName: "clock.badge.exclamationmark").foregroundStyle(Color(nsColor: .systemOrange))
                Text("Showing what was read at \(LMFormat.clock(updated)). \(store.state == .notRunning ? "The gateway is not running." : store.state.label + ".")")
                    .font(.system(size: 11.5))
                    .foregroundStyle(palette.ink2)
                Spacer()
            }
            .padding(.horizontal, 10)
            .frame(height: 28)
            .background(RoundedRectangle(cornerRadius: 7, style: .continuous).fill(Color(nsColor: .systemOrange).opacity(0.1)))
            .padding(.bottom, 10)
        }
    }
}

/// The recovery for a gateway that isn't usable, shared by every section that needs it running.
struct GatewayRecovery: View {
    let ws: LocalModelsWorkspaceState
    let palette: StatsPalette

    var body: some View {
        let store = ws.store
        switch store.state {
        case .unknown:
            LMEmpty(icon: "antenna.radiowaves.left.and.right", title: "Checking the gateway…",
                    message: "Asking \(ws.settings.address)/health.", palette: palette)
        case .starting:
            LMEmpty(icon: "hourglass", title: "Starting the gateway…",
                    message: "tinybar-models serve is starting on \(ws.settings.address). This page updates when it answers.", palette: palette) {
                ProgressView().controlSize(.small)
            }
        case .notRunning:
            LMEmpty(icon: "power", title: "The gateway is not running",
                    message: startExplanation, palette: palette) {
                StartStopButtons(ws: ws, palette: palette)
            }
        case let .unreachable(why):
            LMEmpty(icon: "exclamationmark.triangle", title: "The gateway can't be reached", message: why, palette: palette) {
                Button("Try again") { Task { await store.refreshModels() } }
                Button("Settings") { ws.section = .settings }
            }
        case let .keyProblem(why):
            LMEmpty(icon: "key", title: "The key can't be used", message: why + " The key itself is never shown here.", palette: palette) {
                Button("Reveal key file in Finder") { LMFinder.reveal(ws.settings.keyFile.url) }
                Button("Try again") { Task { await store.refreshModels() } }
                Button("Settings") { ws.section = .settings }
            }
        case .running:
            EmptyView()
        }
    }

    private var startExplanation: String {
        guard let cli = ws.settings.cli else {
            return "Nothing answers at \(ws.settings.address). Set the backend folder in Settings to start it from here."
        }
        return "Nothing answers at \(ws.settings.address). Start runs \(Paths.abbreviatedPath(cli)) serve --port \(ws.settings.address.port), which loads models only when a request names them."
    }
}

struct StartStopButtons: View {
    let ws: LocalModelsWorkspaceState
    let palette: StatsPalette
    @State private var confirmStop = false
    @State private var working = false

    var body: some View {
        let store = ws.store
        HStack(spacing: 8) {
            if isUp(store.state) {
                Button {
                    // Only a gateway this app started, at this address, stops without asking.
                    if ws.store.ownsGateway { stop() } else { confirmStop = true }
                } label: {
                    Label(working ? "Stopping…" : "Stop", systemImage: "stop.fill")
                }
                .disabled(working)
                .help("Asks the gateway at \(ws.settings.address.description) to stop its workers and then itself")
            } else {
                Button {
                    working = true
                    Task { await store.start(); working = false }
                } label: {
                    Label(working ? "Starting…" : "Start", systemImage: "play.fill")
                }
                .keyboardShortcut(.defaultAction)
                .disabled(working || ws.settings.cli == nil)
                if ws.settings.cli == nil {
                    Button("Settings") { ws.section = .settings }
                }
            }
        }
        .controlSize(.regular)
        .alert("Stop a gateway this app didn't start?", isPresented: $confirmStop) {
            Button("Stop it", role: .destructive) { stop() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(ws.store.status?.startedByLaunchd == true
                 ? "The gateway at \(ws.settings.address.description) was started at login. Stop asks it to stop its workers and then itself; it stays stopped until you Start it again or log in again."
                 : "The gateway at \(ws.settings.address.description) was started elsewhere. Stop asks it, over its own API, to stop its workers and then itself. Nothing is killed.")
        }
    }

    private func stop() {
        working = true
        Task { await ws.store.stop(); working = false }
    }

    /// Something answers (a key problem included): Stop, not Start.
    private func isUp(_ s: GatewayState) -> Bool {
        switch s {
        case .running, .starting, .keyProblem: true
        default: false
        }
    }
}

enum LMFinder {
    static func reveal(_ url: URL) {
        if FileManager.default.fileExists(atPath: url.path) {
            NSWorkspace.shared.activateFileViewerSelecting([url])
        } else {
            NSWorkspace.shared.open(url.deletingLastPathComponent())
        }
    }
}

extension Paths {
    static func abbreviatedPath(_ url: URL) -> String { abbreviated(url) }
}

enum LMFormat {
    static func mb(_ mb: Int) -> String {
        mb >= 1024 ? String(format: "%.1f GB", Double(mb) / 1024) : "\(mb) MB"
    }

    static func mb(_ mb: Int?) -> String { mb.map { self.mb($0) } ?? "—" }

    static func bytes(_ b: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: b, countStyle: .file)
    }

    static func seconds(_ s: Double) -> String {
        let total = Int(s.rounded())
        if total >= 3600 { return "\(total / 3600) h \(total % 3600 / 60) m" }
        if total >= 60 { return "\(total / 60) m \(total % 60) s" }
        return "\(total) s"
    }

    static func ms(_ ms: Int?) -> String {
        guard let ms else { return "—" }
        return ms >= 10_000 ? String(format: "%.1f s", Double(ms) / 1000) : "\(ms.formatted()) ms"
    }

    static func tokens(_ n: Int?) -> String { n.map { $0.formatted() } ?? "unknown" }

    private static let clockFormat: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "HH:mm:ss"
        return f
    }()

    static func clock(_ d: Date) -> String { clockFormat.string(from: d) }

    /// A clock time today; the day too otherwise.
    static func when(_ d: Date) -> String {
        Calendar.current.isDateInToday(d) ? clock(d) : d.formatted(.dateTime.month(.abbreviated).day().hour(.twoDigits(amPM: .omitted)).minute())
    }

    /// A setting's length in words: "10 min", "45 s", "1 h 30 min".
    static func span(_ s: Double) -> String {
        let t = Int(s.rounded())
        if t >= 3600 { return t % 3600 == 0 ? "\(t / 3600) h" : "\(t / 3600) h \(t % 3600 / 60) min" }
        if t >= 60 { return t % 60 == 0 ? "\(t / 60) min" : "\(t / 60) min \(t % 60) s" }
        return "\(t) s"
    }

    private static let isoFormat: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ssZ"
        return f
    }()

    /// The gateway's `2026-11-17T13:59:40+0530` as a date and time; as given when it doesn't parse.
    static func isoDate(_ text: String) -> String {
        isoFormat.date(from: text).map { $0.formatted(date: .abbreviated, time: .shortened) } ?? text
    }

    /// "42 s", "11 m", "2 h 5 m": short enough for a table cell.
    static func ago(_ s: Double) -> String {
        let t = Int(s.rounded())
        if t >= 3600 { return "\(t / 3600) h \(t % 3600 / 60) m" }
        if t >= 60 { return "\(t / 60) m" }
        return "\(t) s"
    }

    static func statusColor(_ status: Int?) -> Color {
        guard let status else { return .secondary }
        switch status {
        case 200..<300: return Color(nsColor: .systemGreen)
        case 499: return .secondary
        case 400..<500: return Color(nsColor: .systemOrange)
        default: return Color(nsColor: .systemRed)
        }
    }
}
