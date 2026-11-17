import AppKit
import LocalModelCore
import SwiftUI
import TinybarCore

/// The menu-bar popover (ADR 0017): the gateway, its memory, its models with their switches, and
/// what was run last. Built when it opens and dropped when it closes; on open it reads `/health`,
/// `/api/status`, `/api/models` and `/api/requests?limit=5`, then every 3 s while it stays open.
struct ModelbarPopover: View {
    let app: AppModel
    let monitor: GatewayMonitor
    /// Snapshots hand in a prepared state; the popover otherwise builds its own.
    var preset: LocalModelsWorkspaceState?
    var refreshes = true
    @State private var state: LocalModelsWorkspaceState?

    static let width: CGFloat = 344

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let ws = preset ?? state {
                Content(ws: ws, app: app)
            } else {
                ProgressView().frame(maxWidth: .infinity, minHeight: 80)
            }
        }
        .padding(14)
        .frame(width: Self.width)
        .task {
            let ws = preset ?? {
                let s = LocalModelsWorkspaceState(section: .overview, savesSection: false)
                state = s
                return s
            }()
            ws.app = app
            guard refreshes else { return }
            while !Task.isCancelled {
                await ws.store.refreshForMenu()
                monitor.observe(ws.store)
                try? await Task.sleep(for: .seconds(3))
            }
        }
    }

    private struct Content: View {
        let ws: LocalModelsWorkspaceState
        let app: AppModel
        @Environment(\.colorScheme) private var scheme

        var body: some View {
            let palette = StatsPalette(scheme)
            let store = ws.store
            header(store, palette)
            switch store.state {
            case .running:
                if let memory = store.memory {
                    memoryBar(memory, palette)
                }
                models(store, palette)
                recent(store, palette)
            case .unknown:
                note("antenna.radiowaves.left.and.right", "Checking the gateway…", nil, palette)
            case .starting:
                note("hourglass", "Starting the gateway…", "This updates when it answers.", palette)
            case .notRunning:
                note("power", "The gateway is not running", ws.store.status?.startedByLaunchd == true || ws.store.status?.loginItem == "installed"
                     ? "It starts at login. Start runs tinybar-models start." : "Nothing answers at \(ws.settings.address). Models load only when asked.", palette)
            case let .unreachable(why):
                note("exclamationmark.triangle", "The gateway can't be reached", why, palette)
            case let .keyProblem(why):
                note("key", "The key can't be used", why, palette)
                Button("Reveal key file in Finder") { LMFinder.reveal(ws.settings.keyFile.url) }.controlSize(.small)
            }
            footer(palette)
        }

        private func header(_ store: LocalModelsStore, _ palette: StatsPalette) -> some View {
            HStack(alignment: .center, spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text(ForkIdentity.appName).font(.system(size: 14, weight: .semibold))
                        GatewayChip(state: store.state, palette: palette)
                    }
                    Text(ws.settings.address.description + (store.status?.startedByLaunchd == true ? " · started at login" : ""))
                        .font(.system(size: 10.5, design: .monospaced)).foregroundStyle(.secondary)
                }
                Spacer()
                StartStopButtons(ws: ws, palette: palette).controlSize(.small)
            }
        }

        private func memoryBar(_ m: MemoryTotals, _ palette: StatsPalette) -> some View {
            VStack(alignment: .leading, spacing: 4) {
                MemoryBar(used: m.footprintMb, held: m.reservedMb, total: m.budgetTotalMb, palette: palette, height: 7)
                Text("\(LMFormat.mb(m.footprintMb)) measured · \(LMFormat.mb(m.reservedMb)) held · \(LMFormat.mb(m.budgetTotalMb)) budget")
                    .font(.system(size: 10.5)).monospacedDigit().foregroundStyle(.secondary)
            }
        }

        @ViewBuilder
        private func models(_ store: LocalModelsStore, _ palette: StatsPalette) -> some View {
            VStack(alignment: .leading, spacing: 2) {
                HStack {
                    Text("Models").font(.system(size: 12, weight: .semibold))
                    if let n = store.availableCount { Text("\(n) of \(store.models.count) on").font(.system(size: 11)).foregroundStyle(.secondary) }
                    Spacer()
                }
                if store.models.isEmpty {
                    Text("No models in the registry.").font(.system(size: 11.5)).foregroundStyle(.secondary)
                } else if store.availableCount == 0 {
                    Text("Every model is off: none can be served or take memory. Turn one on below.")
                        .font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                ForEach(store.models) { m in
                    MenuModelRow(model: m, store: store, palette: palette)
                }
            }
            .padding(8)
            .platter(cornerRadius: 12)
        }

        @ViewBuilder
        private func recent(_ store: LocalModelsStore, _ palette: StatsPalette) -> some View {
            VStack(alignment: .leading, spacing: 3) {
                HStack {
                    Text("Recent").font(.system(size: 12, weight: .semibold))
                    Spacer()
                    Button("Activity") { app.openLocalModels(.activity) }
                        .buttonStyle(.plain).font(.system(size: 11, weight: .medium)).foregroundStyle(Color.accentColor)
                }
                if store.requests.isEmpty {
                    Text("Nothing run yet.").font(.system(size: 11)).foregroundStyle(.secondary)
                }
                ForEach(store.requests.sorted { ($0.isInFlight ? 1 : 0, $0.startedAt) > ($1.isInFlight ? 1 : 0, $1.startedAt) }.prefix(5)) { r in
                    HStack(spacing: 6) {
                        Text(LMFormat.when(r.started)).frame(width: 52, alignment: .leading)
                        Text(r.client ?? "—").lineLimit(1).frame(width: 74, alignment: .leading).foregroundStyle(.secondary)
                        Text(r.model ?? r.routeName).lineLimit(1).frame(maxWidth: .infinity, alignment: .leading)
                        Text(r.isInFlight ? "…" : r.outcome == "interrupted" || r.outcome == "partial" || r.isNotAvailable ? r.outcomeLabel : r.status.map(String.init) ?? "—")
                            .fontWeight(.semibold).foregroundStyle(LMFormat.statusColor(r.status)).lineLimit(1).frame(width: 30, alignment: .trailing)
                        Text(r.isInFlight ? (r.phase ?? "running") : LMFormat.ms(r.durationMs)).frame(width: 58, alignment: .trailing).foregroundStyle(.secondary)
                    }
                    .font(.system(size: 11)).monospacedDigit()
                    .accessibilityElement(children: .combine)
                }
            }
        }

        private func note(_ icon: String, _ title: String, _ detail: String?, _ palette: StatsPalette) -> some View {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: icon).font(.system(size: 18, weight: .light)).foregroundStyle(.secondary).frame(width: 24)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.system(size: 13, weight: .semibold))
                    if let detail { Text(detail).font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true) }
                }
            }
            .padding(.vertical, 6)
        }

        private func footer(_ palette: StatsPalette) -> some View {
            HStack(spacing: 8) {
                Button { app.openLocalModels(.overview) } label: { Label("Open \(ForkIdentity.appName)", systemImage: "macwindow") }
                    .keyboardShortcut("o")
                Spacer()
                Button { app.openLocalModels(.settings) } label: { Image(systemName: "gearshape") }
                    .help("Settings").accessibilityLabel("Settings").keyboardShortcut(",")
                Button { NSApp.terminate(nil) } label: { Image(systemName: "power") }
                    .help("Quit \(ForkIdentity.appName)").accessibilityLabel("Quit \(ForkIdentity.appName)").keyboardShortcut("q")
            }
            .buttonStyle(.borderless)
            .controlSize(.small)
        }
    }
}

/// One model in the popover: name, switch, state, memory, idle time left, Load or Unload.
private struct MenuModelRow: View {
    let model: ModelInfo
    let store: LocalModelsStore
    let palette: StatsPalette

    var body: some View {
        let m = model
        HStack(spacing: 6) {
            VStack(alignment: .leading, spacing: 0) {
                Text(m.name).font(.system(size: 12, weight: .medium)).lineLimit(1)
                    .foregroundStyle(m.isOff ? .secondary : .primary)
                Text(detail(m)).font(.system(size: 10.5)).foregroundStyle(.secondary).lineLimit(1).monospacedDigit()
            }
            .help(m.availabilityHelp)
            Spacer(minLength: 4)
            if let doing = store.busy[m.name] {
                ProgressView().controlSize(.mini).help(doing)
            } else if !m.isOff, m.state == .loaded {
                Button("Unload") { Task { await store.unload(m.name) } }.controlSize(.mini)
            } else if !m.isOff, m.state == .downloaded {
                Button("Load") { Task { await store.load(m.name) } }.controlSize(.mini)
            }
            if let on = m.available {
                Toggle("\(m.name) available", isOn: Binding(get: { on }, set: { new in Task { await store.setAvailable(m.name, new) } }))
                    .toggleStyle(.switch).controlSize(.mini).labelsHidden()
                    .disabled(store.busy[m.name] != nil)
                    .help(on ? "On: served, loads on demand. Switch off to free its memory." : "Off: switch on to serve it")
            }
        }
        .frame(minHeight: 30)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(m.name), \(m.availabilityLabel ?? m.state.label)")
    }

    private func detail(_ m: ModelInfo) -> String {
        var parts = [m.availabilityLabel ?? m.state.label]
        if let f = m.footprintMb { parts.append(LMFormat.mb(f)) }
        if let left = m.idleSecondsLeft { parts.append("idle \(LMFormat.ago(left)) left") } else if m.pinned, m.state == .loaded { parts.append("pinned") }
        if m.inFlight > 0 { parts.append("\(m.inFlight) running") }
        return parts.joined(separator: " · ")
    }
}
