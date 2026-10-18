import SwiftUI

struct MainView: View {
    var body: some View {
        TabView {
            StatusView().tabItem { Label("Status", systemImage: "gauge") }
            ReportsView().tabItem { Label("Reports", systemImage: "chart.bar") }
            MarketsView().tabItem { Label("Markets", systemImage: "building.columns") }
            SettingsView().tabItem { Label("Settings", systemImage: "gearshape") }
        }
        .padding()
    }
}

// MARK: - Status

struct StatusView: View {
    @EnvironmentObject var engine: Engine

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                if !engine.serverMode {
                    Button(engine.collecting ? "Collecting…" : "Collect Now") { Task { await engine.collectNow() } }
                        .disabled(engine.collecting)
                }
                Button(engine.syncing ? "Syncing…" : "Sync Now") { Task { await engine.syncNow() } }
                    .disabled(engine.syncing)
                Button("Push to Addon") { Task { await engine.pushNow() } }
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
                    GroupBox("Markets") {
                        Table(s.markets) {
                            TableColumn("Market", value: \.name)
                            TableColumn("Game") { Text($0.game ?? "") }.width(70)
                            TableColumn("Snapshots") { Text("\($0.snapshots)") }.width(70)
                            TableColumn("Items") { Text("\($0.items ?? 0)") }.width(60)
                            TableColumn("AH data from") { Text(relativeTime($0.last_modified)) }
                        }
                        .frame(minHeight: 120)
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
                Text("\(s.transactions) ledger entries · \(s.scans) AH scans · \(s.recipes) recipes · database \(ByteCountFormatter.string(fromByteCount: Int64(s.db_bytes), countStyle: .file))")
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

// MARK: - Markets

struct MarketsView: View {
    @EnvironmentObject var engine: Engine
    @State private var browsing = false
    @State private var saved = false

    var body: some View {
        if engine.serverMode {
            VStack(alignment: .leading, spacing: 12) {
                Text("Markets are managed on your Goldmine server.").font(.headline)
                Text("The server collects prices for every configured market; add or remove them in its web interface.")
                    .foregroundStyle(.secondary)
                if let url = engine.serverDisplayURL {
                    Link("Open \(url.host ?? "server") in your browser", destination: url.appendingPathComponent("markets"))
                }
                Spacer()
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        } else {
            VStack(alignment: .leading, spacing: 10) {
                Text("Each market is one auction house to collect hourly. Forever's AH is shared per region and realm type, so one market per type you play is enough.")
                    .font(.caption).foregroundStyle(.secondary)
                List {
                    ForEach($engine.settings.markets) { $m in
                        HStack {
                            TextField("name", text: $m.name).frame(width: 170)
                            Picker("", selection: $m.kind) {
                                Text("commodities").tag("commodities")
                                Text("realm").tag("realm")
                                Text("classic (per faction)").tag("classic")
                            }
                            .frame(width: 170)
                            TextField("region", text: $m.region).frame(width: 40)
                            TextField("namespace", text: $m.namespace).frame(width: 170)
                            TextField("realm slug", text: $m.realm).frame(width: 130)
                                .disabled(m.kind == "commodities")
                            TextField("game (auto)", text: $m.game).frame(width: 90)
                                .help("Game version label. Leave empty to derive it from the namespace (retail, classic, ...). Set it for Forever if its namespace is ambiguous.")
                            Toggle("Pricing", isOn: $m.pricing)
                                .help("Use this market's prices for tooltips and reports. Turn off for other game versions whose item ids overlap.")
                            Button(role: .destructive) {
                                engine.settings.markets.removeAll { $0.id == m.id }
                            } label: { Image(systemName: "trash") }
                            .buttonStyle(.borderless)
                        }
                    }
                }
                HStack {
                    Button("Add Market") { engine.settings.markets.append(MarketConfig()) }
                    Button("Browse Realms…") { browsing = true }
                    Button("Probe Namespaces") { Task { await engine.run(["probe", "us"], label: "probe") } }
                        .help("Checks which API namespaces exist (shown in the Status tab's activity log). Use on Forever launch day.")
                    Spacer()
                    if saved { Text("Saved").foregroundStyle(.green) }
                    Button("Revert") { Task { await engine.loadConfig() } }
                    Button("Save") {
                        Task { saved = await engine.saveConfig(); try? await Task.sleep(nanoseconds: 2_000_000_000); saved = false }
                    }
                    .keyboardShortcut("s")
                    .buttonStyle(.borderedProminent)
                }
            }
            .sheet(isPresented: $browsing) { RealmBrowser().environmentObject(engine) }
        }
    }
}

struct RealmBrowser: View {
    @EnvironmentObject var engine: Engine
    @Environment(\.dismiss) private var dismiss
    @State private var region = "us"
    @State private var namespace = "dynamic-us"
    @State private var realms: [RealmInfo] = []
    @State private var loading = false
    @State private var filter = ""
    @State private var selection: RealmInfo.ID?

    private let namespaces = ["dynamic-us", "dynamic-classic-us", "dynamic-classic1x-us", "dynamic-classicann-us"]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Browse Realms").font(.headline)
            HStack {
                Picker("Region", selection: $region) {
                    ForEach(["us", "eu", "kr", "tw"], id: \.self) { Text($0) }
                }.frame(width: 120)
                TextField("namespace", text: $namespace).frame(width: 200)
                Menu("Known") {
                    ForEach(namespaces, id: \.self) { ns in
                        Button(ns) { namespace = ns.replacingOccurrences(of: "-us", with: "-\(region)") }
                    }
                }.frame(width: 80)
                Button(loading ? "Loading…" : "Load") {
                    Task { loading = true; realms = await engine.loadRealms(region: region, namespace: namespace); loading = false }
                }.disabled(loading)
                TextField("Filter", text: $filter)
            }
            Table(filtered, selection: $selection) {
                TableColumn("Realm") { Text($0.name ?? "") }
                TableColumn("Slug") { Text($0.slug ?? "") }
                TableColumn("Type") { Text($0.type ?? "") }.width(70)
                TableColumn("Category") { Text($0.category ?? "") }
                TableColumn("Group") { Text("\($0.connected_realm_id)") }.width(60)
            }
            HStack {
                Button("Add Region Commodities") {
                    engine.settings.markets.append(MarketConfig(name: "\(region)-commodities", region: region,
                                                                namespace: namespace, kind: "commodities"))
                    dismiss()
                }
                Spacer()
                Button("Cancel") { dismiss() }
                Button("Add Selected Realm") {
                    if let r = realms.first(where: { $0.id == selection }), let slug = r.slug {
                        engine.settings.markets.append(MarketConfig(name: "\(region)-\(slug)", region: region,
                                                                    namespace: namespace, kind: "realm", realm: slug))
                    }
                    dismiss()
                }
                .disabled(selection == nil)
                .buttonStyle(.borderedProminent)
            }
            Text("Added markets aren't saved until you press Save.").font(.caption).foregroundStyle(.secondary)
        }
        .padding()
        .frame(width: 760, height: 480)
    }

    private var filtered: [RealmInfo] {
        filter.isEmpty ? realms : realms.filter {
            ($0.name ?? "").localizedCaseInsensitiveContains(filter) || ($0.slug ?? "").localizedCaseInsensitiveContains(filter)
        }
    }
}

// MARK: - Settings

struct SettingsView: View {
    @EnvironmentObject var engine: Engine
    @State private var saved = false
    @State private var showSecret = false
    @State private var checking = false
    @State private var serverResult: (Bool, String)?

    var body: some View {
        Form {
            Section("Goldmine server") {
                HStack {
                    if showSecret {
                        TextField("Connection URL", text: $engine.settings.serverURL, prompt: Text("https://gm_…@gold.example.com"))
                    } else {
                        SecureField("Connection URL", text: $engine.settings.serverURL, prompt: Text("https://gm_…@gold.example.com"))
                    }
                    Button(checking ? "Testing…" : "Test") {
                        Task {
                            checking = true
                            serverResult = await engine.checkServer(engine.settings.serverURL)
                            checking = false
                        }
                    }
                    .disabled(checking || engine.settings.serverURL.isEmpty)
                }
                if let (ok, text) = serverResult {
                    Label(text, systemImage: ok ? "checkmark.circle" : "xmark.octagon")
                        .foregroundStyle(ok ? .green : .red)
                }
                Text("Generate a connection URL on your server's Clients page. With a server set, it collects prices and stores your data; this app uploads game data when WoW saves it and downloads prices for the addon. Leave empty to run everything on this Mac.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Blizzard API (local mode)") {
                TextField("Client ID", text: $engine.settings.clientId)
                HStack {
                    if showSecret {
                        TextField("Client secret", text: $engine.settings.clientSecret)
                    } else {
                        SecureField("Client secret", text: $engine.settings.clientSecret)
                    }
                    Button(showSecret ? "Hide" : "Show") { showSecret.toggle() }
                }
                Link("Manage API clients", destination: URL(string: "https://develop.battle.net/access/clients")!)
            }
            Section("Background work") {
                Toggle("Collect auction prices automatically", isOn: $engine.settings.autoCollect)
                    .disabled(engine.serverMode)
                    .help(engine.serverMode ? "The server collects prices." : "")
                Stepper("Every \(engine.settings.collectMinutes) minutes", value: $engine.settings.collectMinutes, in: 5...180, step: 5)
                    .disabled(!engine.settings.autoCollect || engine.serverMode)
                Toggle("Import game data when WoW saves it (logout, /reload)", isOn: $engine.settings.autoSync)
                Toggle("Update the addon's Data.lua after each collect and import", isOn: $engine.settings.autoPush)
                Toggle("Launch Goldmine at login", isOn: Binding(get: { engine.launchAtLogin }, set: { engine.launchAtLogin = $0 }))
            }
            Section("Storage") {
                if engine.serverMode {
                    Text("All data is stored on the server.").font(.caption).foregroundStyle(.secondary)
                } else if let s = engine.status {
                    Text("Database: \(s.db_path)").font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                }
            }
            Section("Paths") {
                TextField("Python 3.11+", text: $engine.pythonPath)
                TextField("Goldmine folder", text: $engine.goldmineDir)
                Text("Applied immediately; press Save to restart background work with them.")
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
