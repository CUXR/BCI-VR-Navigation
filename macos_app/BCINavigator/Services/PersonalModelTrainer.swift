import Foundation
import Observation

/// Fits the signed-in participant's saved recordings in a separate Python process.
@Observable
final class PersonalModelTrainer {
    enum Phase: Equatable {
        case idle, training, cancelling, cancelled
        case trained(String)
        case failed(String)
    }

    private(set) var phase: Phase = .idle
    private(set) var modelURL: URL?
    private(set) var logTail: [String] = []
    private var process: Process?

    var isRunning: Bool { phase == .training || phase == .cancelling }

    func refresh(settings: AppSettings, participant: Int) {
        guard !isRunning else { return }
        let url = settings.personalModelURL(participant: participant)
        modelURL = FileManager.default.fileExists(atPath: url.path) ? url : nil
    }

    func reset() {
        guard !isRunning else { return }
        phase = .idle
        modelURL = nil
        logTail = []
    }

    func start(settings: AppSettings, participant: Int) {
        guard !isRunning else { return }
        guard FileManager.default.isExecutableFile(atPath: settings.pythonPath),
              FileManager.default.fileExists(atPath: settings.trainingScriptURL.path) else {
            phase = .failed("The training helper or Python was not found. Ask the research team to check Settings.")
            return
        }
        let target = settings.personalModelURL(participant: participant)
        let process = Process()
        process.executableURL = URL(fileURLWithPath: settings.pythonPath)
        process.arguments = [settings.trainingScriptURL.path, "--participant", String(participant),
                             "--data-root", settings.dataURL.path,
                             "--output-dir", target.deletingLastPathComponent().path]
        process.currentDirectoryURL = settings.repoURL
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        process.environment = environment
        process.standardInput = FileHandle.nullDevice
        let out = Pipe()
        let err = Pipe()
        process.standardOutput = out
        process.standardError = err
        err.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            Task { @MainActor in
                guard let self else { return }
                self.logTail.append(contentsOf: text.split(separator: "\n").map(String.init))
                if self.logTail.count > 100 { self.logTail.removeFirst(self.logTail.count - 100) }
            }
        }
        // The helper writes one small result. Drain it after exit so a fast helper's
        // final JSON cannot race the process termination callback.
        process.terminationHandler = { [weak self] finished in
            let data = out.fileHandleForReading.readDataToEndOfFile()
            err.fileHandleForReading.readabilityHandler = nil
            let status = finished.terminationStatus
            Task { @MainActor in self?.finish(status: status, data: data, target: target) }
        }
        phase = .training
        logTail = []
        do {
            try process.run()
            self.process = process
        } catch {
            err.fileHandleForReading.readabilityHandler = nil
            phase = .failed("Could not start training: \(error.localizedDescription)")
        }
    }

    func cancel() {
        guard phase == .training, let process else { return }
        phase = .cancelling
        if process.isRunning { process.terminate() }
        Task {
            try? await Task.sleep(for: .seconds(3))
            if process.isRunning { kill(process.processIdentifier, SIGKILL) }
        }
    }

    private func finish(status: Int32, data: Data, target: URL) {
        process = nil
        modelURL = FileManager.default.fileExists(atPath: target.path) ? target : nil
        let result = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        // If the atomic save completed just before Cancel, report that success honestly.
        if status == 0, result?["event"] as? String == "trained", modelURL != nil {
            let counts = result?["mi_class_counts"] as? [String: Int] ?? [:]
            let hands = (counts["left_motor_imagery"] ?? 0) + (counts["right_motor_imagery"] ?? 0)
            let blinks = result?["n_blink_pos"] as? Int ?? 0
            if let walking = counts["mi_forward"] {
                phase = .trained("Trained on \(hands) hand trials, \(walking) walking trials, \(counts["idle"] ?? 0) relax trials and \(blinks) blinks. Ready to use.")
            } else {
                phase = .trained("Trained on \(hands) hand trials and \(blinks) blinks. Ready to use.")
            }
        } else if phase == .cancelling {
            phase = .cancelled
        } else {
            phase = .failed(result?["message"] as? String
                            ?? "Training stopped with an error (code \(status)). Ask the research team to check the Python environment.")
        }
    }
}
