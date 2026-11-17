import Foundation

/// The backend folder Start runs from, checked before anything in it is executed.
public enum BackendFolder {
    public enum Problem: Error, Equatable, Sendable {
        case notAFolder(String)
        case notSetUp(String)
        case outside(String)
        case notYours(String)
        case writableByOthers(String)

        public var summary: String {
            switch self {
            case let .notAFolder(p): "\(p) is not a backend folder: it has no pyproject.toml."
            case let .notSetUp(p): "No \(p). Set the backend up once with `uv sync --directory backend`."
            case let .outside(p): "\(p) leads outside the backend folder; it is not run."
            case let .notYours(p): "\(p) belongs to another user; it is not run."
            case let .writableByOthers(p): "\(p) or its folder can be changed by other users; it is not run."
            }
        }
    }

    /// `<folder>/.venv/bin/tinybar-models`, links resolved, inside the folder, a regular file owned by
    /// this user that neither group nor others can write, beside the backend's `pyproject.toml`.
    public static func cli(in folder: URL, uid: uid_t = getuid()) throws(Problem) -> URL {
        let root = folder.resolvingSymlinksInPath().standardizedFileURL
        guard FileManager.default.fileExists(atPath: root.appendingPathComponent("pyproject.toml").path) else {
            throw .notAFolder(root.path)
        }
        let named = root.appendingPathComponent(".venv/bin/tinybar-models")
        guard FileManager.default.fileExists(atPath: named.path) else { throw .notSetUp(named.path) }
        let real = named.resolvingSymlinksInPath().standardizedFileURL
        guard real.path.hasPrefix(root.path + "/") else { throw .outside(named.path) }
        for path in [real.path, real.deletingLastPathComponent().path] {
            var info = stat()
            guard stat(path, &info) == 0 else { throw .notSetUp(named.path) }
            guard info.st_uid == uid else { throw .notYours(path) }
            guard info.st_mode & 0o022 == 0 else { throw .writableByOthers(path) }
        }
        guard FileManager.default.isExecutableFile(atPath: real.path) else { throw .notSetUp(named.path) }
        return real
    }
}
