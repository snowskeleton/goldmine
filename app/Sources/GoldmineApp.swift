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
            Text(engine.watching ? "Uploading game data as WoW saves it" : "Not watching game data")
        } else {
            Text("Not connected to a server")
            Button("Connect…") {
                openWindow(id: "main")
                NSApp.activate(ignoringOtherApps: true)
            }
        }
        if let rel = engine.updates.appUpdate {
            Divider()
            Button("Install Goldmine \(rel.version) and Relaunch") { Task { await engine.installAppUpdate() } }
                .disabled(engine.updates.installingApp)
        }
        if engine.updates.addonUpdates > 0 {
            Button("Update WoW Addon") { Task { await engine.installAddon() } }
                .disabled(engine.updates.installingAddon)
        }
        if engine.serverMode {
            Divider()
            Button(engine.syncing ? "Uploading…" : "Upload Game Data Now") { Task { await engine.syncNow() } }
                .disabled(engine.syncing)
            Button("Download Prices to Addon") { Task { await engine.pushNow() } }
        }
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
}
