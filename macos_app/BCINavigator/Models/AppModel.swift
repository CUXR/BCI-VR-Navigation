import Foundation
import Observation
import SwiftData

enum AppMode { case user, admin }

enum UserScreen: Equatable {
    case signIn, firstTime, home, calibrationIntro, calibration, collectIntro, recording, done
    case realtimeIntro, realtimeLive
}

/// Steps shown in the user-mode sidebar timeline.
enum UserStep: CaseIterable, Identifiable {
    case signIn, calibration, collect, realtime
    var id: Self { self }

    var title: String {
        switch self {
        case .signIn: "Sign in"
        case .calibration: "Calibration"
        case .collect: "Collect EEG data"
        case .realtime: "Real-time control"
        }
    }

    var comingSoon: Bool { false }
}

enum StepState { case done, current, todo, soon }

@Observable
final class AppModel {
    var mode: AppMode = .user
    var screen: UserScreen = .signIn
    private(set) var participant: Participant?
    private(set) var session: SessionRecord?

    let settings = AppSettings()
    let headband = MuseConnection()
    let recorder = RecorderRunner()
    let realtime = RealtimeRunner()
    /// Sensors were checked during this sign-in. Real-time control recommends it but can skip it.
    private(set) var calibratedThisVisit = false
    /// Where passing the sensor check leads: data collection, or back to real-time control.
    private(set) var calibrationReturnsTo: UserScreen = .collectIntro

    /// A recording or real-time session holds the headband; navigation and sign-out wait.
    var isBusy: Bool { recorder.isRunning || realtime.isRunning }

    var dataFolder: DataFolder { DataFolder(root: settings.dataURL) }

    // MARK: - Sign in

    func signIn(firstName: String, lastName: String, context: ModelContext) {
        let first = firstName.trimmingCharacters(in: .whitespacesAndNewlines)
        let last = lastName.trimmingCharacters(in: .whitespacesAndNewlines)
        let all = (try? context.fetch(FetchDescriptor<Participant>())) ?? []
        if let match = all.first(where: {
            Participant.normalized($0.fullName) == Participant.normalized("\(first) \(last)")
        }) {
            participant = match
            session = match.openSession
            screen = .home
        } else {
            let highest = max(all.map(\.subjectNumber).max() ?? 0, dataFolder.highestSubjectNumber())
            let created = Participant(firstName: first, lastName: last, subjectNumber: highest + 1)
            context.insert(created)
            try? context.save()
            participant = created
            session = nil
            screen = .firstTime
        }
    }

    /// "Not Me" on the first sign-in screen: remove the profile that was just created.
    func undoNewProfile(context: ModelContext) {
        if let participant { context.delete(participant); try? context.save() }
        signOut()
    }

    func signOut() {
        guard !isBusy else { return }
        headband.stop()
        participant = nil
        session = nil
        recorder.reset()
        realtime.reset()
        calibratedThisVisit = false
        calibrationReturnsTo = .collectIntro
        screen = .signIn
    }

    // MARK: - Session progress

    func completedSessions(for participant: Participant) -> Int {
        dataFolder.recordings(subjectNumber: participant.subjectNumber).count
    }

    func lastSessionDate(for participant: Participant) -> Date? {
        dataFolder.recordings(subjectNumber: participant.subjectNumber).last?.date
    }

    /// Opens the current session, creating it if this is a fresh visit, and goes to its first unfinished step.
    func resumeSession(context: ModelContext) {
        guard let participant else { return }
        let current = participant.openSession ?? {
            let record = SessionRecord(number: completedSessions(for: participant) + 1)
            record.participant = participant
            context.insert(record)
            try? context.save()
            return record
        }()
        session = current
        calibrationReturnsTo = .collectIntro
        screen = current.calibrationDone ? .collectIntro : .calibrationIntro
    }

    func selectHeadband(_ serial: String, context: ModelContext) {
        settings.museSerial = serial
        session?.headband = serial
        try? context.save()
    }

    var selectedHeadband: String? {
        let serial = settings.museSerial
        return AppSettings.headbands.contains(serial) ? serial : nil
    }

    func beginSensorCheck() {
        guard let serial = selectedHeadband else { return }
        session?.headband = serial
        if headband.serial != serial || !headband.isRunning {
            headband.start(serial: serial, settings: settings)
        }
        screen = .calibration
    }

    func retryHeadband() {
        guard let serial = selectedHeadband else { return }
        headband.start(serial: serial, settings: settings)
    }

    func finishCalibration(context: ModelContext) {
        calibratedThisVisit = true
        if calibrationReturnsTo == .realtimeIntro {
            screen = .realtimeIntro
            return
        }
        session?.calibrationDone = true
        try? context.save()
        screen = .collectIntro
    }

    /// Debug: leave the sensor check without four green sensors. A recording session
    /// remembers that its check was skipped; real-time control still reads "Not checked".
    func skipCalibration(context: ModelContext) {
        if calibrationReturnsTo == .realtimeIntro {
            screen = .realtimeIntro
            return
        }
        session?.calibrationDone = true
        session?.sensorCheckSkipped = true
        try? context.save()
        screen = .collectIntro
    }

    // MARK: - Real-time control

    func openRealtime() {
        guard participant != nil, !isBusy else { return }
        realtime.reset()
        screen = .realtimeIntro
    }

    /// Sensor check on the way to real-time control; passing it comes back here.
    func checkSensorsForRealtime() {
        calibrationReturnsTo = .realtimeIntro
        screen = .calibrationIntro
    }

    func startRealtime() {
        guard participant != nil, !isBusy else { return }
        screen = .realtimeLive
        realtime.start(settings: settings, headband: headband)
    }

    /// The live screen goes back to the intro once the helper has released the headband.
    func realtimeEnded() {
        guard screen == .realtimeLive, realtime.phase == .stopped else { return }
        realtime.reset()
        screen = .realtimeIntro
    }

    func setThresholds(mi: Double, blink: Double) {
        settings.miThreshold = mi
        settings.blinkThreshold = blink
        realtime.setThresholds(mi: mi, blink: blink)
    }

    func startRecording() {
        guard let participant, !recorder.isRunning else { return }
        screen = .recording
        recorder.start(settings: settings, participant: participant, headband: headband)
    }

    func recordingEnded(context: ModelContext) {
        guard let session, case .finished(saved: true) = recorder.phase else { return }
        session.collectionDone = true
        session.recordingStartedAt = recorder.startedAt
        session.recordingEndedAt = recorder.endedAt
        session.imageryTrials = recorder.imageryDone
        session.blinks = recorder.blinksDone
        session.eegFile = recorder.eegFile
        session.trialLogFile = recorder.trialLogFile
        try? context.save()
        screen = .done
    }

    func finishVisit() {
        session = nil
        recorder.reset()
        screen = .home
    }

    // MARK: - Sidebar

    /// Whether clicking this step in the sidebar can jump to it.
    func canNavigate(to step: UserStep) -> Bool {
        guard participant != nil, screen != .firstTime, !isBusy else { return false }
        switch step {
        case .signIn, .calibration, .realtime: return true
        case .collect: return session?.calibrationDone == true && session?.collectionDone == false
        }
    }

    func navigate(to step: UserStep, context: ModelContext) {
        guard canNavigate(to: step) else { return }
        switch step {
        case .signIn:
            headband.stop()
            screen = .home
        case .calibration:
            if session == nil { resumeSession(context: context) }
            headband.stop()
            calibrationReturnsTo = .collectIntro
            screen = .calibrationIntro
        case .collect:
            screen = .collectIntro
        case .realtime:
            openRealtime()
        }
    }

    func state(of step: UserStep) -> StepState {
        if step.comingSoon { return .soon }
        let order: [UserScreen: UserStep] = [
            .signIn: .signIn, .firstTime: .signIn, .home: .calibration,
            .calibrationIntro: .calibration, .calibration: .calibration,
            .collectIntro: .collect, .recording: .collect, .done: .collect,
            .realtimeIntro: .realtime, .realtimeLive: .realtime,
        ]
        let current = order[screen] ?? .signIn
        if screen == .done && step == .collect { return .done }
        switch step {
        case .signIn: return participant == nil || screen == .firstTime ? .current : .done
        case .calibration:
            if current == .calibration { return .current }
            return session?.calibrationDone == true || calibratedThisVisit ? .done : .todo
        case .collect:
            if session?.collectionDone == true { return .done }
            return current == .collect ? .current : .todo
        case .realtime: return current == .realtime ? .current : .todo
        }
    }

    func subtitle(of step: UserStep) -> String {
        switch step {
        case .signIn: participant?.firstName ?? "Your full name"
        case .calibration: session?.calibrationDone == true ? "All sensors green" : "Fit headband, check sensors"
        case .collect:
            if session?.collectionDone == true { "Saved" }
            else if recorder.isRunning { "Recording, \(recorder.imageryDone + recorder.blinksDone) of \(RecorderRunner.imageryTarget + RecorderRunner.blinkTarget) trials" }
            else { "About 5 to 10 minutes" }
        case .realtime:
            if realtime.isRunning { realtime.vrConnected ? "Live · VR app connected" : "Live · VR app not open" }
            else { "Turn and select with your mind" }
        }
    }
}
