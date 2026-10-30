import Foundation

// Launched by the user's LaunchAgent, never by Bridge. Paths come from the
// signed bundle's installation configuration, not from an IPC request.
struct Runtime: Decodable {
    let python: String
    let socket: String
    let environment: [String: String]
}
do {
    let url = Bundle.main.resourceURL!.appendingPathComponent("runtime.json")
    let runtime = try JSONDecoder().decode(Runtime.self, from: Data(contentsOf: url))
    guard runtime.python.hasPrefix("/"), runtime.socket.hasPrefix("/") else { exit(2) }
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
    termination.resume(); interrupt.resume()
    try child.run()
    child.waitUntilExit()
    termination.cancel(); interrupt.cancel()
    exit(child.terminationStatus)
} catch { exit(2) }
