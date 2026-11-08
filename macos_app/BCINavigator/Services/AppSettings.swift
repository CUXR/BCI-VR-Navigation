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
    /// Confidence (0 to 1) a left or right prediction needs before it is sent to the VR app.
    var miThreshold: Double {
        didSet { defaults.set(miThreshold, forKey: "miThreshold") }
    }
    /// Confidence (0 to 1) an intentional blink needs before it is sent to the VR app.
    var blinkThreshold: Double {
        didSet { defaults.set(blinkThreshold, forKey: "blinkThreshold") }
    }

    init() {
        let repo = defaults.string(forKey: "repoPath") ?? Self.detectedRepoPath
        repoPath = repo
        pythonPath = defaults.string(forKey: "pythonPath")
            ?? URL(fileURLWithPath: repo).appending(path: ".venv/bin/python").path
        museSerial = defaults.string(forKey: "museSerial") ?? ""
        // Defaults match eeg_to_meta/config.py.
        miThreshold = defaults.object(forKey: "miThreshold") as? Double ?? 0.55
        blinkThreshold = defaults.object(forKey: "blinkThreshold") as? Double ?? 0.60
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
        repoURL.appending(path: "macos_app/backend/muse_session_recorder.py")
    }
    var realtimeScriptURL: URL {
        repoURL.appending(path: "macos_app/backend/realtime_control.py")
    }
    var trainingScriptURL: URL {
        repoURL.appending(path: "macos_app/backend/train_personal_model.py")
    }
    func personalModelURL(participant: Int) -> URL {
        repoURL.appending(path: String(format: "ml_pipeline/models/participants/sub%02d_app.pkl", participant))
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
