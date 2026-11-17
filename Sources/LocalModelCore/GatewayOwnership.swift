import Foundation

/// Whether this app owns the gateway at an address: it started that gateway itself, at that
/// address, and the gateway answering there is that one. Only then may Stop skip its confirmation,
/// and only then may the app signal the process it started.
public struct LaunchRecord: Equatable, Sendable {
    public var address: GatewayAddress
    public var pid: Int32
    public var startedAt: Date

    public init(address: GatewayAddress, pid: Int32, startedAt: Date) {
        self.address = address
        self.pid = pid
        self.startedAt = startedAt
    }
}

public enum GatewayOwnership {
    /// How far the gateway's own start time may lie from the launch: it starts within seconds.
    public static let window: TimeInterval = 120

    /// `status` is what the gateway at `address` says now. Launched by launchd, at another port,
    /// started outside the window or not saying when: not owned.
    public static func owns(_ record: LaunchRecord?, childRunning: Bool, address: GatewayAddress, status: GatewayStatus?) -> Bool {
        guard let record, childRunning, record.address == address, let status else { return false }
        if status.startedBy == "launchd" { return false }
        if let port = status.port, port != address.port { return false }
        guard let text = status.startedAt, let started = parseDate(text) else { return false }
        let delta = started.timeIntervalSince(record.startedAt)
        return delta >= -5 && delta <= window
    }

    /// `2026-11-17T13:59:40+0530`, as the gateway writes it.
    public static func parseDate(_ text: String) -> Date? {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ssZ"
        return f.date(from: text)
    }
}
