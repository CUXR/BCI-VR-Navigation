import Foundation
import Observation

enum Sensor: String, CaseIterable, Identifiable {
    case tp9 = "TP9", af7 = "AF7", af8 = "AF8", tp10 = "TP10"
    var id: String { rawValue }

    var place: String {
        switch self {
        case .tp9: "Behind left ear"
        case .af7: "Left forehead"
        case .af8: "Right forehead"
        case .tp10: "Behind right ear"
        }
    }

    /// One-line fix for the fit alert, tailored to the quality monitor's reason codes.
    func quickFix(reasons: [String]) -> String {
        if reasons.contains("line_noise") {
            return "Electrical noise on the \(place.lowercased()) sensor. Move away from chargers and power cables, then press the sensor to your skin."
        }
        return quickFix
    }

    /// One-line fix shown in the fit alert.
    var quickFix: String {
        switch self {
        case .tp9: "Tuck the left ear piece behind your ear so the sensor touches bare skin."
        case .tp10: "Tuck the right ear piece behind your ear so the sensor touches bare skin."
        case .af7: "Press the band flat against your forehead above your left eyebrow."
        case .af8: "Press the band flat against your forehead above your right eyebrow."
        }
    }

    var fixTitle: String {
        switch self {
        case .tp9: "Fix the left ear sensor"
        case .tp10: "Fix the right ear sensor"
        case .af7: "Fix the left forehead sensor"
        case .af8: "Fix the right forehead sensor"
        }
    }

    var fixSteps: [String] {
        switch self {
        case .tp9, .tp10:
            let side = self == .tp9 ? "left" : "right"
            return [
                "Lift the \(side) arm of the band and tuck it behind your ear.",
                "Make sure the sensor sits on bare skin, not on hair.",
                "Hold still for about five seconds.",
            ]
        case .af7, .af8:
            let side = self == .af7 ? "left" : "right"
            return [
                "Move any hair away from your forehead.",
                "Press the band flat above your \(side) eyebrow.",
                "Hold still for about five seconds.",
            ]
        }
    }
}

enum SensorQuality: Int, Comparable {
    /// No reading yet: the headband is not connected or has not sent data.
    case unknown = -1
    case poor = 0, adjust = 1, good = 2

    static func < (lhs: SensorQuality, rhs: SensorQuality) -> Bool { lhs.rawValue < rhs.rawValue }

    init(wire: String) {
        switch wire {
        case "good": self = .good
        case "fair": self = .adjust
        case "poor": self = .poor
        default: self = .unknown
        }
    }

    var label: String {
        switch self {
        case .good: "Good"
        case .adjust: "Adjust"
        case .poor: "Poor signal"
        case .unknown: "Waiting"
        }
    }
}

/// Plain-language version of the quality monitor's reason codes (docs/signal-quality.md).
enum QualityReason {
    static func text(_ code: String) -> String? {
        switch code {
        case "saturation": "signal maxed out"
        case "flat_line": "no signal"
        case "high_amplitude", "ear_noise_gate": "too noisy"
        case "kurtosis_artifact", "motion_artifact": "movement or blink noise"
        case "drl_disconnected", "all_sensors_bad": "reference not touching"
        case "line_noise": "electrical noise"
        case "invalid_values", "out_of_range", "nan_alert": "bad reading"
        default: nil
        }
    }

    static func summary(_ codes: [String]) -> String? {
        let parts = codes.compactMap(text)
        return parts.isEmpty ? nil : parts.joined(separator: ", ")
    }
}

enum HeadbandConnection: Equatable {
    case disconnected
    case connecting(attempt: Int)
    case connected
    /// Connected, but no samples for a few seconds.
    case signalLost
    case failed(String)

    var label: String {
        switch self {
        case .disconnected: "Not connected"
        case .connecting: "Connecting…"
        case .connected: "Connected"
        case .signalLost: "Signal lost"
        case .failed: "Connection failed"
        }
    }

    var isLive: Bool { self == .connected }
}

/// Connects to the selected Muse through `macos_app/backend/muse_signal_check.py` (BrainFlow)
/// and publishes the connection state and per-electrode contact quality it reports.
@Observable
final class MuseConnection {
    private(set) var connection: HeadbandConnection = .disconnected
    private(set) var serial: String?
    private(set) var qualities: [Sensor: SensorQuality] = [:]
    /// Mean-centered RMS per electrode in microvolts, from the quality monitor.
    private(set) var noise: [Sensor: Double] = [:]
    /// The quality monitor's reason codes per electrode, e.g. `saturation`, `line_noise`.
    private(set) var reasons: [Sensor: [String]] = [:]
    /// 0 to 1: progress through the three-second EEG readiness check.
    private(set) var readyProgress: [Sensor: Double] = [:]
    private(set) var referenceStatus = "unavailable"

    var referenceNotice: String? {
        guard connection.isLive, referenceStatus != "connected", referenceStatus != "disconnected" else { return nil }
        return "Reference contact is unverified. Readiness below checks the four EEG signals only."
    }
    private(set) var sampleRate: Double?
    private(set) var logTail: [String] = []

    private var process: Process?
    private var pending = ""
    private var log: FileHandle?

    /// Every helper line is kept in `~/Library/Logs/BCI Navigator/headband-<time>.jsonl` for troubleshooting.
    static let logFolder = URL.libraryDirectory.appending(path: "Logs/BCI Navigator", directoryHint: .isDirectory)
    private(set) var logURL: URL?

    var isRunning: Bool { process != nil }

    func quality(_ sensor: Sensor) -> SensorQuality {
        connection.isLive ? (qualities[sensor] ?? .unknown) : .unknown
    }

    func progress(_ sensor: Sensor) -> Double {
        quality(sensor) == .good ? (readyProgress[sensor] ?? 0) : 0
    }

    /// Good without a break for the full three-second readiness interval.
    func isReady(_ sensor: Sensor) -> Bool { progress(sensor) >= 1 }

    var readyCount: Int { Sensor.allCases.filter(isReady).count }

    /// Only measured DRL/REF evidence identifies a disconnected reference contact.
    var referenceProblem: Bool {
        connection.isLive && referenceStatus == "disconnected"
    }

    static let referenceFixTitle = "Fix the middle of the band"
    static let referenceQuickFix = "The middle of the band is not touching your forehead, so no sensor can read. Press it flat just above your eyebrows."
    static let referenceFixSteps = [
        "Center the band on your forehead, just above your eyebrows.",
        "Move hair away so the middle of the band touches bare skin.",
        "If your skin is dry, wipe your forehead with a slightly damp cloth.",
        "Hold still for about ten seconds while the signal settles.",
    ]
    var allGood: Bool { readyCount == Sensor.allCases.count }

    /// The sensor most in need of fixing, worst first, in head order. Nil until readings arrive.
    var worstSensor: Sensor? {
        Sensor.allCases
            .filter { quality($0) != .good && quality($0) != .unknown }
            .min { quality($0) < quality($1) }
    }

    func start(serial: String, settings: AppSettings) {
        stop()
        self.serial = serial
        qualities = [:]
        referenceStatus = "unavailable"
        noise = [:]
        sampleRate = nil
        logTail = []
        pending = ""
        openLog(serial: serial)

        let script = settings.signalCheckScriptURL
        guard FileManager.default.isExecutableFile(atPath: settings.pythonPath) else {
            connection = .failed("Python was not found at \(settings.pythonPath). Ask the research team to check Settings.")
            return
        }
        guard FileManager.default.fileExists(atPath: script.path) else {
            connection = .failed("The headband helper was not found at \(script.path). Ask the research team to check Settings.")
            return
        }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: settings.pythonPath)
        process.arguments = [script.path, "--serial", serial]
        process.currentDirectoryURL = settings.repoURL
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        process.environment = environment

        let out = Pipe()
        let err = Pipe()
        process.standardOutput = out
        process.standardError = err
        out.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            Task { @MainActor in self?.consume(text) }
        }
        err.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            Task { @MainActor in self?.appendLog(text) }
        }
        process.terminationHandler = { [weak self] finished in
            out.fileHandleForReading.readabilityHandler = nil
            err.fileHandleForReading.readabilityHandler = nil
            let status = finished.terminationStatus
            Task { @MainActor in self?.processEnded(finished, status: status) }
        }

        do {
            try process.run()
            self.process = process
            connection = .connecting(attempt: 1)
        } catch {
            connection = .failed("Could not start the headband helper: \(error.localizedDescription)")
        }
    }

    /// Asks the helper to release the headband and returns without waiting.
    func stop() {
        guard let process else { return }
        process.terminate()
        self.process = nil
        connection = .disconnected
        qualities = [:]
    }

    /// Releases the headband and waits until the helper has exited, so another
    /// program (the PsychoPy recorder) can connect. Force-quits after a few seconds.
    func stopAndWait(timeout: Duration = .seconds(5)) async {
        guard let process else { return }
        process.terminate()
        let deadline = ContinuousClock.now + timeout
        while process.isRunning && ContinuousClock.now < deadline {
            try? await Task.sleep(for: .milliseconds(100))
        }
        if process.isRunning { kill(process.processIdentifier, SIGKILL) }
        self.process = nil
        connection = .disconnected
        qualities = [:]
    }

    // MARK: - Helper output

    private func consume(_ text: String) {
        pending += text
        while let newline = pending.firstIndex(of: "\n") {
            let line = String(pending[..<newline])
            pending = String(pending[pending.index(after: newline)...])
            handle(line)
        }
    }

    private func openLog(serial: String) {
        try? log?.close()
        try? FileManager.default.createDirectory(at: Self.logFolder, withIntermediateDirectories: true)
        let stamp = Date.now.formatted(.iso8601.year().month().day().dateSeparator(.omitted).time(includingFractionalSeconds: false).timeSeparator(.omitted))
        let url = Self.logFolder.appending(path: "headband-\(serial)-\(stamp).jsonl")
        FileManager.default.createFile(atPath: url.path, contents: nil)
        log = try? FileHandle(forWritingTo: url)
        logURL = url
    }

    private func handle(_ line: String) {
        if let data = (line + "\n").data(using: .utf8) { try? log?.write(contentsOf: data) }
        guard let data = line.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let event = object["event"] as? String
        else {
            appendLog(line + "\n")
            return
        }
        // Ignore late lines from a helper we already stopped.
        guard process != nil else { return }

        switch event {
        case "status":
            switch object["state"] as? String {
            case "connecting": connection = .connecting(attempt: object["attempt"] as? Int ?? 1)
            case "connected": connection = .connected
            case "signal_lost": connection = .signalLost
            case "disconnected": if case .failed = connection {} else { connection = .disconnected }
            default: break
            }
        case "quality":
            guard let sensors = object["sensors"] as? [String: [String: Any]] else { return }
            referenceStatus = object["reference_status"] as? String ?? "unavailable"
            var newQualities: [Sensor: SensorQuality] = [:]
            var newNoise: [Sensor: Double] = [:]
            var newReasons: [Sensor: [String]] = [:]
            var newProgress: [Sensor: Double] = [:]
            for sensor in Sensor.allCases {
                guard let reading = sensors[sensor.rawValue] else { continue }
                newQualities[sensor] = SensorQuality(wire: reading["quality"] as? String ?? "")
                if let value = reading["rms_uv"] as? Double { newNoise[sensor] = value }
                newReasons[sensor] = reading["reasons"] as? [String] ?? []
                newProgress[sensor] = (reading["ready_progress"] as? NSNumber)?.doubleValue ?? 0
            }
            qualities = newQualities
            noise = newNoise
            reasons = newReasons
            readyProgress = newProgress
            if connection != .connected { connection = .connected }
        case "error":
            connection = .failed(object["message"] as? String ?? "The headband connection stopped.")
        default:
            break
        }
    }

    private func appendLog(_ text: String) {
        logTail.append(contentsOf: text.split(separator: "\n").map(String.init))
        if logTail.count > 100 { logTail.removeFirst(logTail.count - 100) }
    }

    private func processEnded(_ ended: Process, status: Int32) {
        if !pending.isEmpty { handle(pending); pending = "" }
        guard ended === process else { return }  // a stopped helper finishing late
        process = nil
        qualities = [:]
        if case .failed = connection { return }
        connection = status == 0 ? .disconnected : .failed("The headband connection stopped unexpectedly (code \(status)).")
    }
}
