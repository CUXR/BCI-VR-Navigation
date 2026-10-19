import Foundation
import Observation

/// Paths and device options, persisted in UserDefaults and edited in admin Settings.
@Observable
final class AppSettings {
    private let defaults = UserDefaults.standard

    var repoPath: String {
        didSet { defaults.set(repoPath, forKey: "repoPath") }
    }
    var pythonPath: String {
        didSet { defaults.set(pythonPath, forKey: "pythonPath") }
    }
    /// Muse Bluetooth name such as `Muse-15C3`. Empty lets the recorder show its own device picker.
    var museSerial: String {
        didSet { defaults.set(museSerial, forKey: "museSerial") }
    }

    init() {
        let repo = defaults.string(forKey: "repoPath") ?? Self.detectedRepoPath
        repoPath = repo
        pythonPath = defaults.string(forKey: "pythonPath")
            ?? URL(fileURLWithPath: repo).appending(path: ".venv/bin/python").path
        museSerial = defaults.string(forKey: "museSerial") ?? ""
    }

    /// The lab's two headbands. Their Bluetooth names match the recorder's device list.
    static let headbands = ["Muse-15C3", "Muse-12A6"]

    /// The last four characters of the serial, which is what is printed on the band.
    static func shortCode(_ serial: String) -> String { String(serial.suffix(4)) }

    var repoURL: URL { URL(fileURLWithPath: repoPath, isDirectory: true) }
    var dataURL: URL { repoURL.appending(path: "data", directoryHint: .isDirectory) }
    var signalCheckScriptURL: URL {
        repoURL.appending(path: "macos_app/backend/muse_signal_check.py")
    }
    var recorderScriptURL: URL {
        repoURL.appending(path: "src/pilot_data_collection/psychopy_recording/muse_psychopy_recording_structured.py")
    }

    /// Development default: the repo checkout this source file was compiled from
    /// (`<repo>/macos_app/BCINavigator/Services/AppSettings.swift`).
    private static var detectedRepoPath: String {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()  // Services
            .deletingLastPathComponent()  // BCINavigator
            .deletingLastPathComponent()  // macos_app
            .deletingLastPathComponent()  // repo
            .path
    }
}
