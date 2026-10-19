import Foundation
import SwiftData

enum WritingHand: String, Codable, CaseIterable, Identifiable {
    case left, right, both
    var id: String { rawValue }
    var label: String { rawValue.capitalized }
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
    var collectionDone: Bool = false
    var recordingStartedAt: Date?
    var recordingEndedAt: Date?
    var imageryTrials: Int = 0
    var blinks: Int = 0
    var eegFile: String?
    var trialLogFile: String?
    /// Muse Bluetooth name used for this session, e.g. `Muse-15C3`.
    var headband: String?
    var participant: Participant?

    init(number: Int, startedAt: Date = .now) {
        self.number = number
        self.startedAt = startedAt
    }

    var recordingLength: TimeInterval? {
        guard let start = recordingStartedAt, let end = recordingEndedAt else { return nil }
        return end.timeIntervalSince(start)
    }
}
