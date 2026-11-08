import Foundation
import SwiftData

@main struct PersonalModelTrainerTests {
    static let trainingHelper = """
    import argparse, json, pathlib, sys, time
    p = argparse.ArgumentParser()
    p.add_argument('--participant', type=int)
    p.add_argument('--data-root')
    p.add_argument('--output-dir')
    args = p.parse_args()
    root = pathlib.Path.cwd()
    with (root / 'launches.txt').open('a') as f: f.write('launch\\n')
    mode = (root / 'mode.txt').read_text()
    time.sleep(0.2)
    if mode == 'wait': time.sleep(20)
    if mode == 'fail':
        print(json.dumps({'event': 'error', 'message': 'Collect more right-hand trials.'}), flush=True)
        sys.exit(2)
    target = pathlib.Path(args.output_dir) / f'sub{args.participant:02d}_app.pkl'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b'trained model')
    print(json.dumps({'event': 'trained', 'path': str(target), 'n_mi_epochs': 20, 'n_blink_pos': 10}), flush=True)
    """

    static let realtimeHelper = """
    import json, pathlib, sys
    pathlib.Path('realtime-args.json').write_text(json.dumps(sys.argv[1:]))
    print(json.dumps({'event': 'model', 'kind': 'personalized', 'subjects': 0}), flush=True)
    print(json.dumps({'event': 'status', 'state': 'connected'}), flush=True)
    for line in sys.stdin:
        if json.loads(line)['cmd'] == 'stop': break
    """

    @MainActor static func main() async throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        let backend = root.appending(path: "macos_app/backend")
        try FileManager.default.createDirectory(at: backend, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        try trainingHelper.write(to: backend.appending(path: "train_personal_model.py"), atomically: true, encoding: .utf8)
        try realtimeHelper.write(to: backend.appending(path: "realtime_control.py"), atomically: true, encoding: .utf8)
        let mode = root.appending(path: "mode.txt")
        try "success".write(to: mode, atomically: true, encoding: .utf8)
        let container = try ModelContainer(for: Participant.self, SessionRecord.self,
                                          configurations: ModelConfiguration(isStoredInMemoryOnly: true))
        let model = AppModel()
        let saved = (model.settings.repoPath, model.settings.pythonPath)
        defer { model.settings.repoPath = saved.0; model.settings.pythonPath = saved.1 }
        model.settings.repoPath = root.path
        model.settings.pythonPath = "/usr/bin/python3"
        model.signIn(firstName: "Training", lastName: "Fixture", context: container.mainContext)
        model.openRealtime()
        precondition(model.trainer.modelURL == nil)
        model.trainPersonalModel()
        model.trainPersonalModel() // duplicate clicks cannot launch another process
        precondition(model.isBusy && !model.canNavigate(to: .calibration))
        model.startRealtime()
        model.startRecording()
        model.signOut()
        precondition(model.screen == .realtimeIntro && model.participant != nil)
        precondition(!model.realtime.isRunning && !model.recorder.isRunning)
        try await waitUntil { !model.trainer.isRunning }
        guard case .trained = model.trainer.phase else { fatalError("\(model.trainer.phase)") }
        let target = model.trainer.modelURL!
        let launches = try String(contentsOf: root.appending(path: "launches.txt"), encoding: .utf8)
        precondition(launches.split(separator: "\n").count == 1)
        model.usePersonalModel = true
        model.startRealtime()
        try await waitUntil { model.realtime.phase == .live }
        precondition(model.realtime.modelKind == "personalized")
        let arguments = try JSONSerialization.jsonObject(with: Data(contentsOf: root.appending(path: "realtime-args.json"))) as! [String]
        precondition(arguments.contains(target.path) && arguments.contains("--participant"))
        model.realtime.stop()
        try await waitUntil { model.realtime.phase == .stopped }
        model.realtimeEnded()

        try "fail".write(to: mode, atomically: true, encoding: .utf8)
        model.trainPersonalModel()
        try await waitUntil { !model.trainer.isRunning }
        precondition(model.trainer.phase == .failed("Collect more right-hand trials."))
        precondition(model.trainer.modelURL == target && (try? Data(contentsOf: target)) == Data("trained model".utf8))

        try "wait".write(to: mode, atomically: true, encoding: .utf8)
        model.trainPersonalModel()
        model.trainer.cancel()
        precondition(model.isBusy, "Stay locked until the cancelled process exits")
        try await waitUntil { !model.trainer.isRunning }
        precondition(model.trainer.phase == .cancelled && model.trainer.modelURL == target)

        model.signOut()
        precondition(!model.usePersonalModel && model.trainer.modelURL == nil)
        model.signIn(firstName: "Other", lastName: "Fixture", context: container.mainContext)
        model.openRealtime()
        precondition(model.trainer.modelURL == nil, "Never offer another participant's model")
        model.signOut()
        model.signIn(firstName: "Training", lastName: "Fixture", context: container.mainContext)
        model.openRealtime()
        precondition(model.trainer.modelURL == target, "Saved model is rediscovered after sign-in")
        print("PASS: training, duplicate prevention, navigation lock, selected model launch, failure preservation, cancel, participant isolation, rediscovery")
    }

    @MainActor static func waitUntil(_ condition: () -> Bool) async throws {
        let deadline = ContinuousClock.now + .seconds(10)
        while !condition() {
            guard ContinuousClock.now < deadline else { fatalError("Timed out") }
            try await Task.sleep(for: .milliseconds(10))
        }
    }
}
