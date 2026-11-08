import AppKit
import Observation

/// Opens the desktop Unity environment, which connects to the live prediction server itself.
@Observable
final class VREnvironmentLauncher {
    private(set) var isOpening = false
    private(set) var problem: String?

    func open(settings: AppSettings) async {
        guard !isOpening else { return }
        isOpening = true
        problem = nil
        defer { isOpening = false }

        let imageURL = settings.repoURL.appending(path: "releases/environment_1/BCIIndoor-mac.dmg")
        do {
            // Disk image verification/mounting must not block the live feedback UI.
            let application = try await Task.detached {
                try Self.applicationURL(imageURL: imageURL)
            }.value
            _ = try await NSWorkspace.shared.openApplication(
                at: application, configuration: NSWorkspace.OpenConfiguration())
        } catch {
            problem = "Could not open the VR environment. \(error.localizedDescription)"
        }
    }

    nonisolated static func applicationURL(imageURL: URL) throws -> URL {
        let fm = FileManager.default
        let installed = [
            fm.homeDirectoryForCurrentUser.appending(path: "Applications/BCI Indoor.app"),
            URL(fileURLWithPath: "/Applications/BCI Indoor.app"),
        ]
        if let application = installed.first(where: { fm.fileExists(atPath: $0.path) }) {
            return application
        }
        guard fm.fileExists(atPath: imageURL.path) else {
            throw NSError(domain: "VREnvironmentLauncher", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "The environment DMG is missing from the repository. Ask the research team to check Settings."
            ])
        }

        let mount = Process()
        mount.executableURL = URL(fileURLWithPath: "/usr/bin/hdiutil")
        mount.arguments = ["attach", "-readonly", "-nobrowse", "-plist", imageURL.path]
        let output = Pipe()
        mount.standardOutput = output
        mount.standardError = FileHandle.nullDevice
        mount.standardInput = FileHandle.nullDevice
        try mount.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        mount.waitUntilExit()
        guard mount.terminationStatus == 0 else {
            throw NSError(domain: "VREnvironmentLauncher", code: 2, userInfo: [
                NSLocalizedDescriptionKey: "The environment DMG could not be mounted (code \(mount.terminationStatus)). Try opening it in Finder."
            ])
        }
        let info = try PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any]
        let entities = info?["system-entities"] as? [[String: Any]] ?? []
        for entity in entities {
            guard let path = entity["mount-point"] as? String else { continue }
            let application = URL(fileURLWithPath: path).appending(path: "BCI Indoor.app")
            if fm.fileExists(atPath: application.path) { return application }
        }
        throw NSError(domain: "VREnvironmentLauncher", code: 3, userInfo: [
            NSLocalizedDescriptionKey: "BCI Indoor.app was not found in the environment DMG."
        ])
    }
}
