import Foundation

/// Reads the `data/` layout the Python pipeline writes:
/// new recordings land in `data/subNN/`, older ones live in `data/Muse_Data/<Phase>/subNN/`.
struct DataFolder {
    let root: URL
    private let fm = FileManager.default

    struct RecordingFile: Identifiable, Hashable {
        let url: URL
        let date: Date?
        var id: URL { url }
    }

    struct ExistingSubject {
        let number: Int
        let name: String?
    }

    /// Where the recorder writes this subject's next session.
    func recordingFolder(subjectNumber: Int) -> URL {
        root.appending(path: String(format: "sub%02d", subjectNumber), directoryHint: .isDirectory)
    }

    /// Every `eeg_data_*.csv` for a subject, oldest first.
    func recordings(subjectNumber: Int) -> [RecordingFile] {
        subjectFolders(number: subjectNumber)
            .flatMap { folder in
                ((try? fm.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil)) ?? [])
                    .filter { $0.lastPathComponent.hasPrefix("eeg_data_") && $0.pathExtension == "csv" }
            }
            .map { RecordingFile(url: $0, date: Self.timestamp(in: $0.lastPathComponent)) }
            .sorted { ($0.date ?? .distantPast) < ($1.date ?? .distantPast) }
    }

    /// Subjects that already have a folder with `metadata.yaml`.
    func existingSubjects() -> [ExistingSubject] {
        var found: [Int: ExistingSubject] = [:]
        for folder in allSubjectFolders() {
            guard let number = Self.subjectNumber(folder.lastPathComponent) else { continue }
            let metadata = folder.appending(path: "metadata.yaml")
            guard fm.fileExists(atPath: metadata.path) else { continue }
            let name = Self.participantName(inMetadata: metadata)
            if found[number]?.name == nil {
                found[number] = ExistingSubject(number: number, name: name)
            }
        }
        return found.values.sorted { $0.number < $1.number }
    }

    func highestSubjectNumber() -> Int {
        allSubjectFolders().compactMap { Self.subjectNumber($0.lastPathComponent) }.max() ?? 0
    }

    // MARK: - Helpers

    private func subjectFolders(number: Int) -> [URL] {
        allSubjectFolders().filter { Self.subjectNumber($0.lastPathComponent) == number }
    }

    private func allSubjectFolders() -> [URL] {
        var candidates = children(of: root)
        for phase in children(of: root.appending(path: "Muse_Data")) {
            candidates += children(of: phase)
        }
        return candidates.filter { Self.subjectNumber($0.lastPathComponent) != nil }
    }

    private func children(of url: URL) -> [URL] {
        ((try? fm.contentsOfDirectory(at: url, includingPropertiesForKeys: [.isDirectoryKey])) ?? [])
            .filter { (try? $0.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true }
    }

    static func subjectNumber(_ folderName: String) -> Int? {
        guard folderName.hasPrefix("sub") else { return nil }
        return Int(folderName.dropFirst(3))
    }

    /// Parses `eeg_data_YYYYMMDD_HHMMSS.csv`.
    static func timestamp(in fileName: String) -> Date? {
        let stem = fileName.replacingOccurrences(of: "eeg_data_", with: "")
            .replacingOccurrences(of: ".csv", with: "")
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyyMMdd_HHmmss"
        return formatter.date(from: stem)
    }

    private static func participantName(inMetadata url: URL) -> String? {
        guard let text = try? String(contentsOf: url, encoding: .utf8) else { return nil }
        for line in text.split(separator: "\n") where line.hasPrefix("participant:") {
            let value = line.dropFirst("participant:".count).trimmingCharacters(in: .whitespaces)
            return value.isEmpty || value == "unknown" ? nil : value
        }
        return nil
    }
}
