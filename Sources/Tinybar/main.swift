import AppKit
import ServiceManagement
import TinybarCore

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var model: AppModel?
    private var statusItem: StatusItemController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.mainMenu = AppMenu.make()
        let model = AppModel(settings: Settings())
        self.model = model
        statusItem = StatusItemController(model: model)
        model.start()
        // Developer aid: open the Local models page at launch, as its popover button does.
        if CommandLine.arguments.contains("--open-local-models") { model.openLocalModels() }
    }
}

// The fork's code inside a bundle that claims upstream's identity would share its preferences,
// login item and notifications: refuse to run at all (ADR 0015).
if Bundle.main.bundleIdentifier == ForkIdentity.upstreamBundleIdentifier {
    FileHandle.standardError.write(Data("This is \(ForkIdentity.appName); it will not run as \(ForkIdentity.upstreamBundleIdentifier).\n".utf8))
    exit(78)
}

// Developer aid: print whether this bundle is registered as a login item, changing nothing.
if CommandLine.arguments.contains("--login-item-status") {
    let status: String = switch SMAppService.mainApp.status {
    case .enabled: "enabled"
    case .notRegistered: "notRegistered"
    case .requiresApproval: "requiresApproval"
    case .notFound: "notFound"
    @unknown default: "unknown"
    }
    print("\(Bundle.main.bundleIdentifier ?? "no bundle") login item: \(status)")
    exit(0)
}

HistoryImport.runChildIfRequested()
MainActor.assumeIsolated { PopoverSnapshot.runIfRequested() }

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)  // menu bar only, no Dock icon
app.run()
