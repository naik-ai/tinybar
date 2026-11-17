import Foundation

/// Whether Modelbar tracks coding subscriptions itself. Off by default (ADR 0017): the installed
/// Tinybar already does, and two apps polling the same providers and reading the same logins would
/// double the work and the risk. On, Modelbar behaves as upstream Tinybar does.
public enum SubscriptionTracking {
    public static let key = "tracksSubscriptions"

    public static func isOn(_ defaults: UserDefaults = .standard) -> Bool {
        defaults.bool(forKey: key)
    }

    public static func set(_ on: Bool, _ defaults: UserDefaults = .standard) {
        defaults.set(on, forKey: key)
    }
}
