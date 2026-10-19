import Foundation
import Observation

/// Runs the PsychoPy recorder as a child process and tracks progress from its stdout.
@Observable
final class RecorderRunner {
    enum Phase: Equatable {
        case idle
        case starting
        case running
        case finished(saved: Bool)
        case failed(String)
    }

    static let imageryTarget = 20
    static let blinkTarget = 10

    private(set) var phase: Phase = .idle
    private(set) var startedAt: Date?
    private(set) var endedAt: Date?
    private(set) var imageryDone = 0
    private(set) var blinksDone = 0
    /// Spoken cue for the imagery trial in progress, e.g. "Grab left".
    private(set) var currentCue: String?
    private(set) var eegFile: String?
    private(set) var trialLogFile: String?
    /// Set when the recorder reports a partial recording (reader or disk failure).
    private(set) var incompleteReason: String?
    /// Last lines of recorder output, kept for error reports.
    private(set) var logTail: [String] = []

    private var process: Process?
    private var currentTrialType: String?
    private var pending = ""

    var isRunning: Bool { phase == .starting || phase == .running }
    var imageryRound: Int { min(imageryDone / 2 + 1, Self.imageryTarget / 2) }

    func start(settings: AppSettings, participant: Participant, headband: MuseConnection) {
        guard !isRunning else { return }
        reset()
        phase = .starting

        let fm = FileManager.default
        guard fm.isExecutableFile(atPath: settings.pythonPath) else {
            phase = .failed("Python was not found at \(settings.pythonPath). Ask the research team to check Settings.")
            return
        }
        guard fm.fileExists(atPath: settings.recorderScriptURL.path) else {
            phase = .failed("The recorder script was not found in \(settings.repoPath). Ask the research team to check Settings.")
            return
        }

        var arguments = [
            settings.recorderScriptURL.path,
            "--name", participant.fullName,
            "--number", String(participant.subjectNumber),
        ]
        let serial = settings.museSerial.trimmingCharacters(in: .whitespaces)
        if !serial.isEmpty { arguments += ["--serial", serial] }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: settings.pythonPath)
        process.arguments = arguments
        // The recorder writes to the relative path data/subNN, so run from the repo root.
        process.currentDirectoryURL = settings.repoURL
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        process.environment = environment

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            Task { @MainActor in self?.consume(text) }
        }
        process.terminationHandler = { [weak self] finished in
            let status = finished.terminationStatus
            pipe.fileHandleForReading.readabilityHandler = nil
            Task { @MainActor in self?.finish(status: status) }
        }

        Task {
            // Keep navigation locked while the single Bluetooth connection is released.
            await headband.stopAndWait()
            do {
                try process.run()
                self.process = process
                startedAt = .now
                phase = .running
            } catch {
                phase = .failed("Could not start the recorder: \(error.localizedDescription)")
            }
        }
    }

    /// Ends the process without letting the recorder save. Pressing Esc in the
    /// recording window is the way to stop and keep data.
    func forceStop() {
        process?.terminate()
    }

    func reset() {
        phase = .idle
        startedAt = nil
        endedAt = nil
        imageryDone = 0
        blinksDone = 0
        currentCue = nil
        eegFile = nil
        trialLogFile = nil
        incompleteReason = nil
        logTail = []
        currentTrialType = nil
        pending = ""
    }

    // MARK: - Output parsing

    private func consume(_ text: String) {
        pending += text
        while let newline = pending.firstIndex(of: "\n") {
            let line = String(pending[..<newline]).trimmingCharacters(in: .whitespaces)
            pending = String(pending[pending.index(after: newline)...])
            handle(line)
        }
    }

    private func handle(_ line: String) {
        logTail.append(line)
        if logTail.count > 200 { logTail.removeFirst(logTail.count - 200) }

        if line.hasPrefix("Started trial:"), let type = Self.parenthesized(line) {
            currentTrialType = type
            switch type {
            case "motor_imagery_left": currentCue = "Grab left"
            case "motor_imagery_right": currentCue = "Grab right"
            default: currentCue = nil
            }
        } else if line.hasPrefix("Ended trial:") {
            if currentTrialType?.hasPrefix("motor_imagery") == true { imageryDone += 1 }
            currentTrialType = nil
        } else if line.hasPrefix("Blink marker added:") {
            blinksDone += 1
        } else if line.hasPrefix("Recording saved to") {
            eegFile = Self.path(after: "Recording saved to", in: line)
        } else if line.hasPrefix("RECORDING INCOMPLETE:") {
            incompleteReason = Self.path(after: "RECORDING INCOMPLETE:", in: line)
        } else if line.hasPrefix("Recording stopped. Data saved to") {
            // Recorder output before the signal-quality rewrite.
            eegFile = Self.path(after: "Data saved to", in: line)
        } else if line.hasPrefix("Trial log saved to") {
            trialLogFile = Self.path(after: "Trial log saved to", in: line)
        }
    }

    private func finish(status: Int32) {
        if !pending.isEmpty { handle(pending); pending = "" }
        endedAt = .now
        process = nil
        currentCue = nil
        if let incompleteReason {
            phase = .failed("The recording is incomplete: \(incompleteReason). Ask the research team to check the files before trying again.")
        } else if eegFile != nil {
            phase = .finished(saved: true)
        } else if status == 0 || status == 15 {
            phase = .finished(saved: false)
        } else {
            phase = .failed("The recorder stopped with an error (code \(status)).")
        }
    }

    private static func parenthesized(_ line: String) -> String? {
        guard let open = line.lastIndex(of: "("), let close = line.lastIndex(of: ")"), open < close else { return nil }
        return String(line[line.index(after: open)..<close])
    }

    private static func path(after marker: String, in line: String) -> String? {
        guard let range = line.range(of: marker) else { return nil }
        let value = line[range.upperBound...].trimmingCharacters(in: .whitespaces)
        return value.isEmpty ? nil : value
    }
}
