import AppKit
import SwiftData
import SwiftUI

enum AdminSection: Hashable, CaseIterable, Identifiable {
    case participants, sessionLog, settings
    var id: Self { self }

    var title: String {
        switch self {
        case .participants: "All Participants"
        case .sessionLog: "Session Log"
        case .settings: "Settings"
        }
    }

    var symbol: String {
        switch self {
        case .participants: "person.2"
        case .sessionLog: "list.bullet.rectangle"
        case .settings: "gearshape"
        }
    }
}

struct AdminRootView: View {
    @Environment(AppModel.self) private var model
    @State private var section: AdminSection? = .participants

    var body: some View {
        NavigationSplitView {
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: 8) {
                    Text("ADMIN")
                        .font(.system(size: 12, weight: .bold))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(Color(white: 0.23), in: .rect(cornerRadius: 6))
                    Text("Research team").font(.system(size: 13)).foregroundStyle(.secondary)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
                List(AdminSection.allCases, selection: $section) { item in
                    Label(item.title, systemImage: item.symbol)
                        .font(.system(size: 15))
                        .tag(item)
                }
                Button("Exit Admin") { model.mode = .user }
                    .controlSize(.large)
                    .padding(16)
            }
            .navigationSplitViewColumnWidth(min: 220, ideal: 250, max: 300)
        } detail: {
            switch section ?? .participants {
            case .participants: AdminParticipantsView()
            case .sessionLog: AdminSessionLogView()
            case .settings: AdminSettingsView()
            }
        }
    }
}

struct AdminParticipantsView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.modelContext) private var context
    @Query(sort: \Participant.subjectNumber) private var participants: [Participant]
    @State private var search = ""
    @State private var selection: Participant.ID?

    private struct Row: Identifiable {
        let id: Participant.ID
        let subject: String
        let name: String
        let sessions: Int
        let last: Date?
        let progress: String
    }

    private var rows: [Row] {
        let folder = model.dataFolder
        return participants
            .filter { search.isEmpty || $0.fullName.localizedCaseInsensitiveContains(search) || $0.subjectLabel.contains(search) }
            .map { p in
                let recordings = folder.recordings(subjectNumber: p.subjectNumber)
                let progress: String = if let open = p.openSession {
                    open.calibrationDone ? "Session \(open.number) · calibrated" : "Session \(open.number) · started"
                } else {
                    recordings.isEmpty ? "Not started" : "All sessions complete"
                }
                return Row(id: p.id, subject: p.subjectLabel, name: p.fullName.isEmpty ? "–" : p.fullName,
                           sessions: recordings.count, last: recordings.last?.date, progress: progress)
            }
    }

    var body: some View {
        Table(rows, selection: $selection) {
            TableColumn("Subject", value: \.subject).width(80)
            TableColumn("Full name", value: \.name)
            TableColumn("Sessions") { Text("\($0.sessions)").monospacedDigit() }.width(80)
            TableColumn("Last session") { row in
                Text(row.last?.formatted(date: .abbreviated, time: .omitted) ?? "–")
            }
            .width(130)
            TableColumn("Saved progress", value: \.progress).width(190)
        }
        .font(.system(size: 14))
        .searchable(text: $search, placement: .toolbar)
        .navigationTitle("All Participants")
        .navigationSubtitle("\(participants.count) participants")
        .toolbar {
            ToolbarItem {
                Button("Import Existing", systemImage: "square.and.arrow.down") { importExisting() }
                    .help("Add participants found in the data folder")
            }
            ToolbarItem {
                Button("Show in Finder", systemImage: "folder") { revealSelected() }
                    .disabled(selection == nil)
            }
        }
        .overlay {
            if participants.isEmpty {
                ContentUnavailableView {
                    Label("No participants yet", systemImage: "person.2")
                } description: {
                    Text("Participants appear here after they sign in, or import the ones already in the data folder.")
                } actions: {
                    Button("Import Existing") { importExisting() }
                }
            }
        }
    }

    private func importExisting() {
        let known = Set(participants.map(\.subjectNumber))
        for subject in model.dataFolder.existingSubjects() where !known.contains(subject.number) {
            let parts = (subject.name ?? "").split(separator: " ", maxSplits: 1).map { String($0).capitalized }
            context.insert(Participant(firstName: parts.first ?? "", lastName: parts.count > 1 ? parts[1] : "",
                                       subjectNumber: subject.number))
        }
        try? context.save()
    }

    private func revealSelected() {
        guard let p = participants.first(where: { $0.id == selection }) else { return }
        let urls = model.dataFolder.recordings(subjectNumber: p.subjectNumber).map(\.url)
        if urls.isEmpty {
            NSWorkspace.shared.open(model.settings.dataURL)
        } else {
            NSWorkspace.shared.activateFileViewerSelecting(urls)
        }
    }
}

struct AdminSessionLogView: View {
    @Environment(AppModel.self) private var model
    @Query(sort: \SessionRecord.startedAt, order: .reverse) private var sessions: [SessionRecord]

    var body: some View {
        Table(sessions) {
            TableColumn("Participant") { s in
                Text(s.participant.map { "\($0.subjectLabel) · \($0.fullName)" } ?? "–")
            }
            TableColumn("Session") { Text("\($0.number)").monospacedDigit() }.width(60)
            TableColumn("Started") { Text($0.startedAt.formatted(date: .abbreviated, time: .shortened)) }.width(150)
            TableColumn("Status") { s in
                Text(s.collectionDone ? "Recorded" : s.calibrationDone ? "Calibrated" : "Started")
            }
            .width(90)
            TableColumn("Headband") { s in
                Text(s.headband.map(AppSettings.shortCode) ?? "–").monospaced()
            }
            .width(70)
            TableColumn("Imagery") { Text("\($0.imageryTrials)").monospacedDigit() }.width(60)
            TableColumn("Blinks") { Text("\($0.blinks)").monospacedDigit() }.width(50)
            TableColumn("Files") { s in
                HStack {
                    Text([s.eegFile, s.trialLogFile].compactMap { $0.map { URL(fileURLWithPath: $0).lastPathComponent } }.joined(separator: ", "))
                        .font(.system(size: 12, design: .monospaced))
                        .lineLimit(1).truncationMode(.middle)
                    if s.eegFile != nil {
                        Button("Show in Finder", systemImage: "folder") { reveal(s) }
                            .labelStyle(.iconOnly).buttonStyle(.borderless)
                    }
                }
            }
        }
        .font(.system(size: 14))
        .navigationTitle("Session Log")
        .navigationSubtitle("Sessions recorded through this app")
        .overlay {
            if sessions.isEmpty {
                ContentUnavailableView("No sessions yet", systemImage: "list.bullet.rectangle",
                                       description: Text("Sessions show up here once a participant starts calibration."))
            }
        }
    }

    private func reveal(_ session: SessionRecord) {
        let urls = [session.eegFile, session.trialLogFile].compactMap { $0 }.map { path in
            path.hasPrefix("/") ? URL(fileURLWithPath: path) : model.settings.repoURL.appending(path: path)
        }
        NSWorkspace.shared.activateFileViewerSelecting(urls)
    }
}

struct AdminSettingsView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var settings = model.settings
        Form {
            Section {
                PathRow(title: "Repo folder", path: $settings.repoPath, directory: true,
                        valid: FileManager.default.fileExists(atPath: settings.recorderScriptURL.path),
                        help: "The BCI checkout that contains src/ and data/.")
                PathRow(title: "Python interpreter", path: $settings.pythonPath, directory: false,
                        valid: FileManager.default.isExecutableFile(atPath: settings.pythonPath),
                        help: "An environment with BrainFlow and PsychoPy installed, for example .venv/bin/python.")
            } header: {
                Text("Recorder")
            }
            Section {
                Picker("Default headband", selection: $settings.museSerial) {
                    Text("None, participant chooses").tag("")
                    ForEach(AppSettings.headbands, id: \.self) { serial in
                        Text("\(AppSettings.shortCode(serial))  (\(serial))").tag(serial)
                    }
                }
                Text("Participants pick their headband on the calibration screen; this is the choice shown first.")
                    .font(.callout).foregroundStyle(.secondary)
            } header: {
                Text("Headband")
            }
            Section {
                LabeledContent("Sensor check", value: "Live from the headband over BrainFlow")
                Text("The team quality monitor in src/signal_quality (see docs/signal-quality.md) rates each sensor good, fair or poor from the last 2 seconds of raw EEG.")
                    .font(.callout).foregroundStyle(.secondary)
            } header: {
                Text("Calibration")
            }
        }
        .formStyle(.grouped)
        .navigationTitle("Settings")
    }
}

private struct PathRow: View {
    var title: String
    @Binding var path: String
    var directory: Bool
    var valid: Bool
    var help: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                TextField(title, text: $path)
                Image(systemName: valid ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                    .foregroundStyle(valid ? Palette.good : Palette.adjust)
                    .help(valid ? "Found" : "Not found")
                Button("Choose…") { choose() }
            }
            Text(help).font(.callout).foregroundStyle(.secondary)
        }
    }

    private func choose() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = directory
        panel.canChooseFiles = !directory
        panel.showsHiddenFiles = true
        panel.directoryURL = URL(fileURLWithPath: path).deletingLastPathComponent()
        if panel.runModal() == .OK, let url = panel.url { path = url.path }
    }
}
