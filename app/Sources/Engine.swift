import Foundation
import ServiceManagement
import SwiftUI

// Runs goldmine.py for everything: the Python tool stays the single source of truth for
// collection, syncing, reports, and config; this app schedules it and gives it a UI.

struct LogLine: Identifiable {
    let id = UUID()
    let date: Date
    let source: String
    let text: String
    let isError: Bool
}

struct MarketConfig: Identifiable, Hashable {
    var id = UUID()
    var name: String
    var region: String
    var namespace: String
    var kind: String
    var realm: String
    var pricing: Bool  // false: collected, but kept out of prices/advice
    var game: String   // empty: derived from the namespace (retail, classic, ...)

    init(name: String = "", region: String = "us", namespace: String = "dynamic-us",
         kind: String = "realm", realm: String = "", pricing: Bool = true, game: String = "") {
        self.name = name; self.region = region; self.namespace = namespace
        self.kind = kind; self.realm = realm; self.pricing = pricing; self.game = game
    }

    init(dict: [String: Any]) {
        self.init(name: dict["name"] as? String ?? "", region: dict["region"] as? String ?? "us",
                  namespace: dict["namespace"] as? String ?? "", kind: dict["kind"] as? String ?? "realm",
                  realm: dict["realm"] as? String ?? "", pricing: dict["pricing"] as? Bool ?? true,
                  game: dict["game"] as? String ?? "")
    }

    var dict: [String: Any] {
        var d: [String: Any] = ["name": name, "region": region, "namespace": namespace, "kind": kind]
        if kind != "commodities" && !realm.isEmpty { d["realm"] = realm }
        if !pricing { d["pricing"] = false }
        if !game.isEmpty { d["game"] = game }
        return d
    }
}

/// Editable view of config.toml. Unknown keys in the file are preserved on save.
struct Settings {
    var serverURL = ""      // https://gm_KEY@host: when set, the server collects and stores everything
    var clientId = ""
    var clientSecret = ""
    var autoCollect = true
    var collectMinutes = 20
    var autoSync = true
    var autoPush = true
    var markets: [MarketConfig] = []
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

struct RealmInfo: Codable, Identifiable, Hashable {
    var id: String { "\(connected_realm_id)-\(slug ?? "")" }
    let connected_realm_id: Int
    let slug: String?
    let name: String?
    let type: String?
    let category: String?
}

/// A game version to browse realms in (from the server via `goldmine.py namespaces`).
struct NamespaceOption: Codable, Identifiable, Hashable {
    var id: String { game }
    let game: String
    let label: String
    let prefix: String
    let auctions: Bool
    func namespace(region: String) -> String { "\(prefix)-\(region)" }
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
    @Published var collecting = false
    @Published var syncing = false
    @Published var lastCollect: Date?
    @Published var lastCollectOK = true
    @Published var configError: String?
    @Published var watching = false

    @AppStorage("pythonPath") var pythonPath: String = Bundle.main.object(forInfoDictionaryKey: "GoldminePython") as? String ?? "/usr/bin/python3"
    @AppStorage("goldmineDir") var goldmineDir: String = Bundle.main.object(forInfoDictionaryKey: "GoldmineDir") as? String ?? ""

    private var rawConfig: [String: Any] = [:]
    private var collectTimer: Timer?
    private var statusTimer: Timer?
    private var watchProcess: Process?
    private var stopping = false

    var scriptPath: String { (goldmineDir as NSString).appendingPathComponent("desktop/goldmine.py") }
    var busy: Bool { collecting || syncing }
    var serverMode: Bool { !settings.serverURL.trimmingCharacters(in: .whitespaces).isEmpty }

    /// The server's address without the client key, for showing and opening in a browser.
    var serverDisplayURL: URL? {
        guard serverMode, var parts = URLComponents(string: settings.serverURL.trimmingCharacters(in: .whitespaces)) else { return nil }
        parts.user = nil
        parts.password = nil
        return parts.url
    }

    /// Tests a server URL (saved or not). Returns a description of the result.
    func checkServer(_ url: String) async -> (Bool, String) {
        let (code, out) = await run(["--server", url, "server-check", "--json"], label: "server", quiet: true)
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
        await refreshStatus()
        applySchedule()
        statusTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.refreshStatus() }
        }
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
    func run(_ args: [String], label: String, quiet: Bool = false, stdin: Data? = nil) async -> (Int32, String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: pythonPath)
        process.arguments = [scriptPath] + args
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
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
                Task { @MainActor in self.append(label, "could not start \(self.pythonPath): \(error.localizedDescription)", error: true) }
                cont.resume(returning: (-1, ""))
            }
        }
    }

    // MARK: - Actions

    func collectNow() async {
        guard !collecting else { return }
        collecting = true
        defer { collecting = false }
        var args = ["collect"]
        if settings.autoPush { args.append("--push") }
        let (code, _) = await run(args, label: "collect")
        lastCollect = Date()
        lastCollectOK = code == 0
        await refreshStatus()
    }

    func syncNow() async {
        guard !syncing else { return }
        syncing = true
        defer { syncing = false }
        var args = ["sync"]
        if settings.autoPush { args.append("--push") }
        await run(args, label: "sync")
        await refreshStatus()
    }

    func pushNow() async {
        await run(["push"], label: "push")
    }

    func refreshStatus() async {
        let (code, out) = await run(["status"], label: "status", quiet: true)
        guard code == 0, let data = out.data(using: .utf8) else { return }
        status = try? JSONDecoder().decode(StatusInfo.self, from: data)
    }

    func report(_ args: [String]) async -> String {
        let (_, out) = await run(args, label: args.first ?? "report", quiet: true)
        return out
    }

    func timing(_ item: String) async -> TimingResult? {
        let (code, out) = await run(["timing", item, "--json"], label: "timing", quiet: true)
        guard code == 0, let data = out.data(using: .utf8) else { return nil }
        return try? JSONDecoder().decode(TimingResult.self, from: data)
    }

    func loadRealms(region: String, namespace: String) async -> [RealmInfo] {
        let (code, out) = await run(["realms", region, namespace, "--json"], label: "realms", quiet: true)
        guard code == 0, let data = out.data(using: .utf8) else { return [] }
        return (try? JSONDecoder().decode([RealmInfo].self, from: data)) ?? []
    }

    /// Game versions for the realm browser: the server's list when one is configured, else goldmine.py's built-in one.
    func loadNamespaces() async -> [NamespaceOption] {
        let (code, out) = await run(["namespaces", "--json"], label: "realms", quiet: true)
        guard code == 0, let data = out.data(using: .utf8) else { return [] }
        return (try? JSONDecoder().decode([NamespaceOption].self, from: data)) ?? []
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
        let api = obj["api"] as? [String: Any] ?? [:]
        let app = obj["app"] as? [String: Any] ?? [:]
        let server = obj["server"] as? [String: Any] ?? [:]
        var s = Settings()
        s.serverURL = server["url"] as? String ?? ""
        s.clientId = api["client_id"] as? String ?? ""
        s.clientSecret = api["client_secret"] as? String ?? ""
        s.autoCollect = app["auto_collect"] as? Bool ?? true
        s.collectMinutes = app["collect_minutes"] as? Int ?? 20
        s.autoSync = app["auto_sync"] as? Bool ?? true
        s.autoPush = app["auto_push"] as? Bool ?? true
        s.markets = (obj["market"] as? [[String: Any]] ?? []).map(MarketConfig.init(dict:))
        settings = s
    }

    func saveConfig() async -> Bool {
        var cfg = rawConfig
        var api = cfg["api"] as? [String: Any] ?? [:]
        api["client_id"] = settings.clientId
        api["client_secret"] = settings.clientSecret
        cfg["api"] = api
        var app = cfg["app"] as? [String: Any] ?? [:]
        app["auto_collect"] = settings.autoCollect
        app["collect_minutes"] = settings.collectMinutes
        app["auto_sync"] = settings.autoSync
        app["auto_push"] = settings.autoPush
        cfg["app"] = app
        let url = settings.serverURL.trimmingCharacters(in: .whitespaces)
        if url.isEmpty { cfg.removeValue(forKey: "server") } else { cfg["server"] = ["url": url] }
        cfg["market"] = settings.markets.map(\.dict)

        // Keep [[market]] last so the TOML stays readable.
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
        collectTimer?.invalidate()
        collectTimer = nil
        if settings.autoCollect && !settings.clientId.isEmpty && !serverMode {
            let interval = TimeInterval(max(settings.collectMinutes, 5) * 60)
            collectTimer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
                Task { @MainActor in await self?.collectNow() }
            }
            Task { await collectNow() }
        }
        if settings.autoSync { startWatch() } else { stopWatch() }
    }

    func startWatch() {
        guard watchProcess == nil else { return }
        stopping = false
        let process = Process()
        process.executableURL = URL(fileURLWithPath: pythonPath)
        var args = [scriptPath, "watch", "--exit-with-parent"]
        if settings.autoPush { args.append("--push") }
        process.arguments = args
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
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
                if !self.stopping && self.settings.autoSync {
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
