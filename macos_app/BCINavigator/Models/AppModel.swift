import Foundation
import Observation
import SwiftData

enum AppMode { case user, admin }

enum UserScreen: Equatable {
    case signIn, firstTime, home, calibrationIntro, calibration, collectIntro, recording, done
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

    var comingSoon: Bool { self == .realtime }
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
        guard !recorder.isRunning else { return }
        headband.stop()
        participant = nil
        session = nil
        recorder.reset()
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
        session?.calibrationDone = true
        try? context.save()
        screen = .collectIntro
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
        guard participant != nil, screen != .firstTime, !recorder.isRunning else { return false }
        switch step {
        case .signIn, .calibration: return true
        case .collect: return session?.calibrationDone == true && session?.collectionDone == false
        case .realtime: return false
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
            screen = .calibrationIntro
        case .collect:
            screen = .collectIntro
        case .realtime:
            break
        }
    }

    func state(of step: UserStep) -> StepState {
        if step.comingSoon { return .soon }
        let order: [UserScreen: UserStep] = [
            .signIn: .signIn, .firstTime: .signIn, .home: .calibration,
            .calibrationIntro: .calibration, .calibration: .calibration,
            .collectIntro: .collect, .recording: .collect, .done: .collect,
        ]
        let current = order[screen] ?? .signIn
        if screen == .done && step == .collect { return .done }
        switch step {
        case .signIn: return participant == nil || screen == .firstTime ? .current : .done
        case .calibration:
            if session?.calibrationDone == true { return .done }
            return current == .calibration ? .current : .todo
        case .collect:
            if session?.collectionDone == true { return .done }
            return current == .collect ? .current : .todo
        case .realtime: return .soon
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
        case .realtime: "Coming soon"
        }
    }
}
