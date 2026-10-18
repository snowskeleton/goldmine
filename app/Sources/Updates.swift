import AppKit
import SwiftUI

// Keeps the WoW addon and this app up to date. goldmine.py does the network and file work
// (`addon status|install`, `app-update check|download`); this side schedules it, shows it, and
// swaps the app bundle, which has to happen after the app quits.

struct AddonRelease: Codable {
    let version: String?
    let hash: String?
    let source: String?
    let error: String?
}

struct AddonCopy: Codable, Identifiable {
    var id: String { path }
    let flavor: String
    let wow_dir: String
    let path: String
    let installed: Bool
    let linked: Bool
    let link_target: String?
    let version: String?
    let hash: String?
    let current: Bool

    var needsUpdate: Bool { installed && !linked && !current }
}

struct AddonStatus: Codable {
    let available: AddonRelease
    let copies: [AddonCopy]
    let wow_dirs: [String]
}

struct AppRelease: Codable {
    let version: String
    let build: Int
    let notes: String?
    let size: Int?
}

struct AppCheck: Codable {
    let available: Bool
    let release: AppRelease?
    let reason: String?
}

struct AppDownload: Codable {
    let path: String
    let version: String
    let build: Int
}

struct UpdateState {
    var addon: AddonStatus?
    var app: AppCheck?
    var lastCheck: Date?
    var checking = false
    var installingAddon = false
    var installingApp = false
    var appError: String?

    var addonUpdates: Int { addon?.copies.filter(\.needsUpdate).count ?? 0 }
    var appUpdate: AppRelease? { app?.available == true ? app?.release : nil }
}

extension Engine {
    static let checkInterval: TimeInterval = 6 * 3600

    var appVersion: String { Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "?" }
    var appBuild: Int { Int(Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "") ?? 0 }

    func startUpdateChecks() {
        updateTimer?.invalidate()
        updateTimer = Timer.scheduledTimer(withTimeInterval: Self.checkInterval, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.checkForUpdates(auto: true) }
        }
        Task {
            try? await Task.sleep(nanoseconds: 15_000_000_000)  // let startup work go first
            await checkForUpdates(auto: true)
        }
    }

    /// Refreshes what's installed and what's available; with auto, applies updates the settings allow.
    func checkForUpdates(auto: Bool = false) async {
        guard !updates.checking else { return }
        updates.checking = true
        await refreshAddonStatus()
        if serverMode {
            let (code, out) = await run(["app-update", "check", "--current-build", "\(appBuild)", "--json"],
                                        label: "update", quiet: true)
            if code == 0, let data = out.data(using: .utf8) {
                updates.app = try? JSONDecoder().decode(AppCheck.self, from: data)
            }
        } else {
            updates.app = nil
        }
        updates.lastCheck = Date()
        updates.checking = false

        guard auto else { return }
        if settings.autoUpdateAddon && updates.addonUpdates > 0 {
            await installAddon(auto: true)
        }
        if settings.autoUpdateApp, let rel = updates.appUpdate {
            if busy {
                append("update", "Goldmine \(rel.version) is available; installing after current work finishes")
                Task {
                    try? await Task.sleep(nanoseconds: 600_000_000_000)
                    await checkForUpdates(auto: true)
                }
                return
            }
            await installAppUpdate()
        }
    }

    func refreshAddonStatus() async {
        let (code, out) = await run(["addon", "status", "--json"], label: "addon", quiet: true)
        guard code == 0, let data = out.data(using: .utf8) else { return }
        updates.addon = try? JSONDecoder().decode(AddonStatus.self, from: data)
    }

    /// Installs or updates the addon. flavors nil: every installed copy (auto: only outdated ones).
    func installAddon(flavors: [String]? = nil, auto: Bool = false, force: Bool = false) async {
        guard !updates.installingAddon else { return }
        updates.installingAddon = true
        defer { updates.installingAddon = false }
        var args = ["addon", "install"]
        for f in flavors ?? [] { args += ["--flavor", f] }
        if auto { args.append("--auto") }
        if force { args.append("--force") }
        let (_, out) = await run(args, label: "addon")
        // A fresh install has no Data.lua yet; write one so tooltips work right away.
        if out.contains(": installed") && settings.autoPush { await pushNow() }
        await refreshAddonStatus()
    }

    // MARK: - App self-update

    private var updateDir: URL {
        FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent(Bundle.main.bundleIdentifier ?? "Goldmine")
    }

    /// Downloads the newer build, checks it, and replaces this app with it (quits and relaunches).
    func installAppUpdate() async {
        guard !updates.installingApp else { return }
        updates.installingApp = true
        updates.appError = nil
        defer { updates.installingApp = false }
        func fail(_ msg: String) {
            updates.appError = msg
            append("update", msg, error: true)
        }

        let target = Bundle.main.bundleURL
        guard target.pathExtension == "app",
              FileManager.default.isWritableFile(atPath: target.deletingLastPathComponent().path) else {
            return fail("can't update in place: \(target.deletingLastPathComponent().path) isn't writable. Move Goldmine to ~/Applications.")
        }
        let dest = updateDir.appendingPathComponent("new")
        let (code, out) = await run(["app-update", "download", "--current-build", "\(appBuild)",
                                     "--dest", dest.path, "--json"], label: "update", quiet: true)
        guard code == 0, let data = out.data(using: .utf8),
              let dl = try? JSONDecoder().decode(AppDownload.self, from: data) else {
            return fail("app update download failed")
        }
        let newApp = URL(fileURLWithPath: dl.path)
        guard let bundle = Bundle(url: newApp), bundle.bundleIdentifier == Bundle.main.bundleIdentifier,
              Int(bundle.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "") ?? 0 > appBuild else {
            return fail("the downloaded app isn't a newer Goldmine build; not installing it")
        }
        guard await codesignValid(newApp) else {
            return fail("the downloaded app's code signature doesn't verify; not installing it")
        }

        // The swap runs after we quit: wait for this process, move the new bundle into place
        // (restoring the old one if that fails), relaunch.
        let script = updateDir.appendingPathComponent("swap.sh")
        let sh = """
        #!/bin/sh
        pid="$1"; target="$2"; new="$3"; backup="$target.previous"
        while kill -0 "$pid" 2>/dev/null; do sleep 0.2; done
        rm -rf "$backup"
        mv "$target" "$backup" || { open "$target"; exit 1; }
        if mv "$new" "$target"; then rm -rf "$backup"; else mv "$backup" "$target"; fi
        xattr -dr com.apple.quarantine "$target" 2>/dev/null
        open "$target"
        """
        do {
            try sh.write(to: script, atomically: true, encoding: .utf8)
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/bin/sh")
            p.arguments = [script.path, "\(ProcessInfo.processInfo.processIdentifier)", target.path, newApp.path]
            p.standardOutput = FileHandle.nullDevice
            p.standardError = FileHandle.nullDevice
            try p.run()
        } catch {
            return fail("couldn't start the updater: \(error.localizedDescription)")
        }
        append("update", "installing Goldmine \(dl.version) (build \(dl.build)) and relaunching")
        shutdown()
        NSApp.terminate(nil)
    }

    private func codesignValid(_ url: URL) async -> Bool {
        await withCheckedContinuation { cont in
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/codesign")
            p.arguments = ["--verify", "--deep", "--strict", url.path]
            p.standardOutput = FileHandle.nullDevice
            p.standardError = FileHandle.nullDevice
            p.terminationHandler = { cont.resume(returning: $0.terminationStatus == 0) }
            do { try p.run() } catch { cont.resume(returning: false) }
        }
    }
}

// MARK: - Updates tab

struct UpdatesView: View {
    @EnvironmentObject var engine: Engine

    var body: some View {
        Form {
            Section("Goldmine app") {
                LabeledContent("Installed", value: "\(engine.appVersion) (build \(engine.appBuild))")
                if !engine.serverMode {
                    Text("App updates come from a Goldmine server. Connect one in Settings to get them.")
                        .font(.caption).foregroundStyle(.secondary)
                } else if let rel = engine.updates.appUpdate {
                    HStack {
                        Label("Goldmine \(rel.version) (build \(rel.build)) is available", systemImage: "arrow.down.circle.fill")
                            .foregroundStyle(.blue)
                        Spacer()
                        Button(engine.updates.installingApp ? "Installing…" : "Install and Relaunch") {
                            Task { await engine.installAppUpdate() }
                        }
                        .disabled(engine.updates.installingApp)
                    }
                    if let notes = rel.notes, !notes.isEmpty {
                        Text(notes).font(.caption).textSelection(.enabled)
                    }
                } else if engine.updates.app?.release == nil && engine.updates.lastCheck != nil {
                    Text("The server has no app release yet.").foregroundStyle(.secondary)
                } else if engine.updates.lastCheck != nil {
                    Label("Up to date", systemImage: "checkmark.circle").foregroundStyle(.green)
                }
                if let err = engine.updates.appError {
                    Text(err).foregroundStyle(.red).font(.caption)
                }
            }

            Section("WoW addon") {
                if let st = engine.updates.addon {
                    LabeledContent("Available", value: "\(st.available.version ?? "?") · \(String((st.available.hash ?? "").prefix(10)))")
                    if let src = st.available.source {
                        Text("From \(src)").font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                    }
                    if let err = st.available.error {
                        Text(err).foregroundStyle(.red).font(.caption)
                    }
                    if st.copies.isEmpty {
                        Text("No World of Warcraft install found in \(st.wow_dirs.joined(separator: ", ")).")
                            .foregroundStyle(.secondary)
                    }
                    ForEach(st.copies) { copy in
                        AddonCopyRow(copy: copy)
                    }
                } else {
                    Text(engine.updates.checking ? "Checking…" : "Not checked yet.").foregroundStyle(.secondary)
                }
            }

            Section("Automatic updates") {
                Toggle("Keep the addon up to date", isOn: $engine.settings.autoUpdateAddon)
                Toggle("Install app updates from the server", isOn: $engine.settings.autoUpdateApp)
                    .disabled(!engine.serverMode)
                Text("Checked at launch and every 6 hours. Linked development copies of the addon are never replaced. Press Save in Settings to keep changes to these.")
                    .font(.caption).foregroundStyle(.secondary)
            }

            HStack {
                if let last = engine.updates.lastCheck {
                    Text("Last checked \(relativeTime(Int(last.timeIntervalSince1970)))")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Button(engine.updates.checking ? "Checking…" : "Check Now") {
                    Task { await engine.checkForUpdates() }
                }
                .disabled(engine.updates.checking)
            }
        }
        .formStyle(.grouped)
        .task { if engine.updates.addon == nil { await engine.refreshAddonStatus() } }
    }
}

struct AddonCopyRow: View {
    @EnvironmentObject var engine: Engine
    let copy: AddonCopy

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(copy.flavor).font(.body.monospaced())
                Text(detail).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
            }
            Spacer()
            state
        }
    }

    private var detail: String {
        if copy.linked { return "linked to \(copy.link_target ?? "?")" }
        return copy.path
    }

    @ViewBuilder private var state: some View {
        let busy = engine.updates.installingAddon
        if copy.linked {
            Label("Development link", systemImage: "link").foregroundStyle(.secondary)
        } else if !copy.installed {
            Button("Install") { Task { await engine.installAddon(flavors: [copy.flavor]) } }.disabled(busy)
        } else if copy.current {
            Label("Current\(copy.version.map { " (\($0))" } ?? "")", systemImage: "checkmark.circle").foregroundStyle(.green)
        } else {
            Text("Outdated\(copy.version.map { " (\($0))" } ?? "")").foregroundStyle(.orange)
            Button("Update") { Task { await engine.installAddon(flavors: [copy.flavor]) } }.disabled(busy)
        }
    }
}
