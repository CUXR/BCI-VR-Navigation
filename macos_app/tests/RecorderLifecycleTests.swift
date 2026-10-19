import Foundation
import SwiftData

// Compile with Models/*.swift and Services/*.swift; see README.md.
@main struct RecorderLifecycleTests {
    @MainActor static func main() async throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let scripts = root.appending(path: "src/pilot_data_collection/psychopy_recording")
        try FileManager.default.createDirectory(at: scripts, withIntermediateDirectories: true)
        let script = scripts.appending(path: "muse_psychopy_recording_structured.py")
        try """
        import pathlib, time
        with pathlib.Path("launches.txt").open("a") as f:
            f.write("launch\\n")
        time.sleep(0.3)
        print("Started trial: test (motor_imagery_left)", flush=True)
        print("Ended trial: test (duration: 1.000s)", flush=True)
        print("Recording saved to data/sub01/eeg_data_test.csv", flush=True)
        print("Trial log saved to data/sub01/trial_log_test.csv", flush=True)
        time.sleep(0.1)
        """.write(to: script, atomically: true, encoding: .utf8)
        let container = try ModelContainer(for: Participant.self, SessionRecord.self,
                                          configurations: ModelConfiguration(isStoredInMemoryOnly: true))
        let model = AppModel()
        let oldRepo = model.settings.repoPath
        let oldPython = model.settings.pythonPath
        defer { model.settings.repoPath = oldRepo; model.settings.pythonPath = oldPython }
        model.settings.repoPath = root.path
        model.settings.pythonPath = "/usr/bin/python3"
        model.signIn(firstName: "Review", lastName: "Fixture", context: container.mainContext)
        model.resumeSession(context: container.mainContext)

        model.startRecording()
        precondition(model.recorder.phase == .starting, "Handoff must lock synchronously")
        precondition(!model.canNavigate(to: .signIn), "Navigation must stay locked during handoff")
        model.signOut()
        precondition(model.participant != nil && model.screen == .recording, "Sign-out must not abandon startup")
        model.startRecording() // Repeated clicks must not create another launch.
        let deadline = ContinuousClock.now + .seconds(10)
        while model.recorder.isRunning && ContinuousClock.now < deadline {
            try await Task.sleep(for: .milliseconds(20))
        }
        precondition(model.recorder.phase == .finished(saved: true), "Recorder must report its saved result")
        let launches = try String(contentsOf: root.appending(path: "launches.txt"), encoding: .utf8)
        precondition(launches.split(separator: "\n").count == 1, "Only one recorder may launch")
        precondition(model.recorder.imageryDone == 1, "Progress must still be parsed")
        model.recordingEnded(context: container.mainContext)
        precondition(model.session?.collectionDone == true && model.screen == .done)
        model.signOut()
        precondition(model.participant == nil, "Sign-out should work after recording finishes")
        print("PASS: startup lock, duplicate launch prevention, output parsing, session save, and sign-out")
    }
}
