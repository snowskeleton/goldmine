import AppKit
import SwiftUI

final class AppDelegate: NSObject, NSApplicationDelegate {
    var engine: Engine?

    func applicationWillTerminate(_ notification: Notification) {
        MainActor.assumeIsolated { engine?.shutdown() }
    }
}

@main
struct GoldmineApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var engine = Engine()

    var body: some Scene {
        MenuBarExtra {
            MenuContent()
                .environmentObject(engine)
                .onAppear { delegate.engine = engine }
        } label: {
            Image(systemName: engine.busy ? "arrow.triangle.2.circlepath.circle" : "dollarsign.circle")
                .onAppear { delegate.engine = engine }
        }

        Window("Goldmine", id: "main") {
            MainView()
                .environmentObject(engine)
                .frame(minWidth: 760, minHeight: 520)
        }
        .defaultSize(width: 900, height: 620)
    }
}

struct MenuContent: View {
    @EnvironmentObject var engine: Engine
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        if let err = engine.configError {
            Text(err)
        }
        if let url = engine.serverDisplayURL {
            Text("Server: \(url.host ?? url.absoluteString)")
        } else {
            Text(collectLine)
        }
        Text(engine.watching ? "Watching SavedVariables" : "Not watching SavedVariables")
        Divider()
        if !engine.serverMode {
            Button(engine.collecting ? "Collecting…" : "Collect Prices Now") { Task { await engine.collectNow() } }
                .disabled(engine.collecting)
        }
        Button(engine.syncing ? "Syncing…" : "Sync Game Data Now") { Task { await engine.syncNow() } }
            .disabled(engine.syncing)
        Button(engine.serverMode ? "Download Prices to Addon" : "Push to Addon") { Task { await engine.pushNow() } }
        Divider()
        Button("Open Goldmine…") {
            openWindow(id: "main")
            NSApp.activate(ignoringOtherApps: true)
        }
        .keyboardShortcut("o")
        Divider()
        Button("Quit Goldmine") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }

    private var collectLine: String {
        guard let last = engine.lastCollect else {
            return engine.settings.autoCollect ? "Collecting every \(engine.settings.collectMinutes) min" : "Auto-collect off"
        }
        let rel = relativeTime(Int(last.timeIntervalSince1970))
        return "Last collect \(rel)\(engine.lastCollectOK ? "" : " (errors)")"
    }
}
