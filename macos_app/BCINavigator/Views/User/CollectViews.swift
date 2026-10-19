import SwiftData
import SwiftUI

struct CollectIntroView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        ScreenScaffold(width: 800) {
            ScreenHeader(
                eyebrow: "Collect EEG data",
                title: "Here is what you will do",
                message: "A new window will open and guide you with spoken and on-screen cues. It takes about 5 to 10 minutes."
            )
            HStack(alignment: .top, spacing: 16) {
                PartCard(part: "Part 1 · 20 trials", title: "Imagine grabbing a cup",
                         text: "When you hear \"Grab left\" or \"Grab right\", imagine reaching out and grabbing the cup with that hand. Keep it short and natural, and do not actually move.",
                         key: "Space", keyHint: "when you finish imagining")
                PartCard(part: "Part 2 · 10 trials", title: "Blink on purpose",
                         text: "Blink once, deliberately, and press the key at the same moment. Repeat until the counter reaches 10.",
                         key: "B", keyHint: "at the same moment you blink")
            }
            VStack(alignment: .leading, spacing: 10) {
                Text("Before you press Start").font(.system(size: 17, weight: .semibold))
                ForEach([
                    "Sit comfortably with your forearms resting on the table.",
                    "A cup sits about 5 cm from each hand, where you can see it.",
                    "Raise your chin slightly and keep your head still. Moving your eyes is fine.",
                    "In the recording window, press Space to connect to the headband, then Enter to begin.",
                ], id: \.self) { line in
                    Label(line, systemImage: "circle.fill")
                        .labelStyle(BulletLabelStyle())
                }
            }
            .card(padding: 20)
        } leading: {
            Button("Back") { model.beginSensorCheck() }
        } trailing: {
            HStack(spacing: 4) {
                Text("Press")
                KeyCap(key: "Esc").scaleEffect(0.8)
                Text("any time to stop. Your data so far is saved.")
            }
            .font(.system(size: 14))
            .foregroundStyle(.secondary)
            Button("Start Recording") { model.startRecording() }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
        }
    }
}

private struct BulletLabelStyle: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            configuration.icon.font(.system(size: 5)).foregroundStyle(.secondary)
            configuration.title.font(.system(size: 15))
        }
    }
}

private struct PartCard: View {
    var part: String
    var title: String
    var text: String
    var key: String
    var keyHint: String

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(part).font(.system(size: 13, weight: .semibold)).foregroundStyle(.secondary)
            Text(title).font(.system(size: 20, weight: .bold))
            Text(text).font(.system(size: 15)).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
            HStack(spacing: 10) {
                KeyCap(key: key)
                Text(keyHint)
            }
        }
        .card(padding: 20)
    }
}

struct RecordingView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.modelContext) private var context
    @State private var confirmStop = false

    var body: some View {
        let recorder = model.recorder
        ScreenScaffold(width: 760) {
            switch recorder.phase {
            case .failed(let message):
                ScreenHeader(title: "Recording could not run", message: message)
                if !recorder.logTail.isEmpty {
                    ScrollView {
                        Text(recorder.logTail.suffix(30).joined(separator: "\n"))
                            .font(.system(size: 12, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .frame(height: 220)
                    .card(padding: 14)
                }
            case .finished(saved: false):
                ScreenHeader(title: "Recording stopped",
                             message: "No EEG data was saved this time. Check that the headband is on and try again.")
            default:
                running(recorder)
            }
        } leading: {
            if !recorder.isRunning {
                Button("Back") {
                    recorder.reset()
                    model.screen = .collectIntro
                }
            } else {
                Text("Press Esc in the recording window to stop and keep your data.")
                    .font(.system(size: 14)).foregroundStyle(.secondary)
            }
        } trailing: {
            if recorder.isRunning {
                Button("Force Stop", role: .destructive) { confirmStop = true }
                    .confirmationDialog("Force stop the recording?", isPresented: $confirmStop) {
                        Button("Force Stop", role: .destructive) { recorder.forceStop() }
                    } message: {
                        Text("This closes the recording window without saving. To keep your data, press Esc in the recording window instead.")
                    }
            } else {
                Button("Try Again") { model.startRecording() }
                    .buttonStyle(.borderedProminent)
            }
        }
        .onChange(of: recorder.phase) { _, phase in
            if case .finished(saved: true) = phase { model.recordingEnded(context: context) }
        }
    }

    @ViewBuilder private func running(_ recorder: RecorderRunner) -> some View {
        HStack(spacing: 14) {
            Circle().fill(Palette.bad).frame(width: 18, height: 18)
                .background(Palette.bad.opacity(0.14), in: .circle.inset(by: -6))
            Text("Recording").font(.system(size: 36, weight: .bold))
            Spacer()
            if let start = recorder.startedAt {
                Text(start, style: .timer)
                    .font(.system(size: 34, weight: .semibold)).monospacedDigit()
                    .foregroundStyle(.secondary)
            }
        }
        Text("Follow the cues in the recording window. This window keeps track of your progress.")
            .font(.system(size: 17)).foregroundStyle(.secondary)
        VStack(alignment: .leading, spacing: 22) {
            PartProgress(
                title: "Part 1 · Imagine grabbing a cup",
                done: recorder.imageryDone, total: RecorderRunner.imageryTarget,
                detail: recorder.currentCue.map { "Now: \($0) · round \(recorder.imageryRound) of \(RecorderRunner.imageryTarget / 2)" }
                    ?? (recorder.imageryDone == 0 ? "Waiting for the recording window to start" : nil),
                active: recorder.imageryDone < RecorderRunner.imageryTarget
            )
            Divider()
            PartProgress(
                title: "Part 2 · Blink on purpose",
                done: recorder.blinksDone, total: RecorderRunner.blinkTarget,
                detail: recorder.imageryDone < RecorderRunner.imageryTarget ? "Starts after Part 1" : "Blink and press B",
                active: recorder.imageryDone >= RecorderRunner.imageryTarget
            )
        }
        .card(padding: 24)
        HStack(spacing: 18) {
            Text("Keys").fontWeight(.semibold)
            HStack(spacing: 6) { KeyCap(key: "Space"); Text("done imagining") }
            HStack(spacing: 6) { KeyCap(key: "B"); Text("blink") }
            HStack(spacing: 6) { KeyCap(key: "Esc"); Text("stop and save") }
        }
        .card(padding: 14)
    }
}

private struct PartProgress: View {
    var title: String
    var done: Int
    var total: Int
    var detail: String?
    var active: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                Text(title).font(.system(size: 18, weight: .bold))
                Spacer()
                Text("\(done) of \(total)").font(.system(size: 16, weight: .semibold)).monospacedDigit()
            }
            .foregroundStyle(active || done > 0 ? .primary : .secondary)
            ProgressView(value: Double(done), total: Double(total))
                .progressViewStyle(.linear)
                .controlSize(.large)
            if let detail {
                Text(detail).font(.system(size: 15)).foregroundStyle(.secondary)
            }
        }
    }
}

struct SessionDoneView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        if let participant = model.participant, let session = model.session {
            ScreenScaffold(width: 640) {
                VStack(spacing: 22) {
                    Image(systemName: "checkmark")
                        .font(.system(size: 40, weight: .bold))
                        .foregroundStyle(Palette.good)
                        .frame(width: 84, height: 84)
                        .background(Palette.doneFill, in: .circle)
                        .accessibilityHidden(true)
                    VStack(spacing: 8) {
                        Text("Session \(session.number) saved").font(.system(size: 36, weight: .bold))
                        Text("Thank you, \(participant.firstName). You can take off the headband now.")
                            .font(.system(size: 17)).foregroundStyle(.secondary)
                    }
                    HStack(spacing: 14) {
                        DoneStat(label: "Imagery trials", value: "\(session.imageryTrials) / \(RecorderRunner.imageryTarget)")
                        DoneStat(label: "Blinks", value: "\(session.blinks) / \(RecorderRunner.blinkTarget)")
                        DoneStat(label: "Time", value: session.recordingLength.map(Self.format) ?? "–")
                    }
                    Text("You have now completed \(model.completedSessions(for: participant)) sessions. See you next time.")
                        .font(.system(size: 15)).foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity)
                .padding(.top, 36)
            } leading: {
                Button("Sign Out") { model.signOut() }
            } trailing: {
                Button("Done") { model.finishVisit() }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
            }
        }
    }

    static func format(_ interval: TimeInterval) -> String {
        let seconds = Int(interval.rounded())
        return String(format: "%d:%02d", seconds / 60, seconds % 60)
    }
}

private struct DoneStat: View {
    var label: String
    var value: String
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).font(.system(size: 13, weight: .semibold)).foregroundStyle(.secondary)
            Text(value).font(.system(size: 30, weight: .bold)).monospacedDigit()
        }
        .card(padding: 18)
    }
}
