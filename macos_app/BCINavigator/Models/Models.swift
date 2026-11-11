import Foundation
import SwiftData

enum WritingHand: String, Codable, CaseIterable, Identifiable {
    case left, right, both
    var id: String { rawValue }
    var label: String { rawValue.capitalized }
}

/// What a participant reports imagining during the forward walking trials.
nonisolated enum ImageryStrategy: String, Codable, CaseIterable, Identifiable, Sendable {
    case ownLegs = "own_legs_stepping"
    case gliding
    case sceneryMoving = "scenery_moving"
    case watchingSelf = "watching_self"
    case other
    case lostFocus = "lost_focus"

    var id: String { rawValue }
    var label: String {
        switch self {
        case .ownLegs: "My own legs stepping"
        case .gliding: "Gliding or floating forward"
        case .sceneryMoving: "The room or scenery moving"
        case .watchingSelf: "Watching myself from outside"
        case .other: "Something else"
        case .lostFocus: "I lost focus"
        }
    }
    /// Only this answer matches the script everyone hears.
    var matchesScript: Bool { self == .ownLegs }
}

@Model
final class Participant {
    var firstName: String
    var lastName: String
    @Attribute(.unique) var subjectNumber: Int
    var age: Int?
    var writingHandRaw: String?
    var createdAt: Date
    @Relationship(deleteRule: .cascade, inverse: \SessionRecord.participant)
    var sessions: [SessionRecord] = []

    init(firstName: String, lastName: String, subjectNumber: Int, createdAt: Date = .now) {
        self.firstName = firstName
        self.lastName = lastName
        self.subjectNumber = subjectNumber
        self.createdAt = createdAt
    }

    var fullName: String {
        [firstName, lastName].filter { !$0.isEmpty }.joined(separator: " ")
    }

    /// Folder label the Python recorder uses, e.g. `sub05`.
    var subjectLabel: String { String(format: "sub%02d", subjectNumber) }

    var writingHand: WritingHand? {
        get { writingHandRaw.flatMap(WritingHand.init(rawValue:)) }
        set { writingHandRaw = newValue?.rawValue }
    }

    /// The session that has not finished collection yet, if any.
    var openSession: SessionRecord? {
        sessions.filter { !$0.collectionDone }.max { $0.number < $1.number }
    }

    static func normalized(_ name: String) -> String {
        name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    }
}

@Model
final class SessionRecord {
    var number: Int
    var startedAt: Date
    var calibrationDone: Bool = false
    /// Calibration was passed with the debug Skip Check button, not with four green sensors.
    var sensorCheckSkipped: Bool = false
    var collectionDone: Bool = false
    var recordingStartedAt: Date?
    var recordingEndedAt: Date?
    var imageryTrials: Int = 0
    var blinks: Int = 0
    /// Part 2 forward walking imagery: recorded Walk and Relax trials and the imagery checks.
    var forwardTrials: Int = 0
    var relaxTrials: Int = 0
    var explainBackPassed: Bool?
    var explainBackAttempts: Int = 0
    var forwardStrategyRaw: String?
    var forwardVividness: Int?
    var eegFile: String?
    var trialLogFile: String?
    /// Muse Bluetooth name used for this session, e.g. `Muse-15C3`.
    var headband: String?
    var participant: Participant?

    init(number: Int, startedAt: Date = .now) {
        self.number = number
        self.startedAt = startedAt
    }

    var forwardStrategy: ImageryStrategy? { forwardStrategyRaw.flatMap(ImageryStrategy.init(rawValue:)) }

    /// Walk trials from this session pass every check: explain-back, own-legs report, vividness 3+.
    var forwardUsable: Bool {
        explainBackPassed == true && forwardStrategy?.matchesScript == true && (forwardVividness ?? 0) >= 3
    }

    var recordingLength: TimeInterval? {
        guard let start = recordingStartedAt, let end = recordingEndedAt else { return nil }
        return end.timeIntervalSince(start)
    }
}
