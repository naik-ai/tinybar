import Foundation
import Testing
@testable import TinybarCore

// ADR 0015: the fork's identity and data never meet installed Tinybar's.

@Test func forkIdentityDiffersFromUpstream() {
    #expect(ForkIdentity.bundleIdentifier != ForkIdentity.upstreamBundleIdentifier)
    #expect(ForkIdentity.appSupportFolder != ForkIdentity.upstreamAppSupportFolder)
    #expect(Paths.appSupport.lastPathComponent == ForkIdentity.appSupportFolder)
    #expect(!ForkIdentity.isUpstreamData(Paths.appSupport.appendingPathComponent("usage.sqlite")))
    #expect(!ForkIdentity.selfUpdates)
    #expect(!ForkIdentity.registersAtLoginOnFirstRun)
}

@Test func usageStoreRefusesUpstreamDatabase() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent("fork-\(UUID().uuidString)", isDirectory: true)
    let upstream = root.appendingPathComponent("Tinybar", isDirectory: true)
    try FileManager.default.createDirectory(at: upstream, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: root) }
    let db = upstream.appendingPathComponent("usage.sqlite")
    #expect(throws: (any Error).self) { try UsageStore(path: db, upstreamData: upstream) }
    #expect(!FileManager.default.fileExists(atPath: db.path))
    // Through a link to it, too.
    let link = root.appendingPathComponent("elsewhere", isDirectory: true)
    try FileManager.default.createSymbolicLink(at: link, withDestinationURL: upstream)
    #expect(throws: (any Error).self) { try UsageStore(path: link.appendingPathComponent("usage.sqlite"), upstreamData: upstream) }
    // A sibling folder whose name starts the same is not upstream's.
    let sibling = root.appendingPathComponent("Modelbar", isDirectory: true)
    try FileManager.default.createDirectory(at: sibling, withIntermediateDirectories: true)
    _ = try UsageStore(path: sibling.appendingPathComponent("usage.sqlite"), upstreamData: upstream)
}

@Test func subscriptionTrackingIsOffUntilTurnedOn() throws {
    let name = "modelbar-test-\(UUID().uuidString)"
    let defaults = try #require(UserDefaults(suiteName: name))
    defer { defaults.removePersistentDomain(forName: name) }
    #expect(!SubscriptionTracking.isOn(defaults))
    SubscriptionTracking.set(true, defaults)
    #expect(SubscriptionTracking.isOn(defaults))
    SubscriptionTracking.set(false, defaults)
    #expect(!SubscriptionTracking.isOn(defaults))
}
