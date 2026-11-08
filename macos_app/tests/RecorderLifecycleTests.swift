import Foundation
import SwiftData

// Compile with Models/*.swift and Services/*.swift; see README.md.
// Drives the in-app experiment against a fake recording helper that speaks the same line protocol.
@main struct RecorderLifecycleTests {
    static let fakeHelper = """
    import json, pathlib, sys
    pathlib.Path("launches.txt").open("a").write("launch\\n")
    log = pathlib.Path("commands.jsonl").open("a")
    def emit(event, **fields):
        print(json.dumps({"event": event, **fields}), flush=True)
    emit("status", state="connecting", serial="Muse-TEST", attempt=1)
    emit("status", state="connected", serial="Muse-TEST")
    recording, trial, blinks, current = False, 0, 0, None
    for line in sys.stdin:
        cmd = json.loads(line)
        log.write(line); log.flush()
        name = cmd["cmd"]
        if name == "start":
            recording = True
            emit("recording")
        elif name == "start_trial":
            trial += 1
            current = cmd["trial_type"]
            assert isinstance(cmd["t"], float)
            emit("trial_started", trial_id=f"trial_{trial:04d}", trial_type=current)
        elif name == "end_trial":
            assert isinstance(cmd["t"], float)
            emit("trial_ended", trial_id=f"trial_{trial:04d}", trial_type=current, duration=1.0)
        elif name == "blink":
            trial += 1
            blinks += 1
            emit("blink", trial_id=f"trial_{trial:04d}", count=blinks)
        elif name == "stop":
            break
    if recording:
        emit("saved", eeg_file="data/sub01/eeg_data_test.csv", trial_log_file="data/sub01/trial_log_test.csv")
    emit("status", state="disconnected")
    """

    @MainActor static func main() async throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        let backend = root.appending(path: "macos_app/backend")
        try FileManager.default.createDirectory(at: backend, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        try fakeHelper.write(to: backend.appending(path: "muse_session_recorder.py"), atomically: true, encoding: .utf8)

        let container = try ModelContainer(for: Participant.self, SessionRecord.self,
                                          configurations: ModelConfiguration(isStoredInMemoryOnly: true))
        let model = AppModel()
        let oldRepo = model.settings.repoPath
        let oldPython = model.settings.pythonPath
        defer { model.settings.repoPath = oldRepo; model.settings.pythonPath = oldPython }
        model.settings.repoPath = root.path
        model.settings.pythonPath = "/usr/bin/python3"
        let recorder = model.recorder
        recorder.speakOverride = { _ in }
        recorder.restSeconds = 0.01

        model.signIn(firstName: "Review", lastName: "Fixture", context: container.mainContext)
        model.resumeSession(context: container.mainContext)

        // Full protocol.
        model.startRecording()
        precondition(recorder.phase == .starting, "Handoff must lock synchronously")
        precondition(!model.canNavigate(to: .signIn), "Navigation must stay locked during handoff")
        model.signOut()
        precondition(model.participant != nil && model.screen == .recording, "Sign-out must not abandon startup")
        model.startRecording() // Repeated clicks must not create another launch.

        try await waitUntil("ready") { recorder.phase == .ready }
        recorder.pressSpace() // Ignored before the protocol starts.
        recorder.pressEnter()
        for trial in 0..<RecorderRunner.imageryTarget {
            try await waitUntil("imagery trial \(trial + 1)") {
                if case .imagining = recorder.phase { return recorder.imageryDone == trial }
                return false
            }
            recorder.pressSpace()  // A double press must not end the trial twice.
            recorder.pressSpace()
        }
        try await waitUntil("blink block") { recorder.phase == .blinking }
        precondition(recorder.imageryDone == RecorderRunner.imageryTarget, "Every imagery trial must be confirmed")
        for blink in 0..<RecorderRunner.blinkTarget {
            recorder.pressBlink()
            try await waitUntil("blink \(blink + 1)") { recorder.blinksDone == blink + 1 }
        }
        try await waitUntil("saved") { !recorder.isRunning }
        precondition(recorder.phase == .finished(saved: true), "Recorder must report its saved result, got \(recorder.phase)")
        precondition(recorder.eegFile == "data/sub01/eeg_data_test.csv")

        let launches = try String(contentsOf: root.appending(path: "launches.txt"), encoding: .utf8)
        precondition(launches.split(separator: "\n").count == 1, "Only one helper may launch")
        let commands = try String(contentsOf: root.appending(path: "commands.jsonl"), encoding: .utf8)
            .split(separator: "\n").map { try JSONSerialization.jsonObject(with: Data($0.utf8)) as! [String: Any] }
        let names = commands.map { $0["cmd"] as! String }
        precondition(names.filter { $0 == "start_trial" }.count == RecorderRunner.imageryTarget)
        precondition(names.filter { $0 == "end_trial" }.count == RecorderRunner.imageryTarget, "Early Space must not end a trial")
        precondition(names.filter { $0 == "blink" }.count == RecorderRunner.blinkTarget)
        let types = commands.compactMap { $0["trial_type"] as? String }
        precondition(types.prefix(2) == ["motor_imagery_left", "motor_imagery_right"], "Trials alternate left, right")

        model.recordingEnded(context: container.mainContext)
        precondition(model.session?.collectionDone == true && model.screen == .done)
        precondition(model.session?.imageryTrials == 20 && model.session?.blinks == 10)
        model.signOut()
        precondition(model.participant == nil, "Sign-out should work after recording finishes")

        // Esc partway through saves what was recorded.
        model.signIn(firstName: "Review", lastName: "Fixture", context: container.mainContext)
        model.resumeSession(context: container.mainContext)
        model.startRecording()
        try await waitUntil("ready again") { recorder.phase == .ready }
        recorder.pressEnter()
        try await waitUntil("first trial") { if case .imagining = recorder.phase { return true }; return false }
        recorder.pressSpace()
        try await waitUntil("second trial") { if case .imagining(.right) = recorder.phase { return true }; return false }
        recorder.stopAndSave()
        try await waitUntil("stopped") { !recorder.isRunning }
        precondition(recorder.phase == .finished(saved: true) && recorder.imageryDone == 1,
                     "Esc must save the partial session, got \(recorder.phase), \(recorder.imageryDone)")

        // Cancel before starting saves nothing.
        recorder.reset()
        model.startRecording()
        try await waitUntil("ready for cancel") { recorder.phase == .ready }
        recorder.stopAndSave()
        try await waitUntil("cancelled") { !recorder.isRunning }
        precondition(recorder.phase == .finished(saved: false), "Cancel before Start must not claim a save")

        print("PASS: startup lock, single launch, full protocol, timestamps, Esc save, cancel, session save, sign-out")
    }

    @MainActor static func waitUntil(_ what: String, timeout: Duration = .seconds(10),
                                     _ condition: () -> Bool) async throws {
        let deadline = ContinuousClock.now + timeout
        while !condition() {
            guard ContinuousClock.now < deadline else { fatalError("Timed out waiting for \(what)") }
            try await Task.sleep(for: .milliseconds(10))
        }
    }
}
