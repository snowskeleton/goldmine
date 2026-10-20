import Foundation
import ServiceManagement
import SwiftUI

// A client for a Goldmine server: the server collects prices and stores everything. This app
// uploads game data when WoW saves it, downloads prices for the addon, shows reports, and keeps
// itself and the addon up to date. It runs goldmine.py for all of it, and only ever in server
// mode: with no server connected it does nothing but ask for one (goldmine.py would otherwise
// fall back to working locally).

struct LogLine: Identifiable {
    let id = UUID()
    let date: Date
    let source: String
    let text: String
    let isError: Bool
}

/// Editable view of config.toml. Unknown keys in the file are preserved on save.
struct Settings {
    var serverURL = ""           // https://gold.example.com; with apiKey, required: the app does nothing without them
    var apiKey = ""              // gm_…, from the server's Clients page
    var autoSync = true          // upload SavedVariables whenever WoW writes them
    var autoPush = true          // download prices into the addon after each upload
    var autoUpdateAddon = true   // keep installed copies of the addon current
    var autoUpdateApp = true     // install new app builds from the server
}

struct MarketStatus: Codable, Identifiable {
    var id: String { name }
    let name: String
    let snapshots: Int
    let last_modified: Int?
    let last_fetched: Int?
    let items: Int?
    let game: String?
}

struct TimingBucket: Codable {
    let samples: Int
    let sold: Int
    let sold_avg: Double?
    let p10_avg: Int?
    let min: Int?
    let qty_avg: Double?
}

struct TimingMarket: Codable, Identifiable {
    var id: String { market }
    let market: String
    let game: String?
    let buckets: [TimingBucket]  // 168, local time, index 0 = Sunday 00:00
}

struct TimingResult: Codable {
    let item_id: Int
    let name: String
    let utc_offset_hours: Int
    let markets: [TimingMarket]
}

struct CharacterStatus: Codable, Identifiable {
    var id: String { key }
    let key: String
    let balance: Int?
    let last_seen: Int?
    let region: String?
}

struct StatusInfo: Codable {
    let db_path: String
    let db_bytes: Int
    let transactions: Int
    let last_transaction: Int?
    let scans: Int
    let recipes: Int
    let characters: [CharacterStatus]
    let markets: [MarketStatus]
    let saved_variables: [String]
    let server: String?
}

struct ServerCheck: Codable {
    let ok: Bool
    let server: String
    let user: String
    let admin: Bool
    let server_version: String
}

func formatGold(_ copper: Int) -> String {
    let sign = copper < 0 ? "-" : ""
    let c = abs(copper)
    let g = c / 10000, s = (c / 100) % 100, cu = c % 100
    if g > 0 { return "\(sign)\(g.formatted())g \(String(format: "%02d", s))s \(String(format: "%02d", cu))c" }
    if s > 0 { return "\(sign)\(s)s \(String(format: "%02d", cu))c" }
    return "\(sign)\(cu)c"
}

func relativeTime(_ unix: Int?) -> String {
    guard let unix else { return "never" }
    let f = RelativeDateTimeFormatter()
    f.unitsStyle = .short
    return f.localizedString(for: Date(timeIntervalSince1970: TimeInterval(unix)), relativeTo: Date())
}

/// Thread-safe byte accumulator for a process's stdout.
final class OutputBuffer: @unchecked Sendable {
    private var data = Data()
    private let lock = NSLock()
    func append(_ d: Data) { lock.lock(); data.append(d); lock.unlock() }
    var text: String { lock.lock(); defer { lock.unlock() }; return String(decoding: data, as: UTF8.self) }
}

/// Splits a pipe's output into lines as it arrives.
final class LineReader {
    private var buffer = Data()
    private let onLine: (String) -> Void
    init(onLine: @escaping (String) -> Void) { self.onLine = onLine }

    func feed(_ data: Data) {
        buffer.append(data)
        while let nl = buffer.firstIndex(of: 0x0A) {
            let line = String(decoding: buffer[buffer.startIndex..<nl], as: UTF8.self)
            buffer.removeSubrange(buffer.startIndex...nl)
            onLine(line)
        }
    }

    func flush() {
        if !buffer.isEmpty { onLine(String(decoding: buffer, as: UTF8.self)); buffer.removeAll() }
    }
}

@MainActor
final class Engine: ObservableObject {
    @Published var log: [LogLine] = []
    @Published var settings = Settings()
    @Published var status: StatusInfo?
    @Published var syncing = false
    @Published var configError: String?
    @Published var watching = false
    @Published var updates = UpdateState()

    // Both empty by default: find Python automatically, and run the copy of desktop/ bundled in
    // the app (so app updates also update the Python side). goldmineDir points at a checkout for development.
    @AppStorage("pythonPath") var pythonPath: String = ""
    @AppStorage("goldmineDir") var goldmineDir: String = ""
    var updateTimer: Timer?

    private var rawConfig: [String: Any] = [:]
    private var statusTimer: Timer?
    private var watchProcess: Process?
    private var stopping = false

    var scriptPath: String {
        let base = goldmineDir.trimmingCharacters(in: .whitespaces).isEmpty ? (Bundle.main.resourcePath ?? "") : goldmineDir
        return (base as NSString).appendingPathComponent("desktop/goldmine.py")
    }

    /// The Python to run: the one set in Settings, else the one the app was built with, else a
    /// common install location. (macOS's /usr/bin/python3 is too old unless the CLT ship 3.11+.)
    var python: String {
        let fm = FileManager.default
        let set = pythonPath.trimmingCharacters(in: .whitespaces)
        if !set.isEmpty { return set }
        let candidates = [Bundle.main.object(forInfoDictionaryKey: "GoldminePython") as? String ?? "",
                          "/opt/homebrew/bin/python3", "/usr/local/bin/python3",
                          "/Library/Frameworks/Python.framework/Versions/Current/bin/python3", "/usr/bin/python3"]
        return candidates.first { !$0.isEmpty && fm.isExecutableFile(atPath: $0) } ?? "/usr/bin/python3"
    }
    var busy: Bool { syncing }
    /// True once a server address and API key are saved. Everything that talks to goldmine.py checks this first.
    var serverMode: Bool {
        !settings.serverURL.trimmingCharacters(in: .whitespaces).isEmpty && !settings.apiKey.trimmingCharacters(in: .whitespaces).isEmpty
    }

    /// The server's address, for showing and opening in a browser.
    var serverDisplayURL: URL? {
        guard serverMode, var parts = URLComponents(string: settings.serverURL.trimmingCharacters(in: .whitespaces)) else { return nil }
        parts.user = nil
        parts.password = nil
        return parts.url
    }

    /// Tests a server address and key (saved or not). Returns a description of the result.
    /// The key goes in the environment, not the arguments, so it doesn't show up in `ps`.
    func checkServer(_ url: String, key: String) async -> (Bool, String) {
        let (code, out) = await run(["--server", url.trimmingCharacters(in: .whitespacesAndNewlines), "server-check", "--json"],
                                    label: "server", quiet: true,
                                    env: ["GOLDMINE_KEY": key.trimmingCharacters(in: .whitespacesAndNewlines)])
        if code == 0, let data = out.data(using: .utf8), let r = try? JSONDecoder().decode(ServerCheck.self, from: data) {
            return (true, "Connected as \(r.user)\(r.admin ? " (admin)" : "") · server \(r.server_version)")
        }
        let last = log.last(where: { $0.source == "server" && $0.isError })?.text ?? "couldn't connect"
        return (false, last.replacingOccurrences(of: "error: ", with: ""))
    }

    init() {
        Task { await start() }
    }

    func start() async {
        await loadConfig()
        applySchedule()
        statusTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.refreshStatus() }
        }
        startUpdateChecks()
    }

    // MARK: - Logging

    func append(_ source: String, _ text: String, error: Bool = false) {
        let trimmed = text.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return }
        log.append(LogLine(date: Date(), source: source, text: trimmed, isError: error))
        if log.count > 2000 { log.removeFirst(log.count - 2000) }
    }

    // MARK: - Running goldmine.py

    /// Runs goldmine.py with args. Output is streamed to the log unless quiet; stdout is returned.
    @discardableResult
    func run(_ args: [String], label: String, quiet: Bool = false, stdin: Data? = nil,
             env extra: [String: String] = [:]) async -> (Int32, String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = ["-B", scriptPath] + args  // -B: never write __pycache__ into the signed bundle
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"  // never write __pycache__ into the signed app bundle
        env.merge(extra) { _, new in new }
        process.environment = env
        let out = Pipe(), err = Pipe()
        process.standardOutput = out
        process.standardError = err
        let inPipe = Pipe()
        if stdin != nil { process.standardInput = inPipe }

        let collected = OutputBuffer()
        let outReader = LineReader { line in
            if !quiet { Task { @MainActor in self.append(label, line) } }
        }
        let errReader = LineReader { line in
            Task { @MainActor in self.append(label, line, error: true) }
        }
        out.fileHandleForReading.readabilityHandler = { h in
            let d = h.availableData
            collected.append(d)
            outReader.feed(d)
        }
        err.fileHandleForReading.readabilityHandler = { h in errReader.feed(h.availableData) }

        return await withCheckedContinuation { cont in
            process.terminationHandler = { p in
                out.fileHandleForReading.readabilityHandler = nil
                err.fileHandleForReading.readabilityHandler = nil
                let rest = out.fileHandleForReading.readDataToEndOfFile()
                outReader.feed(rest); outReader.flush()
                errReader.feed(err.fileHandleForReading.readDataToEndOfFile()); errReader.flush()
                collected.append(rest)
                cont.resume(returning: (p.terminationStatus, collected.text))
            }
            do {
                try process.run()
                if let stdin {
                    inPipe.fileHandleForWriting.write(stdin)
                    try? inPipe.fileHandleForWriting.close()
                }
            } catch {
                Task { @MainActor in self.append(label, "could not start \(self.python): \(error.localizedDescription)", error: true) }
                cont.resume(returning: (-1, ""))
            }
        }
    }

    // MARK: - Actions

    func syncNow() async {
        guard serverMode, !syncing else { return }
        syncing = true
        defer { syncing = false }
        var args = ["sync"]
        if settings.autoPush { args.append("--push") }
        await run(args, label: "sync")
        await refreshStatus()
    }

    func pushNow() async {
        guard serverMode else { return }
        await run(["push"], label: "push")
    }

    func refreshStatus() async {
        guard serverMode else { status = nil; return }
        let (code, out) = await run(["status"], label: "status", quiet: true)
        guard code == 0, let data = out.data(using: .utf8) else { return }
        status = try? JSONDecoder().decode(StatusInfo.self, from: data)
    }

    func report(_ args: [String]) async -> String {
        guard serverMode else { return "Connect to a Goldmine server first." }
        let (_, out) = await run(args, label: args.first ?? "report", quiet: true)
        return out
    }

    func timing(_ item: String) async -> TimingResult? {
        guard serverMode else { return nil }
        let (code, out) = await run(["timing", item, "--json"], label: "timing", quiet: true)
        guard code == 0, let data = out.data(using: .utf8) else { return nil }
        return try? JSONDecoder().decode(TimingResult.self, from: data)
    }

    // MARK: - Config

    func loadConfig() async {
        let (code, out) = await run(["config", "get"], label: "config", quiet: true)
        guard code == 0, let data = out.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            configError = "Couldn't read the config. Check the Python path and Goldmine folder in Settings."
            return
        }
        configError = nil
        rawConfig = obj
        let app = obj["app"] as? [String: Any] ?? [:]
        let server = obj["server"] as? [String: Any] ?? [:]
        var s = Settings()
        s.serverURL = server["url"] as? String ?? ""  // `config get` splits old https://gm_KEY@host URLs
        s.apiKey = server["key"] as? String ?? ""
        s.autoSync = app["auto_sync"] as? Bool ?? true
        s.autoPush = app["auto_push"] as? Bool ?? true
        s.autoUpdateAddon = app["auto_update_addon"] as? Bool ?? true
        s.autoUpdateApp = app["auto_update_app"] as? Bool ?? true
        settings = s
    }

    func saveConfig() async -> Bool {
        var cfg = rawConfig  // keys this app doesn't edit (e.g. a CLI user's [api]) are kept as they are
        var app = cfg["app"] as? [String: Any] ?? [:]
        app["auto_sync"] = settings.autoSync
        app["auto_push"] = settings.autoPush
        app["auto_update_addon"] = settings.autoUpdateAddon
        app["auto_update_app"] = settings.autoUpdateApp
        cfg["app"] = app
        let url = settings.serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
        let key = settings.apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
        if url.isEmpty && key.isEmpty { cfg.removeValue(forKey: "server") } else { cfg["server"] = ["url": url, "key": key] }
        guard let data = try? JSONSerialization.data(withJSONObject: cfg, options: [.sortedKeys]) else { return false }
        let (code, _) = await run(["config", "set"], label: "config", stdin: data)
        if code == 0 {
            append("config", "saved settings")
            await loadConfig()
            applySchedule()
            return true
        }
        return false
    }

    // MARK: - Scheduling

    func applySchedule() {
        if serverMode && settings.autoSync { startWatch() } else { stopWatch() }
        Task { await refreshStatus() }
    }

    /// Saves a new server address and key (after they tested OK) or clears them, and restarts background work.
    func connect(_ url: String, key: String) async -> Bool {
        stopWatch()
        settings.serverURL = url.trimmingCharacters(in: .whitespacesAndNewlines)
        settings.apiKey = key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard await saveConfig() else { return false }
        updates = UpdateState()
        if serverMode { Task { await checkForUpdates(auto: true) } }
        return true
    }

    func startWatch() {
        guard serverMode, watchProcess == nil else { return }
        stopping = false
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        var args = ["-B", scriptPath, "watch", "--exit-with-parent"]
        if settings.autoPush { args.append("--push") }
        process.arguments = args
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"  // never write __pycache__ into the signed app bundle
        process.environment = env
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        let reader = LineReader { line in
            Task { @MainActor in
                self.append("sync", line)
                if line.contains(" new of ") { await self.refreshStatus() }
            }
        }
        pipe.fileHandleForReading.readabilityHandler = { h in reader.feed(h.availableData) }
        process.terminationHandler = { [weak self] _ in
            pipe.fileHandleForReading.readabilityHandler = nil
            Task { @MainActor in
                guard let self else { return }
                self.watchProcess = nil
                self.watching = false
                if !self.stopping && self.serverMode && self.settings.autoSync {
                    self.append("sync", "watcher stopped; restarting in 10s", error: true)
                    try? await Task.sleep(nanoseconds: 10_000_000_000)
                    self.startWatch()
                }
            }
        }
        do {
            try process.run()
            watchProcess = process
            watching = true
        } catch {
            append("sync", "could not start watcher: \(error.localizedDescription)", error: true)
        }
    }

    func stopWatch() {
        stopping = true
        watchProcess?.terminate()
        watchProcess = nil
        watching = false
    }

    func shutdown() {
        stopWatch()
    }

    // MARK: - Launch at login

    var launchAtLogin: Bool {
        get { SMAppService.mainApp.status == .enabled }
        set {
            do {
                if newValue { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
            } catch {
                append("app", "launch at login: \(error.localizedDescription)", error: true)
            }
            objectWillChange.send()
        }
    }
}
