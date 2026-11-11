import AppKit
import Foundation
import Observation

/// Runs the data collection experiment inside the app: spoken and on-screen cues, Space/B/Esc
/// key presses, rests, and progress. The headless helper `macos_app/backend/muse_session_recorder.py`
/// owns the Muse connection and writes the EEG file and unified trial log; this class sends it
/// one JSON command per event, stamped with the moment it happened on the system clock.
///
/// Protocol: Part 1 left/right imagery, Part 2 forward walking imagery (fixed script, practice,
/// explain-back check, then randomized Walk/Relax trials and a post-block check), Part 3 blinks.
@Observable
final class RecorderRunner {
    /// Part 2 trial classes. Walk and Relax alternate with the same length; only Walk has step tones.
    enum ForwardCue: String, CaseIterable {
        case walk, relax

        var trialType: String { self == .walk ? "motor_imagery_forward" : "baseline_quiet" }
        var title: String { rawValue.capitalized }
        var fullInstruction: String {
            switch self {
            case .walk: "Walk. One step per tone."
            case .relax: "Relax. Stay still."
            }
        }
    }

    enum Cue: String, CaseIterable {
        case left, right

        var trialType: String { "motor_imagery_\(rawValue)" }
        var title: String { rawValue.capitalized }
        var shortInstruction: String { "Grab \(rawValue)" }
        var fullInstruction: String {
            "Now, imagine reaching out with your \(rawValue) hand to grab a cup on your \(rawValue) side. Focus on the sensation of your \(rawValue) hand moving."
        }
    }

    enum Phase: Equatable {
        case idle
        /// Releasing the calibration connection and launching the helper.
        case starting
        case connecting(attempt: Int)
        /// Connected; waiting for the participant to press Start.
        case ready
        /// Waiting for the helper to open the recording files.
        case beginning
        /// The spoken instruction for the next imagery trial is playing.
        case listening(Cue)
        /// Trial open; Space ends it.
        case imagining(Cue)
        case resting(until: Date)
        /// Part 2: the fixed walking script is playing.
        case forwardScript
        /// Unrecorded practice: Walk with step tones, or the silent Relax between practice walks.
        case practicing(ForwardCue, trial: Int, until: Date)
        /// The participant describes what they imagined; the researcher judges it.
        case explainBack(attempt: Int)
        /// The spoken cue for the next recorded Walk or Relax trial is playing.
        case forwardListening(ForwardCue)
        /// Recorded fixed-length Walk (with step tones) or Relax (silent) trial.
        case forwardTrial(ForwardCue, until: Date)
        /// Post-block questions: what was imagined, and how vividly.
        case forwardCheck
        case blinkIntro
        /// Blink block; B marks a blink, Enter finishes early.
        case blinking
        case saving
        case finished(saved: Bool)
        case failed(String)
    }

    var rounds = 10
    var imageryTarget: Int { rounds * 2 }
    var blinkTarget = 10
    /// Recorded Walk trials in Part 2; the same number of Relax trials is added. 0 skips Part 2.
    var forwardTrials = 20
    var forwardTarget: Int { forwardTrials * 2 }
    var practiceTrials = 1
    var maxExplainBackAttempts = 3
    /// Length of each Walk, Relax, and practice trial.
    var forwardTrialSeconds: TimeInterval = 4
    /// One tone, and one imagined step, per interval.
    var stepInterval: TimeInterval = 1

    /// Every participant hears exactly this script, so everyone imagines the same movement.
    var forwardScript: String {
        "Now imagine walking forward. Feel your own legs step, one step per tone. "
            + "Don't picture gliding or the room moving. Keep your real legs still. "
            + (practiceTrials == 1 ? "Let's practice once." : "Let's practice \(practiceTrials) times.")
    }
    static let repeatPrefix = "Once more. "
    static let explainBackQuestion = "Tell the researcher what you imagined."
    static let forwardCheckPrompt = "Answer the two questions on screen."

    var blinkInstruction: String {
        "Now we will record intentional blinks. Blink deliberately once, and press B at the same moment. Repeat until the counter reaches \(blinkTarget)."
    }

    private(set) var phase: Phase = .idle
    private(set) var connection: HeadbandConnection = .disconnected
    private(set) var startedAt: Date?
    private(set) var endedAt: Date?
    /// Imagery trials and blinks the helper confirmed it logged.
    private(set) var imageryDone = 0
    private(set) var blinksDone = 0
    /// Walk and Relax trials the helper confirmed it logged.
    private(set) var forwardDone = 0
    private(set) var relaxDone = 0
    /// 1-based position in the recorded Part 2 sequence.
    private(set) var forwardIndex = 0
    /// Counts step tones, so the on-screen guide can alternate feet in time.
    private(set) var stepCount = 0
    private(set) var explainBackAttempts = 0
    /// nil until the researcher judges the explain-back; false if it never matched the script.
    private(set) var explainBackPassed: Bool?
    private(set) var forwardStrategy: ImageryStrategy?
    private(set) var forwardVividness: Int?
    /// 1-based round of the imagery block.
    private(set) var round = 1
    private(set) var lastBlinkAt: Date?
    private(set) var imageryReadyAt: Date?
    /// A non-fatal message from the helper, e.g. a blink it could not record.
    private(set) var notice: String?
    private(set) var eegFile: String?
    private(set) var trialLogFile: String?
    /// Set when the helper reports a partial recording (reader or disk failure).
    private(set) var incompleteReason: String?
    /// Last lines of helper diagnostics, kept for error reports.
    private(set) var logTail: [String] = []

    /// Rest between imagery trials, in seconds.
    var restSeconds: TimeInterval = 3
    /// Replaces the system voice; tests use it to skip speech.
    var speakOverride: ((String) async -> Void)?
    /// Replaces the step tone; tests use it to stay silent.
    var toneOverride: (() -> Void)?
    private var explainBackDecision: Bool?

    private var process: Process?
    private var input: FileHandle?
    private var pending = ""
    private var errorMessage: String?
    private var protocolTask: Task<Void, Never>?
    private var waiter: CheckedContinuation<Bool, Never>?
    private var speech: Process?
    private var stopping = false

    init() {
        // Writing to the helper after it exits must fail with EPIPE, not kill the app.
        signal(SIGPIPE, SIG_IGN)
    }

    var isRunning: Bool {
        switch phase {
        case .idle, .finished, .failed: false
        default: true
        }
    }

    /// The protocol is underway and files are being written.
    var isRecording: Bool { startedAt != nil && isRunning }

    var canStop: Bool { isRunning && phase != .starting && phase != .saving }

    /// The protocol is waiting for a key press, a researcher judgment, or the check answers.
    var isAwaitingInput: Bool { waiter != nil }

    // MARK: - Lifecycle

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
            phase = .failed("The recording helper was not found in \(settings.repoPath). Ask the research team to check Settings.")
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
        // The helper writes to the relative path data/subNN, so run from the repo root.
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
            // Keep navigation locked while the single Bluetooth connection is released.
            await headband.stopAndWait()
            do {
                try process.run()
                self.process = process
                input = stdin.fileHandleForWriting
                phase = .connecting(attempt: 1)
                connection = .connecting(attempt: 1)
            } catch {
                phase = .failed("Could not start the recording helper: \(error.localizedDescription)")
            }
        }
    }

    /// Start (or Enter) on the ready screen: open the recording files, then run the protocol.
    func begin() {
        guard phase == .ready else { return }
        phase = .beginning
        send(["cmd": "start"])
    }

    /// Esc or Stop: end the session now and keep everything recorded so far.
    func stopAndSave() {
        guard canStop else { return }
        stopping = true
        protocolTask?.cancel()
        speech?.terminate()
        resumeWaiter(false)
        phase = .saving
        send(["cmd": "stop"])
    }

    func reset() {
        phase = .idle
        connection = .disconnected
        startedAt = nil
        endedAt = nil
        imageryDone = 0
        blinksDone = 0
        forwardDone = 0
        relaxDone = 0
        forwardIndex = 0
        stepCount = 0
        explainBackAttempts = 0
        explainBackPassed = nil
        explainBackDecision = nil
        forwardStrategy = nil
        forwardVividness = nil
        round = 1
        lastBlinkAt = nil
        imageryReadyAt = nil
        notice = nil
        eegFile = nil
        trialLogFile = nil
        incompleteReason = nil
        errorMessage = nil
        logTail = []
        pending = ""
        stopping = false
    }

    // MARK: - Key presses

    /// Space: the participant finished imagining.
    func pressSpace() {
        guard case .imagining = phase, waiter != nil else { return }
        guard let imageryReadyAt, Date.now >= imageryReadyAt else { return }
        send(["cmd": "end_trial", "t": Self.now()])
        // Leave the cue at once so a second press cannot end the trial twice.
        phase = .resting(until: .now + restSeconds)
        resumeWaiter(true)
    }

    /// B: the participant blinked on purpose.
    func pressBlink() {
        guard phase == .blinking else { return }
        send(["cmd": "blink", "t": Self.now()])
    }

    /// Enter: start from the ready screen, or finish the blink block early.
    func pressEnter() {
        switch phase {
        case .ready: begin()
        case .blinking: resumeWaiter(true)
        default: break
        }
    }

    /// The researcher's judgment of the participant's description.
    func judgeExplainBack(matches: Bool) {
        guard case .explainBack = phase, waiter != nil else { return }
        explainBackDecision = matches
        resumeWaiter(true)
    }

    /// Post-block answers for Part 2.
    func submitForwardCheck(strategy: ImageryStrategy, vividness: Int) {
        guard phase == .forwardCheck, waiter != nil, (1...5).contains(vividness) else { return }
        forwardStrategy = strategy
        forwardVividness = vividness
        resumeWaiter(true)
    }

    // MARK: - Protocol

    private func runProtocol() async {
        for index in 0..<rounds {
            round = index + 1
            for cue in Cue.allCases {
                phase = .listening(cue)
                try? await Task.sleep(for: .milliseconds(500))
                guard !stopping else { return }
                await say(index == 0 ? cue.fullInstruction : cue.shortInstruction)
                guard !stopping else { return }
                send([
                    "cmd": "start_trial", "trial_type": cue.trialType, "block": 1,
                    "notes": "\(cue.title) motor imagery - Round \(index + 1)", "t": Self.now(),
                ])
                // Leave a margin above the trainer's two-second EEG minimum.
                imageryReadyAt = .now + 3
                phase = .imagining(cue)
                guard await waitForKey() else { return }
                let lastTrial = index == rounds - 1 && cue == .right
                if !lastTrial, !(await rest()) { return }
            }
        }
        guard await rest() else { return }
        if forwardTrials > 0 {
            guard await runForward(), await rest() else { return }
        }

        phase = .blinkIntro
        await say(blinkInstruction)
        guard !stopping else { return }
        phase = .blinking
        if blinksDone < blinkTarget {
            guard await waitForKey() else { return }
        }

        Task { await say("Experiment complete. Thank you for participating.") }
        stopping = true
        phase = .saving
        send(["cmd": "stop"])
    }

    /// Part 2: script, practice, explain-back (up to `maxExplainBackAttempts`), recorded trials
    /// alternating Walk, Relax, Walk, Relax with no break between them, then the post-block check.
    /// False if stopped.
    private func runForward() async -> Bool {
        for attempt in 1...max(1, maxExplainBackAttempts) {
            explainBackAttempts = attempt
            phase = .forwardScript
            await say(attempt == 1 ? forwardScript : Self.repeatPrefix + forwardScript)
            guard !stopping else { return false }
            // Walk, Relax, Walk, ... ending on a Walk; Relax replaces the break between practices.
            for index in 0..<practiceTrials {
                for cue in index < practiceTrials - 1 ? [ForwardCue.walk, .relax] : [.walk] {
                    phase = .forwardListening(cue)
                    await say(index == 0 ? cue.fullInstruction : cue.title)
                    guard !stopping else { return false }
                    let end = Date.now + forwardTrialSeconds
                    phase = .practicing(cue, trial: index + 1, until: end)
                    guard await hold(until: end, tones: cue == .walk) else { return false }
                }
            }
            phase = .explainBack(attempt: attempt)
            await say(Self.explainBackQuestion)
            explainBackDecision = nil
            guard await waitForKey() else { return false }
            let matched = explainBackDecision == true
            let lastAttempt = attempt == maxExplainBackAttempts
            sendReport([
                "kind": "explain_back", "attempt": attempt, "matched": matched,
                "flagged": !matched && lastAttempt, "t": Self.now(),
            ])
            if matched || lastAttempt {
                explainBackPassed = matched
                break
            }
        }

        let order = Array(repeating: [ForwardCue.walk, .relax], count: forwardTrials).flatMap { $0 }
        var heard: Set<ForwardCue> = []
        for (index, cue) in order.enumerated() {
            forwardIndex = index + 1
            phase = .forwardListening(cue)
            try? await Task.sleep(for: .milliseconds(500))
            guard !stopping else { return false }
            await say(heard.insert(cue).inserted ? cue.fullInstruction : cue.title)
            guard !stopping else { return false }
            send([
                "cmd": "start_trial", "trial_type": cue.trialType, "block": 2,
                "notes": "\(cue == .walk ? "Forward walking imagery" : "Relax baseline") - Trial \(index + 1)",
                "t": Self.now(),
            ])
            let end = Date.now + forwardTrialSeconds
            phase = .forwardTrial(cue, until: end)
            guard await hold(until: end, tones: cue == .walk) else { return false }
            send(["cmd": "end_trial", "t": Self.now()])
        }

        phase = .forwardCheck
        await say(Self.forwardCheckPrompt)
        guard !stopping, await waitForKey(), let strategy = forwardStrategy, let vividness = forwardVividness
        else { return false }
        sendReport([
            "kind": "block_check", "block": 2, "strategy": strategy.rawValue,
            "matches_script": strategy.matchesScript, "vividness": vividness,
            "explain_back_passed": explainBackPassed == true, "explain_back_attempts": explainBackAttempts,
            "use_for_training": strategy.matchesScript && vividness >= 3 && explainBackPassed == true,
            "t": Self.now(),
        ])
        return true
    }

    /// Waits until `end`, playing one step tone per `stepInterval` when `tones` is set. False if stopped.
    private func hold(until end: Date, tones: Bool) async -> Bool {
        while !stopping {
            let now = Date.now
            if now >= end { return true }
            if tones {
                tone()
                stepCount += 1
            }
            try? await Task.sleep(for: .seconds(min(stepInterval, end.timeIntervalSince(now))))
        }
        return false
    }

    private func tone() {
        if let toneOverride { toneOverride(); return }
        NSSound(named: "Tink")?.play()
    }

    private func sendReport(_ report: [String: Any]) {
        send(["cmd": "imagery_report", "report": report])
    }

    private func rest() async -> Bool {
        phase = .resting(until: .now + restSeconds)
        try? await Task.sleep(for: .seconds(restSeconds))
        return !stopping
    }

    /// Suspends until a key press (true) or a stop (false).
    private func waitForKey() async -> Bool {
        guard !stopping else { return false }
        return await withCheckedContinuation { waiter = $0 }
    }

    private func resumeWaiter(_ value: Bool) {
        let current = waiter
        waiter = nil
        current?.resume(returning: value)
    }

    private func say(_ text: String) async {
        if let speakOverride { await speakOverride(text); return }
        let voice = Process()
        voice.executableURL = URL(fileURLWithPath: "/usr/bin/say")
        voice.arguments = ["-v", "Samantha", "-r", "180", text]
        speech = voice
        await withCheckedContinuation { (done: CheckedContinuation<Void, Never>) in
            voice.terminationHandler = { _ in done.resume() }
            do { try voice.run() } catch { done.resume() }
        }
        if speech === voice { speech = nil }
    }

    private static func now() -> Double { Date().timeIntervalSince1970 }

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
                if case .connecting = phase { phase = .ready }
            case "signal_lost":
                connection = .signalLost
            case "disconnected":
                if case .failed = connection {} else { connection = .disconnected }
            default:
                break
            }
        case "recording":
            guard phase == .beginning else { return }
            startedAt = .now
            protocolTask = Task { await runProtocol() }
        case "trial_ended":
            switch object["trial_type"] as? String {
            case "motor_imagery_left", "motor_imagery_right": imageryDone += 1
            case ForwardCue.walk.trialType: forwardDone += 1
            case ForwardCue.relax.trialType: relaxDone += 1
            default: break
            }
        case "blink":
            blinksDone += 1
            lastBlinkAt = .now
            notice = nil
            if blinksDone >= blinkTarget, phase == .blinking { resumeWaiter(true) }
        case "warning":
            notice = object["message"] as? String
        case "saved":
            eegFile = object["eeg_file"] as? String
            trialLogFile = object["trial_log_file"] as? String
        case "incomplete":
            incompleteReason = object["reason"] as? String ?? "unknown reason"
            eegFile = object["eeg_file"] as? String
            trialLogFile = object["trial_log_file"] as? String
        case "error":
            errorMessage = object["message"] as? String
            connection = .failed(errorMessage ?? "The headband connection stopped.")
        default:
            break
        }
    }

    private func appendLog(_ text: String) {
        logTail.append(contentsOf: text.split(separator: "\n").map(String.init))
        if logTail.count > 200 { logTail.removeFirst(logTail.count - 200) }
    }

    private func finish(status: Int32) {
        if !pending.isEmpty { handle(pending); pending = "" }
        stopping = true
        protocolTask?.cancel()
        protocolTask = nil
        resumeWaiter(false)
        try? input?.close()
        input = nil
        process = nil
        endedAt = .now
        if case .failed = connection {} else { connection = .disconnected }

        if let incompleteReason {
            phase = .failed("The recording is incomplete: \(incompleteReason). Ask the research team to check the files before trying again.")
        } else if eegFile != nil {
            phase = .finished(saved: true)
        } else if let errorMessage {
            phase = .failed(errorMessage)
        } else if status == 0 || status == 15 {
            phase = .finished(saved: false)
        } else {
            phase = .failed("The recording helper stopped with an error (code \(status)).")
        }
    }
}
