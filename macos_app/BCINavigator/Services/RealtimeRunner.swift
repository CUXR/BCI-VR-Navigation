import Foundation
import Observation

/// Runs `macos_app/backend/realtime_control.py`, which reads the headband, classifies each
/// 100 ms tick with the selected model, and serves predictions to the VR app on
/// ws://127.0.0.1:8765. This class publishes what the helper reports and sends it
/// sensitivity changes while it runs.
@Observable
final class RealtimeRunner {
    enum Phase: Equatable {
        case idle
        /// Releasing the calibration connection and launching the helper.
        case starting
        case connecting(attempt: Int)
        case live
        case stopping
        case stopped
        case failed(String)
    }

    enum Intent: String {
        case left, right, forward, blink, idle
    }

    struct Action: Identifiable, Equatable {
        let id = UUID()
        let intent: Intent
        let confidence: Double
        let at: Date
    }

    private(set) var phase: Phase = .idle
    private(set) var connection: HeadbandConnection = .disconnected
    /// Latest recognized intent, after the helper's thresholds and smoothing. A blink lasts one tick.
    private(set) var intent: Intent = .idle
    private(set) var confidence = 0.0
    private(set) var stable = false
    /// Most likely hand, even below threshold, for the confidence bar.
    private(set) var leaning: Intent?
    private(set) var leaningConfidence = 0.0
    private(set) var lastBlinkAt: Date?
    private(set) var lastBlinkConfidence = 0.0
    /// VR app connections on the prediction socket; nil until the helper reports.
    private(set) var vrClients: Int?
    /// Why the VR link is not available, if it failed to start.
    private(set) var vrProblem: String?
    private(set) var modelSubjects: Int?
    private(set) var modelKind = "group"
    private(set) var recent: [Action] = []
    private(set) var logTail: [String] = []

    private var process: Process?
    private var input: FileHandle?
    private var pending = ""
    private var errorMessage: String?
    private var lastLogged: Intent = .idle

    init() {
        // Writing to the helper after it exits must fail with EPIPE, not kill the app.
        signal(SIGPIPE, SIG_IGN)
    }

    var isRunning: Bool {
        switch phase {
        case .starting, .connecting, .live, .stopping: true
        default: false
        }
    }

    var vrConnected: Bool { (vrClients ?? 0) > 0 }

    // MARK: - Lifecycle

    func start(settings: AppSettings, headband: MuseConnection, modelURL: URL? = nil, participant: Int? = nil) {
        guard !isRunning else { return }
        reset()
        phase = .starting

        let fm = FileManager.default
        guard fm.isExecutableFile(atPath: settings.pythonPath) else {
            phase = .failed("Python was not found at \(settings.pythonPath). Ask the research team to check Settings.")
            return
        }
        guard fm.fileExists(atPath: settings.realtimeScriptURL.path) else {
            phase = .failed("The real-time helper was not found in \(settings.repoPath). Ask the research team to check Settings.")
            return
        }

        var arguments = [
            settings.realtimeScriptURL.path,
            "--mi-threshold", String(settings.miThreshold),
            "--blink-threshold", String(settings.blinkThreshold),
        ]
        if let modelURL { arguments += ["--model", modelURL.path] }
        if let participant { arguments += ["--participant", String(participant)] }
        let serial = settings.museSerial.trimmingCharacters(in: .whitespaces)
        if !serial.isEmpty { arguments += ["--serial", serial] }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: settings.pythonPath)
        process.arguments = arguments
        process.currentDirectoryURL = settings.repoURL
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        process.environment = environment

        let stdin = Pipe()
        let out = Pipe()
        let err = Pipe()
        process.standardInput = stdin
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
            let status = finished.terminationStatus
            out.fileHandleForReading.readabilityHandler = nil
            err.fileHandleForReading.readabilityHandler = nil
            Task { @MainActor in self?.finish(status: status) }
        }

        Task {
            // The Muse 2 accepts one Bluetooth connection at a time.
            await headband.stopAndWait()
            guard phase == .starting else { return }
            do {
                try process.run()
                self.process = process
                input = stdin.fileHandleForWriting
                phase = .connecting(attempt: 1)
                connection = .connecting(attempt: 1)
            } catch {
                phase = .failed("Could not start the real-time helper: \(error.localizedDescription)")
            }
        }
    }

    func stop() {
        switch phase {
        case .starting:
            phase = .stopped
        case .connecting, .live:
            phase = .stopping
            send(["cmd": "stop"])
            // A helper stuck in a Bluetooth call gets SIGTERM, then is force-quit.
            let running = process
            Task {
                try? await Task.sleep(for: .seconds(5))
                guard let running, running.isRunning else { return }
                running.terminate()
                try? await Task.sleep(for: .seconds(3))
                if running.isRunning { kill(running.processIdentifier, SIGKILL) }
            }
        default:
            break
        }
    }

    /// New sensitivity, as confidence fractions; applies from the next tick.
    func setThresholds(mi: Double, blink: Double) {
        send(["cmd": "thresholds", "mi": mi, "blink": blink])
    }

    func reset() {
        phase = .idle
        connection = .disconnected
        intent = .idle
        confidence = 0
        stable = false
        leaning = nil
        leaningConfidence = 0
        lastBlinkAt = nil
        vrClients = nil
        vrProblem = nil
        modelSubjects = nil
        modelKind = "group"
        recent = []
        logTail = []
        pending = ""
        errorMessage = nil
        lastLogged = .idle
    }

    private func send(_ command: [String: Any]) {
        guard let input, let data = try? JSONSerialization.data(withJSONObject: command) else { return }
        try? input.write(contentsOf: data + Data("\n".utf8))
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

    private func handle(_ line: String) {
        guard let data = line.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let event = object["event"] as? String
        else {
            appendLog(line)
            return
        }
        switch event {
        case "status":
            switch object["state"] as? String {
            case "connecting":
                let attempt = object["attempt"] as? Int ?? 1
                connection = .connecting(attempt: attempt)
                if case .connecting = phase { phase = .connecting(attempt: attempt) }
            case "connected":
                connection = .connected
                if case .connecting = phase { phase = .live }
            case "signal_lost":
                connection = .signalLost
            case "disconnected":
                if case .failed = connection {} else { connection = .disconnected }
            default:
                break
            }
        case "model":
            modelSubjects = object["subjects"] as? Int
            modelKind = object["kind"] as? String ?? "group"
        case "vr":
            vrClients = object["clients"] as? Int
        case "prediction":
            guard phase == .live else { return }
            prediction(object)
        case "warning":
            vrProblem = object["message"] as? String
        case "error":
            errorMessage = object["message"] as? String
            connection = .failed(errorMessage ?? "The headband connection stopped.")
        default:
            break
        }
    }

    private func prediction(_ object: [String: Any]) {
        let next = Intent(rawValue: object["intent"] as? String ?? "") ?? .idle
        let value = (object["confidence"] as? NSNumber)?.doubleValue ?? 0
        intent = next
        confidence = value
        stable = object["stable"] as? Bool ?? false
        leaning = (object["leaning"] as? String).flatMap(Intent.init(rawValue:))
        leaningConfidence = (object["leaning_confidence"] as? NSNumber)?.doubleValue ?? 0

        switch next {
        case .blink:
            lastBlinkAt = .now
            lastBlinkConfidence = value
            log(.blink, value)
        case .left, .right, .forward:
            if stable && next != lastLogged {
                log(next, value)
                lastLogged = next
            }
        case .idle:
            lastLogged = .idle
        }
    }

    private func log(_ intent: Intent, _ confidence: Double) {
        recent.insert(Action(intent: intent, confidence: confidence, at: .now), at: 0)
        if recent.count > 6 { recent.removeLast(recent.count - 6) }
    }

    private func appendLog(_ text: String) {
        logTail.append(contentsOf: text.split(separator: "\n").map(String.init))
        if logTail.count > 200 { logTail.removeFirst(logTail.count - 200) }
    }

    private func finish(status: Int32) {
        if !pending.isEmpty { handle(pending); pending = "" }
        try? input?.close()
        input = nil
        process = nil
        intent = .idle
        if case .failed = connection {} else { connection = .disconnected }
        if phase == .stopping || (status == 0 && errorMessage == nil) {
            phase = .stopped
        } else {
            phase = .failed(errorMessage ?? "The real-time helper stopped with an error (code \(status)).")
        }
    }
}
