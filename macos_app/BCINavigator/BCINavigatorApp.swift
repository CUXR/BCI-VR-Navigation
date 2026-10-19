import SwiftData
import SwiftUI

@main
struct BCINavigatorApp: App {
    @State private var model = AppModel()

    /// The app is not sandboxed, so give the store its own folder instead of the shared default.store.
    private static let container: ModelContainer = {
        let folder = URL.applicationSupportDirectory.appending(path: "BCI Navigator", directoryHint: .isDirectory)
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let configuration = ModelConfiguration(url: folder.appending(path: "BCINavigator.store"))
        do {
            return try ModelContainer(for: Participant.self, SessionRecord.self, configurations: configuration)
        } catch {
            fatalError("Could not open the participant database: \(error)")
        }
    }()

    var body: some Scene {
        WindowGroup("BCI Navigator") {
            RootView()
                .environment(model)
                .frame(minWidth: 900, minHeight: 600)
                .preferredColorScheme(.light)
        }
        .defaultSize(width: 1200, height: 800)
        .modelContainer(Self.container)
        .commands {
            CommandGroup(after: .sidebar) {
                Button(model.mode == .admin ? "Exit Admin Mode" : "Admin Mode") {
                    model.mode = model.mode == .admin ? .user : .admin
                }
                .keyboardShortcut("a", modifiers: [.command, .shift])
            }
        }
    }
}

struct RootView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        switch model.mode {
        case .user: UserRootView()
        case .admin: AdminRootView()
        }
    }
}
