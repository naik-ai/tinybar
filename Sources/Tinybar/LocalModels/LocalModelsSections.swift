import AppKit
import LocalModelCore
import SwiftUI
import TinybarCore
import UniformTypeIdentifiers

// MARK: Overview

struct OverviewSection: View {
    let ws: LocalModelsWorkspaceState
    let palette: StatsPalette

    var body: some View {
        let store = ws.store
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .center, spacing: 14) {
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 10) {
                        GatewayChip(state: store.state, palette: palette)
                        if store.ownsGateway, let pid = store.launcher.record?.pid {
                            Text("started by this app, pid \(String(pid))").font(.system(size: 11)).foregroundStyle(palette.ink3)
                        }
                    }
                    Text(ws.settings.address.description)
                        .font(.system(size: 22, weight: .bold).width(.compressed))
                        .textSelection(.enabled)
                }
                Spacer()
                // The recovery below offers Start itself when nothing answers.
                if store.state != .notRunning, store.state != .unknown {
                    StartStopButtons(ws: ws, palette: palette)
                }
            }
            .padding(.bottom, 16)

            if store.state == .running || (store.modelsUpdated != nil && store.memory != nil) {
                LMStaleBanner(store: store, updated: store.modelsUpdated, palette: palette)
                running(store)
            } else {
                GatewayRecovery(ws: ws, palette: palette)
            }
        }
    }

    @ViewBuilder
    private func running(_ store: LocalModelsStore) -> some View {
        if let memory = store.memory {
            VStack(alignment: .leading, spacing: 18) {
                VStack(alignment: .leading, spacing: 7) {
                    HStack(alignment: .firstTextBaseline) {
                        SectionTitle(text: "Memory", palette: palette)
                        Spacer()
                        Text("\(LMFormat.mb(memory.footprintMb)) measured · \(LMFormat.mb(memory.reservedMb)) held of \(LMFormat.mb(memory.budgetTotalMb))")
                            .font(.system(size: 12, weight: .medium)).monospacedDigit()
                    }
                    MemoryBar(used: memory.footprintMb, held: memory.reservedMb, total: memory.budgetTotalMb, palette: palette, height: 10)
                    HStack(spacing: 14) {
                        legend(palette.claude, "Measured footprint of the loaded workers")
                        legend(palette.ink.opacity(0.16), "Budgets they hold")
                        legend(palette.faint, "Free in the total budget")
                    }
                    .font(.system(size: 10.5))
                    .foregroundStyle(palette.ink2)
                    if store.availableCount != nil {
                        Text("Only models that are turned on can take memory; a model that is off is never loaded.")
                            .font(.system(size: 10.5)).foregroundStyle(palette.ink2)
                    }
                }
                HStack(alignment: .top, spacing: 0) {
                    if let n = store.availableCount {
                        fact("\(n) of \(store.models.count)", "Available", "turned on")
                    }
                    fact("\(memory.loadedModels)", "Loaded", memory.startingModels.map { $0 > 0 ? "\($0) loading" : "of \(store.models.count) in the registry" } ?? "")
                    fact("\(store.inFlight)", "In flight", store.waiting > 0 ? "\(store.waiting) waiting" : "none waiting")
                    fact(memory.evictions.map(String.init) ?? "—", "Evictions", "to make room")
                    fact(memory.restarts.map(String.init) ?? "—", "Restarts", memory.abandoned.map { "\($0) abandoned" } ?? "")
                    fact(store.status.map { $0.ledgerRows.formatted() } ?? "—", "Ledger rows",
                         store.status.map { refusals($0.refusals) } ?? "")
                }
                if let s = store.status, s.version != nil || s.uptimeSeconds != nil {
                    Text([s.version.map { "tinybar-models \($0)" }, s.uptimeSeconds.map { "up \(LMFormat.seconds($0))" },
                          s.port.map { "port \($0)" }, s.storeFreeGb.map { String(format: "%.0f GB free in the weights store", $0) },
                          s.registryPath.map { "registry \($0)" }].compactMap { $0 }.joined(separator: " · "))
                        .font(.system(size: 11)).foregroundStyle(palette.ink2).textSelection(.enabled)
                }
                VStack(alignment: .leading, spacing: 4) {
                    SectionTitle(text: "Loaded models", palette: palette)
                    if store.loaded.isEmpty {
                        Text("None. A model loads when a request names it, or from Models.")
                            .font(.system(size: 12)).foregroundStyle(palette.ink2).padding(.vertical, 6)
                    }
                    ForEach(store.loaded) { m in
                        HStack(spacing: 10) {
                            StateBadge(state: m.state, palette: palette).frame(width: 92, alignment: .leading)
                            Text(m.displayName).font(.system(size: 12.5, weight: .semibold)).lineLimit(1)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .help("\(m.displayName) (\(m.name))")
                            MemoryBar(used: m.footprintMb, held: m.budgetMb, total: m.budgetMb, palette: palette, height: 5).frame(width: 90)
                            Text("\(LMFormat.mb(m.footprintMb)) of \(LMFormat.mb(m.budgetMb))").frame(width: 110, alignment: .trailing)
                            Text(idle(m)).frame(width: 120, alignment: .trailing).foregroundStyle(palette.ink2)
                        }
                        .font(.system(size: 11.5)).monospacedDigit()
                        .frame(height: 22)
                    }
                }
                Spacer(minLength: 0)
                lastError(store)
            }
        } else {
            LMEmpty(icon: "hourglass", title: "Reading the gateway…", message: "Asking for its models and memory.", palette: palette)
        }
    }

    private func idle(_ m: ModelInfo) -> String {
        if m.pinned { return "pinned" }
        if m.inFlight > 0 { return "\(m.inFlight) in flight" }
        return m.idleSecondsLeft.map { "unloads in \(LMFormat.seconds($0))" } ?? "—"
    }

    private func refusals(_ r: [String: Int]) -> String {
        let n = r.values.reduce(0, +)
        return n == 0 ? "no refusals" : "\(n) refused at the door"
    }

    @ViewBuilder
    private func lastError(_ store: LocalModelsStore) -> some View {
        HStack(spacing: 6) {
            Image(systemName: store.lastError == nil ? "checkmark.circle" : "exclamationmark.circle")
                .foregroundStyle(store.lastError == nil ? palette.ink3 : Color(nsColor: .systemOrange))
            if let e = store.lastError {
                Text("Last error at \(LMFormat.clock(e.at)): \(e.message)").lineLimit(2).textSelection(.enabled)
            } else {
                Text("No errors since this window opened.")
            }
        }
        .font(.system(size: 11.5))
        .foregroundStyle(palette.ink2)
    }

    private func legend(_ color: Color, _ label: String) -> some View {
        HStack(spacing: 5) {
            Capsule().fill(color).frame(width: 12, height: 6)
            Text(label)
        }
    }

    private func fact(_ value: String, _ label: String, _ detail: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value).font(.system(size: 24, weight: .bold).width(.compressed)).monospacedDigit()
            Text(label).font(.system(size: 11.5, weight: .semibold))
            Text(detail).font(.system(size: 10.5)).foregroundStyle(palette.ink2).lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: Models

struct ModelsSection: View {
    @Bindable var ws: LocalModelsWorkspaceState
    let palette: StatsPalette

    var body: some View {
        let store = ws.store
        VStack(alignment: .leading, spacing: 0) {
            if store.models.isEmpty {
                if store.state == .running {
                    LMEmpty(icon: "shippingbox", title: "Reading the registry…", message: "Asking the gateway for its models.", palette: palette)
                } else {
                    GatewayRecovery(ws: ws, palette: palette)
                }
            } else {
                LMStaleBanner(store: store, updated: store.modelsUpdated, palette: palette)
                // An inspector beside the table: the table narrows (and drops its least needed
                // columns when that leaves too little room) rather than being covered.
                GeometryReader { g in
                    let open = ws.details.flatMap { store.model($0) }
                    let compact = open != nil && g.size.width - 346 < 700
                    HStack(alignment: .top, spacing: 16) {
                        VStack(alignment: .leading, spacing: 0) {
                            header(compact: compact)
                            ScrollView {
                                VStack(spacing: 0) {
                                    ForEach(store.models) { m in
                                        ModelRowView(model: m, ws: ws, palette: palette, compact: compact)
                                        Rectangle().fill(palette.faint).frame(height: 1)
                                    }
                                }
                            }
                            Text("Open a model (its row, or ⓘ) for its weights, licence and limits. Load, Unload, Pin and Pull ask the gateway; nothing loads by itself.")
                                .font(.system(size: 10.5)).foregroundStyle(palette.ink3).padding(.top, 8)
                        }
                        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                        if let m = open {
                            ModelDetails(model: m, registry: store.registry?.entry(m.name), registryError: store.registryError, palette: palette) {
                                ws.details = nil
                            }
                            .transition(.move(edge: .trailing).combined(with: .opacity))
                        }
                    }
                }
            }
        }
        .animation(.smooth(duration: 0.25), value: ws.details)
        .onExitCommand { ws.details = nil }
    }

    private func header(compact: Bool) -> some View {
        HStack(spacing: 8) {
            Text("Model").frame(maxWidth: .infinity, alignment: .leading)
            Text("On").frame(width: 40, alignment: .leading)
            Text("State").frame(width: 112, alignment: .leading)
            if !compact {
                Text("Memory of budget").frame(width: 150, alignment: .leading)
                Text("Last used").frame(width: 58, alignment: .trailing)
                Text("Idle left").frame(width: 58, alignment: .trailing)
            }
            Text("").frame(width: 24)
            Text("").frame(width: 20)
            Text("").frame(width: 92)
        }
        .font(.system(size: 10.5, weight: .medium))
        .foregroundStyle(palette.ink3)
        .padding(.bottom, 4)
    }
}

private struct ModelRowView: View {
    let model: ModelInfo
    let ws: LocalModelsWorkspaceState
    let palette: StatsPalette
    var compact = false
    @State private var hovering = false
    @State private var confirmOff = false

    var body: some View {
        let m = model
        let store = ws.store
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                VStack(alignment: .leading, spacing: 1) {
                    Text(m.displayName).font(.system(size: 12.5, weight: .semibold)).lineLimit(1)
                    // Work kinds wrap rather than vanish into an ellipsis.
                    Text("\(m.name) · \(m.work.joined(separator: ", "))").font(.system(size: 10.5)).foregroundStyle(palette.ink2)
                        .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .help("\(m.displayName)\n\(m.name) · \(m.work.joined(separator: ", "))")
                .opacity(m.isOff ? 0.55 : 1)
                availableSwitch(m, store).frame(width: 40, alignment: .leading)
                StateBadge(state: m.state, palette: palette, model: m).frame(width: 112, alignment: .leading)
                if !compact {
                HStack(spacing: 6) {
                    MemoryBar(used: m.footprintMb, held: m.state == .registered || m.state == .downloaded ? 0 : m.budgetMb, total: m.budgetMb, palette: palette, height: 5)
                        .frame(width: 34)
                    Text("\(m.footprintMb.map(LMFormat.mb) ?? "—") / \(LMFormat.mb(m.budgetMb))").lineLimit(1).fixedSize()
                }
                .frame(width: 150, alignment: .leading)
                Text(m.lastUsedSecondsAgo.map { LMFormat.ago($0) + " ago" } ?? "—").frame(width: 58, alignment: .trailing)
                Text(m.pinned ? "pinned" : m.idleSecondsLeft.map(LMFormat.ago) ?? "—").frame(width: 58, alignment: .trailing)
                }
                Button { Task { await store.pin(m.name, !m.pinned) } } label: {
                    Image(systemName: m.pinned ? "pin.fill" : "pin").foregroundStyle(m.pinned ? palette.claude : palette.ink3)
                }
                .buttonStyle(.plain)
                .frame(width: 24)
                .disabled(store.busy[m.name] != nil || store.state != .running || m.isOff)
                .help(m.isOff ? "Off: turn it on to pin it" : m.pinned ? "Pinned: never unloaded for idleness or room. Click to unpin." : "Pin: keep it loaded once loaded")
                .accessibilityLabel(m.pinned ? "Unpin \(m.displayName)" : "Pin \(m.displayName)")
                // A real button, so the details open from the keyboard and VoiceOver too.
                Button { ws.details = ws.details == m.name ? nil : m.name } label: {
                    Image(systemName: ws.details == m.name ? "info.circle.fill" : "info.circle").foregroundStyle(palette.ink2)
                }
                .buttonStyle(.plain)
                .frame(width: 20)
                .help("Details")
                .accessibilityLabel(ws.details == m.name ? "Close details for \(m.displayName)" : "Details for \(m.displayName)")
                actions(m, store).frame(width: 92, alignment: .trailing)
            }
            .font(.system(size: 11.5)).monospacedDigit()
            if let pull = m.pull, !(pull.ok == true && m.state != .registered && pull.isFinished && Date().timeIntervalSince1970 - (pull.finishedAt ?? 0) > 60) {
                PullLine(pull: pull, palette: palette)
            }
            if m.isOff {
                Text("Off: not served, takes no memory. Load, Pin and Try need it on.")
                    .font(.system(size: 11)).foregroundStyle(palette.ink2)
            }
            if let e = m.lastError {
                Text("Last error\(e.kind.map { " (\($0))" } ?? ""): \(e.message)")
                    .font(.system(size: 11)).foregroundStyle(Color(nsColor: .systemOrange)).textSelection(.enabled).lineLimit(2)
            }
            if let error = store.actionErrors[m.name] {
                Text(error).font(.system(size: 11)).foregroundStyle(Color(nsColor: .systemRed)).textSelection(.enabled).lineLimit(3)
            }
        }
        .padding(.vertical, 7)
        .padding(.horizontal, 6)
        .background {
            RoundedRectangle(cornerRadius: 7, style: .continuous)
                .fill(palette.ink.opacity(ws.details == m.name ? 0.07 : hovering ? 0.035 : 0))
        }
        .contentShape(Rectangle())
        .onTapGesture { ws.details = ws.details == m.name ? nil : m.name }
        .onHover { hovering = $0 }
        .padding(.horizontal, -6)
    }

    @ViewBuilder
    private func availableSwitch(_ m: ModelInfo, _ store: LocalModelsStore) -> some View {
        if let on = m.available {
            Toggle("Available", isOn: Binding(get: { on }, set: { new in
                if !new, m.inFlight + m.waiting > 0 { confirmOff = true } else { Task { await store.setAvailable(m.name, new) } }
            }))
            .toggleStyle(.switch).controlSize(.mini).labelsHidden()
            .disabled(store.busy[m.name] != nil || store.state != .running)
            .help(on ? "On: served, loads on demand. Switch off to stop serving it and free its memory." : "Off: switch on to serve it")
            .accessibilityLabel("\(m.displayName) available")
            .alert("Turn \(m.displayName) off now?", isPresented: $confirmOff) {
                Button("Turn off", role: .destructive) { Task { await store.setAvailable(m.name, false) } }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("\(m.inFlight + m.waiting) request(s) are using it. They finish first; then it unloads and takes no memory.")
            }
        } else {
            Color.clear.frame(width: 1)
        }
    }

    @ViewBuilder
    private func actions(_ m: ModelInfo, _ store: LocalModelsStore) -> some View {
        if let doing = store.busy[m.name] {
            HStack(spacing: 5) {
                ProgressView().controlSize(.mini)
                Text(doing + "…").font(.system(size: 11)).foregroundStyle(palette.ink2).lineLimit(1)
            }
        } else {
            let running = store.state == .running
            switch m.state {
            case _ where m.pull?.isCancelling == true:
                Text("Cancelling…").font(.system(size: 11)).foregroundStyle(palette.ink2)
                    .help("The pull is stopping; a new pull waits until it has")
            case _ where m.pull.map({ !$0.isFinished }) ?? false:
                Button("Cancel pull") { Task { await store.cancelPull(m.name) } }.disabled(!running)
                    .help("Stop this pull; nothing partial is recorded")
            case .registered:
                Button("Pull") { Task { await store.pull(m.name) } }.disabled(!running)
                    .help("Verify or download the pinned weights into the store")
            case .downloaded:
                Button("Load") { Task { await store.load(m.name) } }.disabled(!running || m.isOff)
                    .help(m.isOff ? "Off: turn it on to load it" : "Start its worker now (waits until ready)")
            case .loaded:
                Button("Unload") { Task { await store.unload(m.name) } }.disabled(!running)
                    .help("Stop its worker once its requests finish")
            case .starting, .unloading, .unknown:
                EmptyView()
            }
        }
    }
}

private struct PullLine: View {
    let pull: PullProgress
    let palette: StatsPalette

    var body: some View {
        HStack(spacing: 8) {
            if pull.phase == "cancelled" {
                Image(systemName: "xmark.circle").foregroundStyle(palette.ink2)
                Text("Pull cancelled\(pull.message.map { ": \($0)" } ?? "")").foregroundStyle(palette.ink2).lineLimit(1)
            } else if pull.phase == "failed" || pull.ok == false {
                Image(systemName: "xmark.octagon.fill").foregroundStyle(Color(nsColor: .systemRed))
                Text("Pull failed: \(pull.message ?? "no reason given")").foregroundStyle(Color(nsColor: .systemRed)).lineLimit(2).textSelection(.enabled)
            } else if pull.isFinished {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(Color(nsColor: .systemGreen))
                Text(pull.message ?? "Pull done").foregroundStyle(palette.ink2).lineLimit(1)
            } else {
                Text("Pull: \(pull.phase)\(pull.currentFile.map { " \($0)" } ?? "")").foregroundStyle(palette.ink2).lineLimit(1).frame(width: 200, alignment: .leading)
                if let f = pull.fraction {
                    ProgressView(value: f).frame(maxWidth: 220)
                    Text("\(LMFormat.bytes(pull.bytesDone)) of \(LMFormat.bytes(pull.bytesTotal))").foregroundStyle(palette.ink2)
                } else {
                    ProgressView().controlSize(.mini)
                }
            }
            Spacer()
        }
        .font(.system(size: 11)).monospacedDigit()
    }
}

private struct ModelDetails: View {
    let model: ModelInfo
    let registry: RegistryFile.Entry?
    let registryError: String?
    let palette: StatsPalette
    let close: () -> Void

    var body: some View {
        let m = model
        let source = registry?.source(m.weights.source)
        let licence = registry?.licence[m.weights.source]
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(m.displayName).font(.system(size: 14, weight: .semibold))
                    Text(m.name).font(.system(size: 11, design: .monospaced)).foregroundStyle(palette.ink2)
                }
                Spacer()
                Button(action: close) { Image(systemName: "xmark.circle.fill").foregroundStyle(palette.ink3) }
                    .buttonStyle(.plain).keyboardShortcut(.cancelAction).help("Close (Esc)")
                    .accessibilityLabel("Close details")
            }
            .padding(.bottom, 10)
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    let w = m.weights
                    group("Weights") {
                        row("Source", w.source + (w.download.map { $0 == w.source ? "" : " (a pull downloads \($0))" } ?? ""))
                        row("Repository", w.repo, mono: true)
                        row("Pinned revision", w.revision, mono: true)
                        row("Folder", w.path ?? "not downloaded", mono: w.path != nil)
                        if let size = w.approxSizeGb {
                            row("Size", String(format: "about %.1f GB", size))
                        } else if let size = registry?.weights.approxSizeGb?[w.source] {
                            row("Size", String(format: "about %.1f GB", size), registry: true)
                        }
                        if let alt = w.alternative {
                            row("Alternative", "\(alt.source): \(alt.repo)\(alt.commercialUse.map { ", use \($0)" } ?? "")", mono: false)
                        }
                    }
                    group("Checksum") {
                        if let pinned = w.sha256Pinned {
                            row("Pinned sha256", pinned ? "yes, in the registry" : "none in the registry")
                        } else {
                            let names = source?.sha256?.keys.sorted() ?? []
                            row("Pinned sha256", names.isEmpty ? (registry == nil ? "unknown" : "none in the registry") : names.joined(separator: ", "), registry: true)
                        }
                        row("Verified", m.state == .registered ? "no: not pulled, or its files changed"
                            : [w.filesVerified.map { "\($0) files checked" }, w.verifiedAt.map { "on \(LMFormat.isoDate($0))" },
                               w.reused.map { $0 ? "an existing copy" : "downloaded" }].compactMap { $0 }.joined(separator: ", ").nonEmpty
                                ?? "yes: a pull verified it file by file")
                    }
                    group("Licence") {
                        if let l = m.licence {
                            row("Licence", l.id ?? "none declared")
                        } else {
                            row("Licence", licence?.id ?? (registry == nil ? "unknown" : "none declared"), registry: true)
                        }
                        row("Commercial use", m.commercialUse + (m.commercialUse == "allowed" ? "" : " (not served)"))
                        if let from = m.licence?.readFrom ?? licence?.readFrom {
                            Text(from).font(.system(size: 10.5)).foregroundStyle(palette.ink2).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                        }
                    }
                    group("Server") {
                        row("Kind", m.server?.kind ?? m.serverKind)
                        if let max = m.server?.maxTokens {
                            row("Max tokens", max.formatted())
                        } else if let max = registry?.server.maxTokens {
                            row("Max tokens", max.formatted(), registry: true)
                        }
                        row("Concurrency", "\(m.concurrency) at once, \(m.queueLimit) may wait")
                        if let t = m.readyTimeoutSeconds { row("Ready timeout", LMFormat.span(Double(t))) }
                        if let caps = m.capabilities {
                            row("Takes images", caps.imageInput == true ? "yes" : "no")
                            row("JSON schema output", caps.jsonSchemaOutput == true ? "yes" : "no")
                            if let out = caps.output { row("Answers", out) }
                        } else if let caps = registry?.capabilities {
                            row("Takes images", caps.imageInput == true ? "yes" : "no", registry: true)
                            row("JSON schema output", caps.jsonSchemaOutput == true ? "yes" : "no", registry: true)
                            if let out = caps.output { row("Answers", out, registry: true) }
                        }
                        if let needs = registry?.needs { row("Needs", needs, registry: true) }
                    }
                    if let on = m.available {
                        group("Availability") {
                            row("Available", on ? "on" : "off: not served, takes no memory")
                            row("Set by", m.availableBy == "user" ? "you" : m.availableBy ?? "—")
                        }
                    }
                    group("Memory and idle") {
                        row("Budget", LMFormat.mb(m.budgetMb))
                        row("Footprint", m.footprintMb.map(LMFormat.mb) ?? "not loaded")
                        row("Idle unload after", m.pinned ? "never (pinned\(m.pinnedBy.map { " by \($0)" } ?? ""))" : LMFormat.span(Double(m.ttlSeconds)))
                        row("Last load took", m.loadSeconds.map { String(format: "%.1f s", $0) } ?? "—")
                        if m.phase == "stopping" { row("Now", "its process is exiting") }
                    }
                    if let e = m.lastError {
                        group("Last error") {
                            row(e.kind ?? "error", e.message)
                            if let at = e.at { row("At", LMFormat.isoDate(at)) }
                        }
                    }
                    Text(registryError.map { "Registry: \($0)" } ?? "Marked ◆: read from registry.json in the backend folder, because this gateway's API doesn't give it.")
                        .font(.system(size: 10.5)).foregroundStyle(palette.ink3).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(16)
        .frame(width: 330)
        .frame(maxHeight: .infinity)
        .background {
            RoundedRectangle(cornerRadius: 12, style: .continuous).fill(palette.raised)
                .shadow(color: .black.opacity(0.18), radius: 16, y: 4)
        }
        .overlay { RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(palette.ink.opacity(0.1), lineWidth: 0.5) }
    }

    private func group<C: View>(_ title: String, @ViewBuilder _ content: () -> C) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.system(size: 11.5, weight: .semibold))
            content()
        }
    }

    private func row(_ label: String, _ value: String, mono: Bool = false, registry: Bool = false) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(label + (registry ? " ◆" : "")).foregroundStyle(palette.ink2).frame(width: 112, alignment: .leading)
            Text(value).font(mono ? .system(size: 10.5, design: .monospaced) : .system(size: 11.5))
                .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
        }
        .font(.system(size: 11.5))
    }
}

// MARK: Activity

struct ActivitySection: View {
    @Bindable var ws: LocalModelsWorkspaceState
    let palette: StatsPalette
    private var model: String { ws.activityModel }
    private var outcome: String { ws.activityOutcome }

    var body: some View {
        let store = ws.store
        // Requests still running come first, then the rest, newest first.
        let rows = store.requests.filter { (model.isEmpty || $0.model == model) && matches($0) }
            .sorted { ($0.isInFlight ? 1 : 0, $0.startedAt) > ($1.isInFlight ? 1 : 0, $1.startedAt) }
        VStack(alignment: .leading, spacing: 0) {
            if store.requests.isEmpty, store.requestsUpdated == nil {
                if store.state == .running || store.state == .unknown {
                    LMEmpty(icon: "list.bullet.rectangle", title: "Reading the ledger…", message: "Asking for the latest requests.", palette: palette)
                } else {
                    GatewayRecovery(ws: ws, palette: palette)
                }
            } else {
                LMStaleBanner(store: store, updated: store.requestsUpdated, palette: palette)
                HStack(spacing: 10) {
                    Picker("Model", selection: $ws.activityModel) {
                        Text("All models").tag("")
                        ForEach(modelNames(store), id: \.self) { Text($0).tag($0) }
                    }
                    .frame(width: 200)
                    Picker("Outcome", selection: $ws.activityOutcome) {
                        Text("All outcomes").tag("all")
                        Text("OK").tag("ok")
                        Text("Refused").tag("refused")
                        Text("Errors").tag("error")
                        Text("Cancelled").tag("cancelled")
                        Text("Not available (off)").tag("not_available")
                        Text("In flight").tag("in_flight")
                        Text("Interrupted").tag("interrupted")
                        Text("Partial").tag("partial")
                    }
                    .frame(width: 170)
                    Spacer()
                    Text("\(rows.count) of \(store.requests.count) · every \(ws.settings.pollSeconds) s while shown")
                        .font(.system(size: 11)).foregroundStyle(palette.ink3)
                }
                .labelsHidden()
                .controlSize(.small)
                .padding(.bottom, 10)
                header
                if rows.isEmpty {
                    LMEmpty(icon: "line.3.horizontal.decrease.circle", title: store.requests.isEmpty ? "No requests yet" : "No requests match",
                            message: store.requests.isEmpty ? "Requests through the gateway show here, with timings and token counts; never their contents."
                                : "Change the filters to see more.", palette: palette)
                } else {
                    ScrollView {
                        LazyVStack(spacing: 0) {
                            ForEach(rows) { r in
                                ActivityRow(row: r, palette: palette, cancel: cancel(for: r))
                                Rectangle().fill(palette.faint).frame(height: 1)
                            }
                        }
                    }
                }
            }
        }
    }

    /// A Cancel for a request still running, when it is this window's own Try request (the gateway
    /// offers no way to cancel another client's request).
    private func cancel(for r: LedgerRow) -> (() -> Void)? {
        guard r.isInFlight, ws.tryout.isRunning, ws.tryout.result?.requestID == r.id else { return nil }
        return { ws.tryout.cancel() }
    }

    private func matches(_ r: LedgerRow) -> Bool {
        switch outcome {
        case "all": true
        case "not_available": r.isNotAvailable
        case "refused": r.outcome == "refused" && !r.isNotAvailable
        case "in_flight": r.isInFlight
        default: r.outcome == outcome
        }
    }

    private func modelNames(_ store: LocalModelsStore) -> [String] {
        Array(Set(store.models.map(\.name) + store.requests.compactMap(\.model))).sorted()
    }

    private var header: some View {
        HStack(spacing: 8) {
            Text("Time").frame(width: 84, alignment: .leading)
            Text("Client").frame(width: 110, alignment: .leading)
            Text("Model").frame(minWidth: 86, maxWidth: .infinity, alignment: .leading)
            Text("Route").frame(width: 50, alignment: .leading)
            Text("Status").frame(width: 46, alignment: .trailing)
            Text("Queue").frame(width: 46, alignment: .trailing)
            Text("Load").frame(width: 52, alignment: .trailing)
            Text("Total").frame(width: 56, alignment: .trailing)
            Text("Tokens in / out").frame(width: 114, alignment: .trailing)
        }
        .font(.system(size: 10.5, weight: .medium))
        .foregroundStyle(palette.ink3)
        .padding(.bottom, 4)
    }
}

private struct ActivityRow: View {
    let row: LedgerRow
    let palette: StatsPalette
    var cancel: (() -> Void)?

    /// The status column: the HTTP status, or a word where there is none or it would mislead.
    private func statusText(_ r: LedgerRow) -> String {
        if r.isInFlight { return "…" }
        switch r.outcome {
        case "interrupted": return "cut"
        case "partial": return "part"
        default: return r.isNotAvailable ? "off" : r.status.map(String.init) ?? "—"
        }
    }

    var body: some View {
        let r = row
        HStack(spacing: 8) {
            Text(LMFormat.when(r.started)).frame(width: 84, alignment: .leading)
            Text(r.client ?? "—").foregroundStyle(r.client == nil ? palette.ink3 : palette.ink).frame(width: 110, alignment: .leading)
            Text(r.model ?? "—").foregroundStyle(r.model == nil ? palette.ink3 : palette.ink).frame(minWidth: 86, maxWidth: .infinity, alignment: .leading)
            Text(r.isInFlight ? (r.phase ?? "running") : r.routeName).foregroundStyle(r.isInFlight ? Color(nsColor: .systemOrange) : palette.ink2).frame(width: 50, alignment: .leading)
            Text(statusText(r)).fontWeight(.semibold).foregroundStyle(LMFormat.statusColor(r.status)).frame(width: 46, alignment: .trailing)
            Text(LMFormat.ms(r.queueMs)).frame(width: 46, alignment: .trailing)
            Text(LMFormat.ms(r.loadMs)).frame(width: 52, alignment: .trailing)
            Text(r.isInFlight ? LMFormat.ms(r.elapsedMs) + "…" : LMFormat.ms(r.durationMs)).frame(width: 56, alignment: .trailing)
            Text("\(LMFormat.tokens(r.promptTokens)) / \(LMFormat.tokens(r.completionTokens))")
                .foregroundStyle(r.promptTokens == nil && r.completionTokens == nil ? palette.ink3 : palette.ink)
                .frame(width: 114, alignment: .trailing)
        }
        .font(.system(size: 11)).monospacedDigit().lineLimit(1)
        .frame(height: 22)
        .background(r.isInFlight ? Color(nsColor: .systemOrange).opacity(0.07) : .clear)
        .overlay(alignment: .trailing) {
            if let cancel {
                Button("Cancel", action: cancel).controlSize(.mini).padding(.trailing, 2)
                    .accessibilityLabel("Cancel request \(r.id)")
            }
        }
        .help("\(r.id) · \(r.route) · \(r.outcomeLabel)\(r.phase.map { " · \($0)" } ?? "")\(r.errorKind.map { " · \($0)" } ?? "")")
    }

}

// MARK: Logs

struct LogsSection: View {
    let ws: LocalModelsWorkspaceState
    let presence: WindowPresence
    let palette: StatsPalette
    @State private var source = "gateway"
    @State private var lines = 200
    @State private var following = true
    @State private var copied = false

    var body: some View {
        let store = ws.store
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                Picker("Source", selection: $source) {
                    Text("Gateway").tag("gateway")
                    ForEach(sources(store), id: \.self) { Text($0).tag($0) }
                }
                .frame(width: 190)
                Picker("Lines", selection: $lines) {
                    ForEach([100, 200, 500, 1000], id: \.self) { Text("Last \($0)").tag($0) }
                }
                .frame(width: 110)
                Button { following.toggle() } label: {
                    Label(following ? "Pause" : "Follow", systemImage: following ? "pause.fill" : "play.fill")
                }
                .help(following ? "Stop asking for new lines" : "Ask for new lines every 2 s while this shows")
                Button {
                    Task { await store.refreshLog(source: source, lines: lines) }
                } label: { Label("Refresh", systemImage: "arrow.clockwise") }
                    .disabled(following)
                Spacer()
                Button {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString((store.log?.lines ?? []).joined(separator: "\n"), forType: .string)
                    copied = true
                } label: { Label(copied ? "Copied" : "Copy", systemImage: copied ? "checkmark" : "doc.on.doc") }
                    .disabled(store.log?.lines.isEmpty ?? true)
            }
            .labelsHidden()
            .controlSize(.small)
            .padding(.bottom, 10)

            if let error = store.logError, store.state == .running || store.log != nil {
                Label("Couldn't read the log: \(error)", systemImage: "exclamationmark.triangle")
                    .font(.system(size: 11.5)).foregroundStyle(Color(nsColor: .systemOrange))
                    .textSelection(.enabled).padding(.bottom, 8)
            }
            if store.state != .running, store.log == nil {
                GatewayRecovery(ws: ws, palette: palette)
            } else if let log = store.log, log.source == source {
                LMStaleBanner(store: store, updated: store.logUpdated, palette: palette)
                if log.lines.isEmpty {
                    LMEmpty(icon: "text.alignleft", title: "Nothing logged yet",
                            message: source == "gateway" ? "The gateway's log is empty." : "\(source) has no log yet: it is written once its worker first starts.", palette: palette)
                } else {
                    LogText(lines: log.lines, following: following, palette: palette)
                }
            } else {
                LMEmpty(icon: "hourglass", title: "Reading the log…", message: "Asking for the last \(lines) lines of \(source).", palette: palette)
            }
        }
        .task(id: "\(source)|\(lines)|\(following)|\(presence.visible)|\(ws.settings.address)") {
            copied = false
            await store.refreshLog(source: source, lines: lines)
            guard following, presence.visible else { return }
            // Following asks only for what was written since (`since`/`next`), when the gateway offers it.
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(2))
                if Task.isCancelled { return }
                await store.refreshLog(source: source, lines: lines, follow: true)
            }
        }
    }

    private func sources(_ store: LocalModelsStore) -> [String] {
        let names = store.models.map(\.name)
        return names.isEmpty ? (store.registry?.models.map(\.name) ?? []) : names
    }
}

private struct LogText: View {
    let lines: [String]
    let following: Bool
    let palette: StatsPalette

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView(.vertical) {
                LazyVStack(alignment: .leading, spacing: 1) {
                    ForEach(Array(lines.enumerated()), id: \.offset) { i, line in
                        Text(line)
                            .foregroundStyle(line.contains(" ERROR ") || line.contains("Traceback") ? Color(nsColor: .systemRed)
                                : line.contains(" WARNING ") ? Color(nsColor: .systemOrange) : palette.ink)
                            .id(i)
                    }
                }
                .font(.system(size: 11, design: .monospaced))
                .textSelection(.enabled)
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(palette.faint))
            .onAppear { if following { proxy.scrollTo(lines.count - 1, anchor: .bottom) } }
            .onChange(of: lines) { if following { proxy.scrollTo(lines.count - 1, anchor: .bottom) } }
        }
    }
}

// MARK: Settings

struct LMSettingsSection: View {
    let ws: LocalModelsWorkspaceState
    let palette: StatsPalette
    @State private var addressText = ""
    @State private var addressError: String?
    @State private var loginWorking = false
    @State private var loginNote: String?

    var body: some View {
        let settings = ws.settings
        let store = ws.store
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                field("Address", "The gateway's address. Loopback only: 127.0.0.1, never localhost or another host.") {
                    HStack {
                        TextField("http://127.0.0.1:18900", text: $addressText)
                            .textFieldStyle(.roundedBorder)
                            .font(.system(size: 12, design: .monospaced))
                            .frame(width: 260)
                            .onSubmit(apply)
                        Button("Apply", action: apply).disabled(addressText == settings.address.description)
                        Button("Default") { addressText = GatewayAddress.default.description; apply() }
                            .disabled(settings.address == .default)
                    }
                    if let addressError {
                        Text(addressError).font(.system(size: 11)).foregroundStyle(Color(nsColor: .systemRed))
                    }
                }
                field("Backend folder", "Holds pyproject.toml, registry.json and .venv/bin/tinybar-models, which Start runs. Stop asks the gateway itself.") {
                    HStack {
                        path(settings.backendFolder.map { Paths.abbreviated($0) } ?? "Not set")
                        Button("Choose…") { chooseFolder() }
                        Button("Use the repository's") {
                            settings.backendFolder = LocalModelsSettings.repositoryBackend()
                            store.loadRegistry()
                        }
                        .disabled(LocalModelsSettings.repositoryBackend() == nil || settings.backendFolder == LocalModelsSettings.repositoryBackend())
                    }
                    Text(backendStatus).font(.system(size: 11)).foregroundStyle(palette.ink2)
                }
                field("Key file", "The gateway writes it on its first start. It is read for each call and never shown, stored or logged here.") {
                    HStack {
                        path(Paths.abbreviated(settings.keyFile.url))
                        Button("Choose…") { chooseKey() }
                        Button("Reveal in Finder") { LMFinder.reveal(settings.keyFile.url) }
                        Button("Default") { settings.keyFile = .standard() }.disabled(settings.keyFile == .standard())
                    }
                    let problem = settings.keyFile.check()
                    Label(problem?.summary ?? "Present, and only you can read it.", systemImage: problem == nil ? "checkmark.circle" : "exclamationmark.triangle")
                        .font(.system(size: 11))
                        .foregroundStyle(problem == nil ? palette.ink2 : Color(nsColor: .systemOrange))
                }
                field("Start at login", "Runs the backend's own `tinybar-models login install` or `login remove`; launchd then starts the gateway when you log in.") {
                    if let item = store.status?.loginItem {
                        Toggle("Start the gateway at login", isOn: Binding(get: { item == "installed" }, set: { on in
                            loginWorking = true
                            Task { loginNote = await store.setLoginItem(on); loginWorking = false }
                        }))
                        .toggleStyle(.switch)
                        .disabled(loginWorking || ws.settings.cli == nil)
                        Text("Login item: \(item) · this gateway was started \(store.status?.startedBy == "launchd" ? "at login (launchd)" : store.status?.startedBy == "manual" ? "by hand" : "— (not reported)")")
                            .font(.system(size: 11)).foregroundStyle(palette.ink2)
                        if let loginNote, !loginNote.isEmpty {
                            Text(loginNote).font(.system(size: 11)).foregroundStyle(palette.ink3).textSelection(.enabled)
                        }
                    } else {
                        Text(store.state == .running ? "This gateway doesn't report a login item; update the backend for this switch." : "Shown once the gateway runs.")
                            .font(.system(size: 11.5)).foregroundStyle(palette.ink2)
                    }
                }
                field("Menu bar", "While the popover is closed, the menu bar asks only /health (no key) every \(settings.healthSeconds) s, and every 60 s once the gateway is stopped. Nothing while the Mac sleeps.") {
                    Toggle("Show loaded models and memory beside the icon", isOn: Binding(get: { settings.menuText }, set: { settings.menuText = $0 }))
                        .toggleStyle(.switch)
                    Stepper(value: Binding(get: { settings.healthSeconds }, set: { settings.healthSeconds = $0 }), in: LocalModelsSettings.healthRange, step: 5) {
                        Text("Check every \(settings.healthSeconds) s").monospacedDigit()
                    }
                    .frame(width: 200, alignment: .leading)
                }
                field("Subscriptions", "Your installed Tinybar tracks your coding subscriptions. Off, Modelbar polls no provider, reads no login and sends no alert; on, it does all that as Tinybar does.") {
                    if let app = ws.app {
                        Toggle("Track coding subscriptions in Modelbar", isOn: Binding(get: { app.settings.tracksSubscriptions }, set: { app.setTracksSubscriptions($0) }))
                            .toggleStyle(.switch)
                    }
                }
                field("Poll interval", "How often a visible section asks the gateway. Nothing is asked while this window is closed or hidden.") {
                    Stepper(value: Binding(get: { settings.pollSeconds }, set: { settings.pollSeconds = $0 }), in: LocalModelsSettings.pollRange) {
                        Text("Every \(settings.pollSeconds) s").monospacedDigit()
                    }
                    .frame(width: 160, alignment: .leading)
                }
                field("From the registry", "Read-only: change them in registry.json, and restart the gateway.") {
                    if let r = store.registry {
                        Grid(alignment: .leading, horizontalSpacing: 18, verticalSpacing: 3) {
                            gridRow("Total memory budget", LMFormat.mb(r.memoryBudgetTotalMb))
                            gridRow("Idle unload after", r.defaults.ttlSeconds.map { LMFormat.span(Double($0)) } ?? "—")
                            gridRow("Concurrency", r.defaults.concurrency.map { "\($0) per model" } ?? "—")
                            gridRow("Queue limit", r.defaults.queueLimit.map { "\($0) waiting" } ?? "—")
                            gridRow("Ready timeout", r.defaults.readyTimeoutSeconds.map { LMFormat.span(Double($0)) } ?? "—")
                            ForEach(r.models.filter { $0.ttlSeconds != nil }, id: \.name) { m in
                                gridRow("\(m.name) unloads after", LMFormat.span(Double(m.ttlSeconds ?? 0)))
                            }
                        }
                        .font(.system(size: 12))
                    } else {
                        Text(store.registryError ?? "No registry read.").font(.system(size: 11.5)).foregroundStyle(palette.ink2)
                    }
                }
                Button("Reset these settings") {
                    settings.reset()
                    addressText = settings.address.description
                    store.loadRegistry()
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .controlSize(.small)
        .onAppear { addressText = settings.address.description }
    }

    private var backendStatus: String {
        guard let cli = ws.settings.cli else { return "Start and Stop need it." }
        return FileManager.default.isExecutableFile(atPath: cli.path)
            ? "Start runs \(Paths.abbreviated(cli)) serve --port \(ws.settings.address.port)."
            : "No \(Paths.abbreviated(cli)) yet: set the backend up once with `uv sync --directory backend`."
    }

    private func apply() {
        do {
            ws.settings.address = try GatewayAddress(parsing: addressText)
            addressText = ws.settings.address.description
            addressError = nil
        } catch {
            addressError = error.summary
        }
    }

    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.prompt = "Use This Folder"
        panel.message = "Choose the backend folder (it holds registry.json)."
        if panel.runModal() == .OK, let url = panel.url {
            ws.settings.backendFolder = url
            ws.store.loadRegistry()
        }
    }

    private func chooseKey() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.showsHiddenFiles = true
        panel.prompt = "Use This File"
        panel.message = "Choose the gateway's key file. Only its path is kept."
        if panel.runModal() == .OK, let url = panel.url {
            ws.settings.keyFile = KeyFile(url: url)
        }
    }

    private func field<C: View>(_ title: String, _ note: String, @ViewBuilder _ content: () -> C) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            SectionTitle(text: title, palette: palette)
            Text(note).font(.system(size: 11)).foregroundStyle(palette.ink2)
            content()
        }
    }

    private func path(_ text: String) -> some View {
        Text(text)
            .font(.system(size: 11.5, design: .monospaced))
            .lineLimit(1).truncationMode(.middle)
            .textSelection(.enabled)
            .padding(.horizontal, 8)
            .frame(maxWidth: 330, minHeight: 22, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 6, style: .continuous).fill(palette.faint))
    }

    private func gridRow(_ label: String, _ value: String) -> some View {
        GridRow {
            Text(label).foregroundStyle(palette.ink2)
            Text(value).monospacedDigit()
        }
    }
}

extension String {
    var nonEmpty: String? { isEmpty ? nil : self }
}
