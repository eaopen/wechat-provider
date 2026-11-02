import Foundation

// Launched by the user's LaunchAgent (or explicitly by the user), never by Bridge.
// The runtime configuration lives OUTSIDE the app bundle so Python Provider /
// Wechat-MCP SDK / reader environment updates never require rebuilding this helper.
// The path comes from the signed Info.plist or a fixed user-private default, never
// from IPC. Ad-hoc code identity changes per build (Apple TN3127): a rebuild may
// require the user to grant file access again; this helper never changes TCC.
struct Runtime: Decodable {
    let python: String
    let socket: String
    let environment: [String: String]
}

let runtimePathKey = "WCProviderRuntimePath"
// Mirror the build script's allowlist: the helper forwards only fixed provider
// settings to the daemon, even if the external config is hand-edited.
let allowedEnvironment: Set<String> = [
    "WECHAT_PROVIDER_READER", "WECHAT_PROVIDER_WM_PYTHON", "WECHAT_PROVIDER_WM_STATE_DIR",
    "WECHAT_PROVIDER_WM_SOURCE_ID", "WECHAT_PROVIDER_GB_EXECUTABLE",
    "WECHAT_PROVIDER_GB_PROFILE", "WECHAT_PROVIDER_GB_SOURCE_ID", "WECHAT_PROVIDER_DB",
    "WECHAT_PROVIDER_ACCOUNT",
]
let pathEnvironment: Set<String> = [
    "WECHAT_PROVIDER_WM_PYTHON", "WECHAT_PROVIDER_WM_STATE_DIR",
    "WECHAT_PROVIDER_GB_EXECUTABLE", "WECHAT_PROVIDER_DB",
]

func configurationURL() -> URL {
    if let configured = Bundle.main.object(forInfoDictionaryKey: runtimePathKey) as? String,
       configured.hasPrefix("/") {
        return URL(fileURLWithPath: configured)
    }
    return FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/wechat-provider/helper/runtime.json")
}

// Same-user, user-private config only: regular file, owned by this user, no
// group/world permission bits. The external file is written atomically by the
// build script (mode 0600) and is not a place to smuggle command arguments.
func isSafeConfigurationFile(_ url: URL) -> Bool {
    do {
        let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
        guard (attributes[.type] as? FileAttributeType) == .typeRegular,
              let owner = attributes[.ownerAccountID] as? NSNumber,
              owner.uint32Value == getuid(),
              let permissions = attributes[.posixPermissions] as? NSNumber,
              (permissions.uint16Value & 0o077) == 0 else {
            return false
        }
        return true
    } catch {
        return false
    }
}

// Fixed daemon command: no shell, no IPC-provided arguments or paths.
func runDaemon(_ runtime: Runtime) throws -> Never {
    let child = Process()
    child.executableURL = URL(fileURLWithPath: runtime.python)
    child.arguments = ["-m", "wechat_provider.cli", "daemon", "--socket", runtime.socket]
    child.environment = ["HOME": NSHomeDirectory(), "PATH": "/usr/bin:/bin"]
        .merging(runtime.environment) { _, configured in configured }
    child.standardOutput = FileHandle.nullDevice
    child.standardError = FileHandle.nullDevice

    signal(SIGTERM, SIG_IGN)
    signal(SIGINT, SIG_IGN)
    let termination = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .global())
    let interrupt = DispatchSource.makeSignalSource(signal: SIGINT, queue: .global())
    termination.setEventHandler { if child.isRunning { child.terminate() } }
    interrupt.setEventHandler { if child.isRunning { child.terminate() } }
    termination.resume()
    interrupt.resume()

    try child.run()
    child.waitUntilExit()
    termination.cancel()
    interrupt.cancel()
    exit(child.terminationStatus)
}

do {
    let url = configurationURL()
    // Exit 2 = invalid/missing user-private configuration; no paths are logged.
    guard isSafeConfigurationFile(url) else { exit(2) }
    let runtime = try JSONDecoder().decode(Runtime.self, from: Data(contentsOf: url))
    guard runtime.python.hasPrefix("/"), runtime.socket.hasPrefix("/"),
          !runtime.python.contains("\0"), !runtime.socket.contains("\0") else { exit(2) }
    guard Set(runtime.environment.keys).isSubset(of: allowedEnvironment) else { exit(2) }
    for (key, value) in runtime.environment where pathEnvironment.contains(key) {
        if !value.isEmpty && !value.hasPrefix("/") { exit(2) }
        if value.contains("\0") { exit(2) }
    }
    try runDaemon(runtime)
} catch {
    exit(2)
}
