import SwiftUI

struct MainView: View {
    @EnvironmentObject var engine: Engine

    var body: some View {
        if engine.serverMode {
            TabView {
                StatusView().tabItem { Label("Status", systemImage: "gauge") }
                ReportsView().tabItem { Label("Reports", systemImage: "chart.bar") }
                UpdatesView().tabItem { Label("Updates", systemImage: "arrow.down.circle") }
                SettingsView().tabItem { Label("Settings", systemImage: "gearshape") }
            }
            .padding()
        } else {
            ConnectView()
        }
    }
}

// MARK: - Connect

/// Shown until a server is connected: the app has nothing to do without one.
struct ConnectView: View {
    @EnvironmentObject var engine: Engine
    @State private var url = ""
    @State private var key = ""
    @State private var reveal = false
    @State private var working = false
    @State private var result: (Bool, String)?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label("Connect to your Goldmine server", systemImage: "server.rack").font(.title2.bold())
            Text("Your server collects auction prices and stores your ledger. This app uploads your game data whenever WoW saves it, downloads prices into the addon, and keeps itself and the addon up to date.")
                .foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 6) {
                Text("1. Open your server in a browser and sign in.")
                Text("2. On the **Clients** page, generate an API key.")
                Text("3. Paste the server address and the key here. Keys start with gm_ (an invite link won't work).")
            }
            ServerFields(url: $url, key: $key, reveal: $reveal)
            HStack {
                if let (ok, text) = result {
                    Label(text, systemImage: ok ? "checkmark.circle" : "xmark.octagon")
                        .foregroundStyle(ok ? .green : .red)
                }
                Spacer()
                Button(working ? "Connecting…" : "Connect") {
                    Task {
                        working = true
                        defer { working = false }
                        result = await engine.checkServer(url, key: key)
                        if result?.0 == true { _ = await engine.connect(url, key: key) }
                    }
                }
                .disabled(working || url.trimmingCharacters(in: .whitespaces).isEmpty
                          || key.trimmingCharacters(in: .whitespaces).isEmpty)
                .keyboardShortcut(.defaultAction)
                .buttonStyle(.borderedProminent)
            }
            if let err = engine.configError {
                Text(err).foregroundStyle(.red)
            }
            Spacer()
        }
        .padding(30)
        .frame(maxWidth: 640)
    }
}

/// Server address and API key. The key is masked unless revealed.
struct ServerFields: View {
    @Binding var url: String
    @Binding var key: String
    @Binding var reveal: Bool

    var body: some View {
        TextField("Server", text: $url, prompt: Text("https://gold.example.com"))
        HStack {
            if reveal {
                TextField("API key", text: $key, prompt: Text("gm_…"))
            } else {
                SecureField("API key", text: $key, prompt: Text("gm_…"))
            }
            Button(reveal ? "Hide" : "Show") { reveal.toggle() }
        }
    }
}

// MARK: - Status

struct StatusView: View {
    @EnvironmentObject var engine: Engine

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Button(engine.syncing ? "Uploading…" : "Upload Game Data") { Task { await engine.syncNow() } }
                    .disabled(engine.syncing)
                Button("Download Prices to Addon") { Task { await engine.pushNow() } }
                Spacer()
                if let url = engine.serverDisplayURL {
                    Link(destination: url) { Label(url.host ?? "server", systemImage: "server.rack") }
                }
                Label(engine.watching ? "Watching game data" : "Not watching", systemImage: engine.watching ? "eye" : "eye.slash")
                    .foregroundStyle(.secondary)
            }
            if let err = engine.configError {
                Text(err).foregroundStyle(.red)
            }
            if let s = engine.status {
                HStack(alignment: .top, spacing: 16) {
                    GroupBox {
                        Table(s.markets) {
                            TableColumn("Market", value: \.name)
                            TableColumn("Game") { Text($0.game ?? "") }.width(70)
                            TableColumn("Snapshots") { Text("\($0.snapshots)") }.width(70)
                            TableColumn("Items") { Text("\($0.items ?? 0)") }.width(60)
                            TableColumn("AH data from") { Text(relativeTime($0.last_modified)) }
                        }
                        .frame(minHeight: 120)
                    } label: {
                        HStack {
                            Text("Markets")
                            if let url = engine.serverDisplayURL {
                                Link("Manage on server", destination: url.appendingPathComponent("markets")).font(.caption)
                            }
                        }
                    }
                    GroupBox("Characters") {
                        Table(s.characters) {
                            TableColumn("Character", value: \.key)
                            TableColumn("Gold") { Text(formatGold($0.balance ?? 0)).monospacedDigit() }
                        }
                        .frame(minHeight: 120)
                    }
                }
                .frame(maxHeight: 200)
                Text("\(s.transactions) ledger entries · \(s.scans) AH scans · \(s.recipes) recipes · server market data \(ByteCountFormatter.string(fromByteCount: Int64(s.db_bytes), countStyle: .file))")
                    .font(.caption).foregroundStyle(.secondary)
            }
            GroupBox("Activity") {
                LogView(lines: engine.log)
            }
        }
    }
}

struct LogView: View {
    let lines: [LogLine]
    private static let time: DateFormatter = {
        let f = DateFormatter(); f.dateFormat = "HH:mm:ss"; return f
    }()

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    ForEach(lines.suffix(500)) { line in
                        Text("\(Self.time.string(from: line.date))  [\(line.source)]  \(line.text)")
                            .font(.system(.caption, design: .monospaced))
                            .foregroundStyle(line.isError ? .red : .primary)
                            .textSelection(.enabled)
                            .id(line.id)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .onChange(of: lines.count) { _ in
                if let last = lines.last { proxy.scrollTo(last.id, anchor: .bottom) }
            }
        }
    }
}

// MARK: - Reports

struct ReportsView: View {
    @EnvironmentObject var engine: Engine
    enum Kind: String, CaseIterable, Identifiable {
        case crafts = "Crafts", deals = "Deals", timing = "Timing", ledger = "Ledger", price = "Price"
        var id: String { rawValue }
    }
    @State private var kind: Kind = .crafts
    @State private var output = ""
    @State private var running = false
    @State private var itemQuery = ""
    @State private var days = 7
    @State private var timingResult: TimingResult?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Picker("Report", selection: $kind) {
                    ForEach(Kind.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                .frame(maxWidth: 360)
                if kind == .price || kind == .timing {
                    TextField("Item name or id", text: $itemQuery).frame(maxWidth: 220).onSubmit { Task { await runReport() } }
                }
                if kind == .ledger {
                    Stepper("Last \(days) days", value: $days, in: 1...365).frame(maxWidth: 160)
                }
                Button(running ? "Running…" : "Run") { Task { await runReport() } }.disabled(running)
                Spacer()
            }
            Text(caption).font(.caption).foregroundStyle(.secondary)
            if kind == .timing {
                TimingView(result: timingResult)
            } else {
                ScrollView([.vertical, .horizontal]) {
                    Text(output.isEmpty ? "Press Run." : output)
                        .font(.system(.body, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(6)
                }
                .background(Color(nsColor: .textBackgroundColor))
            }
        }
        .onChange(of: kind) { _ in output = ""; timingResult = nil }
    }

    private var caption: String {
        switch kind {
        case .crafts: return "Profit per craft for every recipe your characters know. Open each profession window in game to record recipes."
        case .deals: return "Items listed well below market value that actually sell. Needs a few hours of snapshots."
        case .ledger: return "Your gold in and out by source, from the Goldmine addon's ledger."
        case .price: return "Recent prices for one item across your markets."
        case .timing: return "When an item sells, by day and hour (your local time). Averages are over days it traded; patterns firm up after a week or two of collecting."
        }
    }

    private func runReport() async {
        running = true
        defer { running = false }
        switch kind {
        case .crafts: output = await engine.report(["crafts"])
        case .deals: output = await engine.report(["deals"])
        case .ledger: output = await engine.report(["summary", "--days", "\(days)"])
        case .price:
            guard !itemQuery.isEmpty else { output = "Enter an item name or id."; return }
            output = await engine.report(["price", itemQuery])
        case .timing:
            guard !itemQuery.isEmpty else { return }
            timingResult = await engine.timing(itemQuery)
        }
    }
}

// MARK: - Settings

struct SettingsView: View {
    @EnvironmentObject var engine: Engine
    @State private var saved = false
    @State private var reveal = false
    @State private var checking = false
    @State private var serverResult: (Bool, String)?
    @State private var confirmDisconnect = false

    var body: some View {
        Form {
            Section("Goldmine server") {
                ServerFields(url: $engine.settings.serverURL, key: $engine.settings.apiKey, reveal: $reveal)
                HStack {
                    Spacer()
                    Button(checking ? "Testing…" : "Test") {
                        Task {
                            checking = true
                            serverResult = await engine.checkServer(engine.settings.serverURL, key: engine.settings.apiKey)
                            checking = false
                        }
                    }
                    .disabled(checking || engine.settings.serverURL.isEmpty || engine.settings.apiKey.isEmpty)
                }
                if let (ok, text) = serverResult {
                    Label(text, systemImage: ok ? "checkmark.circle" : "xmark.octagon")
                        .foregroundStyle(ok ? .green : .red)
                }
                HStack {
                    Text("To switch servers or keys, paste a new address or API key (from the server's Clients page) and press Save.")
                        .font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    Button("Disconnect…", role: .destructive) { confirmDisconnect = true }
                }
            }
            Section("Background work") {
                Toggle("Upload game data when WoW saves it (logout, /reload)", isOn: $engine.settings.autoSync)
                Toggle("Download prices into the addon after each upload", isOn: $engine.settings.autoPush)
                Toggle("Launch Goldmine at login", isOn: Binding(get: { engine.launchAtLogin }, set: { engine.launchAtLogin = $0 }))
            }
            Section("Paths") {
                TextField("Python 3.11+", text: $engine.pythonPath, prompt: Text("automatic: \(engine.python)"))
                TextField("Goldmine folder", text: $engine.goldmineDir, prompt: Text("built in (development: a goldmine checkout)"))
                Text("Leave both empty normally. Applied immediately; press Save to restart background work with them.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                Spacer()
                if saved { Text("Saved").foregroundStyle(.green) }
                Button("Revert") { Task { await engine.loadConfig() } }
                Button("Save") {
                    Task {
                        engine.stopWatch()
                        saved = await engine.saveConfig()
                        try? await Task.sleep(nanoseconds: 2_000_000_000)
                        saved = false
                    }
                }
                .keyboardShortcut("s")
                .buttonStyle(.borderedProminent)
            }
        }
        .formStyle(.grouped)
        .confirmationDialog("Disconnect from \(engine.serverDisplayURL?.host ?? "the server")?", isPresented: $confirmDisconnect) {
            Button("Disconnect", role: .destructive) { Task { _ = await engine.connect("", key: "") } }
        } message: {
            Text("Goldmine stops uploading game data and updating the addon until you connect again. Your data stays on the server.")
        }
    }
}

// MARK: - Timing heatmap

struct TimingView: View {
    let result: TimingResult?
    private let days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                if let r = result {
                    Text("\(r.name) (\(r.item_id))").font(.headline)
                    if r.markets.isEmpty {
                        Text("No sales recorded for this item yet.").foregroundStyle(.secondary)
                    }
                    ForEach(r.markets) { m in heatmap(m) }
                } else {
                    Text("Enter an item name or id and press Run.").foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(6)
        }
    }

    @ViewBuilder
    private func heatmap(_ m: TimingMarket) -> some View {
        let peak = max(m.buckets.compactMap(\.sold_avg).max() ?? 0, 0.0001)
        VStack(alignment: .leading, spacing: 4) {
            Text("\(m.market)  ·  \(m.game ?? "")").font(.subheadline.bold())
            HStack(spacing: 2) {
                Text("").frame(width: 34)
                ForEach(0..<24, id: \.self) { h in
                    Text(h % 3 == 0 ? "\(h)" : "").font(.caption2).frame(width: 18)
                }
                Text("best hours").font(.caption2).padding(.leading, 8)
            }
            ForEach(0..<7, id: \.self) { d in
                HStack(spacing: 2) {
                    Text(days[d]).font(.caption).frame(width: 34, alignment: .leading)
                    ForEach(0..<24, id: \.self) { h in
                        let b = m.buckets[d * 24 + h]
                        RoundedRectangle(cornerRadius: 3)
                            .fill(b.samples == 0 ? Color.gray.opacity(0.12)
                                  : Color.orange.opacity(0.15 + 0.85 * (b.sold_avg ?? 0) / peak))
                            .frame(width: 18, height: 18)
                            .help(tip(d, h, b))
                    }
                    Text(best(m, d)).font(.caption).foregroundStyle(.secondary).padding(.leading, 8)
                }
            }
        }
    }

    private func tip(_ d: Int, _ h: Int, _ b: TimingBucket) -> String {
        guard b.samples > 0 else { return "\(days[d]) \(h):00 - no data" }
        var parts = ["\(days[d]) \(h):00", String(format: "%.1f sold/hr", b.sold_avg ?? 0)]
        if let p = b.p10_avg { parts.append("typical price \(formatGold(p))") }
        if let q = b.qty_avg { parts.append(String(format: "%.0f listed", q)) }
        parts.append("\(b.samples) samples")
        return parts.joined(separator: " · ")
    }

    private func best(_ m: TimingMarket, _ d: Int) -> String {
        let row = (0..<24).map { (h: $0, v: m.buckets[d * 24 + $0].sold_avg ?? 0) }.filter { $0.v > 0 }
        return row.sorted { $0.v > $1.v }.prefix(3).map { String(format: "%02dh", $0.h) }.joined(separator: ", ")
    }
}
