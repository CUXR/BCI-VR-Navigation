import Foundation
import SwiftData

// Compile with Models/*.swift and Services/*.swift; see README.md.
// Drives real-time control against a fake helper that speaks realtime_control.py's line protocol.
@main struct RealtimeRunnerTests {
    static let fakeHelper = """
    import json, pathlib, sys, threading, time
    log = pathlib.Path("commands.jsonl").open("a")
    pathlib.Path("args.txt").write_text(" ".join(sys.argv[1:]))
    def emit(event, **fields):
        print(json.dumps({"event": event, **fields}), flush=True)
    stop = threading.Event()
    def read():
        for line in sys.stdin:
            log.write(line); log.flush()
            if json.loads(line)["cmd"] == "stop":
                stop.set()
        stop.set()
    threading.Thread(target=read, daemon=True).start()
    emit("model", kind="group", subjects=11, trained="2026-04-22")
    emit("status", state="connecting", serial="Muse-TEST", attempt=1)
    emit("status", state="connected", serial="Muse-TEST")
    emit("vr", clients=0)
    time.sleep(0.2)
    for intent, conf, stable in [("left", 0.7, True), ("left", 0.72, True), ("idle", 0.4, False),
                                 ("blink", 0.8, True), ("right", 0.66, True)]:
        emit("prediction", intent=intent, confidence=conf, stable=stable, leaning="left", leaning_confidence=0.4)
        time.sleep(0.05)
    emit("vr", clients=1)
    stop.wait(10)
    emit("status", state="disconnected")
    """

    @MainActor static func main() async throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        let backend = root.appending(path: "macos_app/backend")
        try FileManager.default.createDirectory(at: backend, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        try fakeHelper.write(to: backend.appending(path: "realtime_control.py"), atomically: true, encoding: .utf8)

        let container = try ModelContainer(for: Participant.self, SessionRecord.self,
                                          configurations: ModelConfiguration(isStoredInMemoryOnly: true))
        let model = AppModel()
        let saved = (model.settings.repoPath, model.settings.pythonPath, model.settings.miThreshold, model.settings.blinkThreshold)
        defer {
            model.settings.repoPath = saved.0; model.settings.pythonPath = saved.1
            model.settings.miThreshold = saved.2; model.settings.blinkThreshold = saved.3
        }
        model.settings.repoPath = root.path
        model.settings.pythonPath = "/usr/bin/python3"
        let runner = model.realtime

        model.signIn(firstName: "Realtime", lastName: "Fixture", context: container.mainContext)
        model.openRealtime()
        precondition(model.screen == .realtimeIntro && model.state(of: .realtime) == .current)
        model.checkSensorsForRealtime()
        precondition(model.screen == .calibrationIntro)
        model.finishCalibration(context: container.mainContext)
        precondition(model.screen == .realtimeIntro && model.calibratedThisVisit, "Passing the check returns to real-time")
        precondition(model.session?.calibrationDone != true, "A real-time sensor check must not mark the recording session")

        model.startRealtime()
        precondition(runner.phase == .starting && model.isBusy)
        precondition(!model.canNavigate(to: .calibration), "Navigation stays locked while live")
        model.signOut()
        precondition(model.participant != nil, "Sign-out waits until real-time control stops")

        try await waitUntil("live") { runner.phase == .live }
        precondition(runner.modelSubjects == 11)
        try await waitUntil("predictions") { runner.vrClients == 1 }
        precondition(runner.recent.map(\.intent) == [.right, .blink, .left],
                     "A held left logs once; got \(runner.recent.map(\.intent))")
        precondition(runner.intent == .right && runner.vrConnected)
        precondition(runner.lastBlinkAt != nil)

        model.setThresholds(mi: 0.7, blink: 0.45)
        precondition(model.settings.miThreshold == 0.7, "Sensitivity is remembered")
        runner.stop()
        try await waitUntil("stopped") { runner.phase == .stopped }
        model.realtimeEnded()
        precondition(model.screen == .realtimeIntro && runner.phase == .idle && !model.isBusy)

        let args = try String(contentsOf: root.appending(path: "args.txt"), encoding: .utf8)
        precondition(args.contains("--mi-threshold"), "Saved thresholds are passed at launch: \(args)")
        let commands = try String(contentsOf: root.appending(path: "commands.jsonl"), encoding: .utf8)
            .split(separator: "\n").map { try JSONSerialization.jsonObject(with: Data($0.utf8)) as! [String: Any] }
        let thresholds = commands.first { $0["cmd"] as? String == "thresholds" }
        precondition((thresholds?["mi"] as? Double) == 0.7 && (thresholds?["blink"] as? Double) == 0.45)
        precondition(commands.last?["cmd"] as? String == "stop")

        model.signOut()
        precondition(model.participant == nil && !model.calibratedThisVisit, "Sign-out clears the sensor check")

        // Debug Skip Check: a recording session is flagged; real-time control still reads unchecked.
        model.signIn(firstName: "Realtime", lastName: "Fixture", context: container.mainContext)
        model.resumeSession(context: container.mainContext)
        model.skipCalibration(context: container.mainContext)
        precondition(model.screen == .collectIntro && model.session?.calibrationDone == true
                     && model.session?.sensorCheckSkipped == true, "Skip Check must flag the session")
        model.checkSensorsForRealtime()
        model.skipCalibration(context: container.mainContext)
        precondition(model.screen == .realtimeIntro && !model.calibratedThisVisit)
        model.signOut()

        // Skipping the sensor check still starts.
        model.signIn(firstName: "Realtime", lastName: "Fixture", context: container.mainContext)
        model.openRealtime()
        model.startRealtime()
        try await waitUntil("live without a sensor check") { runner.phase == .live }
        runner.stop()
        try await waitUntil("stopped again") { runner.phase == .stopped }
        model.realtimeEnded()
        print("PASS: optional sensor check, debug skip flag, return to real-time, navigation lock, predictions, action log, live thresholds, stop")
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
