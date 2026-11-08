import SwiftData
import SwiftUI

struct CollectIntroView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        ScreenScaffold(width: 800) {
            ScreenHeader(
                eyebrow: "Collect EEG data",
                title: "Here is what you will do",
                message: "This window guides you with spoken and on-screen cues. Turn your volume up. Choose how many trials to record in each part."
            )
            HStack(alignment: .top, spacing: 16) {
                PartCard(part: "Part 1 · \(model.recorder.imageryTarget) trials", title: "Imagine grabbing a cup",
                         text: "When you hear \"Grab left\" or \"Grab right\", imagine reaching out and grabbing the cup with that hand. Keep it short and natural, and do not actually move.",
                         key: "Space", keyHint: "when you finish imagining",
                         trials: Binding(get: { Double(model.recorder.imageryTarget) },
                                         set: { model.recorder.rounds = Int($0) / 2 }),
                         range: 2...100, step: 2,
                         trialHint: "\(model.recorder.rounds) trials per hand")
                PartCard(part: "Part 2 · \(model.recorder.blinkTarget) trials", title: "Blink on purpose",
                         text: "Blink once, deliberately, and press the key at the same moment. Repeat until the counter reaches \(model.recorder.blinkTarget).",
                         key: "B", keyHint: "at the same moment you blink",
                         trials: Binding(get: { Double(model.recorder.blinkTarget) },
                                         set: { model.recorder.blinkTarget = Int($0) }),
                         range: 1...50, step: 1, trialHint: "One deliberate blink per trial")
            }
            VStack(alignment: .leading, spacing: 10) {
                Text("Before you press Start").font(.system(size: 17, weight: .semibold))
                ForEach([
                    "Sit comfortably with your forearms resting on the table.",
                    "A cup sits about 5 cm from each hand, where you can see it.",
                    "Raise your chin slightly and keep your head still. Moving your eyes is fine.",
                    "After you press Start, the app connects to the headband. Press Enter when you are ready to begin.",
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
    @Binding var trials: Double
    var range: ClosedRange<Double>
    var step: Double
    var trialHint: String

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(part).font(.system(size: 13, weight: .semibold)).foregroundStyle(.secondary)
            Text(title).font(.system(size: 20, weight: .bold))
            Text(text).font(.system(size: 15)).fixedSize(horizontal: false, vertical: true)
            VStack(alignment: .leading, spacing: 4) {
                Slider(value: $trials, in: range, step: step) {
                    Text("Number of trials")
                }
                .accessibilityValue("\(Int(trials)) trials")
                Text(trialHint).font(.system(size: 13)).foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
            HStack(spacing: 10) {
                KeyCap(key: key)
                Text(keyHint)
            }
        }
        .card(padding: 20)
    }
}

/// Runs the whole recording in this window: cues, spoken instructions, key presses, and progress.
struct RecordingView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.modelContext) private var context
    @FocusState private var focused: Bool
    @State private var confirmStop = false

    var body: some View {
        let recorder = model.recorder
        ScreenScaffold(width: 760) {
            switch recorder.phase {
            case .idle, .starting:
                ScreenHeader(title: "Preparing to record", message: "Releasing the headband from the sensor check.")
                ProgressView()
            case .connecting(let attempt):
                ScreenHeader(title: "Connecting to the headband",
                             message: attempt > 1
                                 ? "Still trying (attempt \(attempt) of 3). Keep the headband on, turned on, and nearby."
                                 : "This takes a few seconds. Keep the headband on and sit still.")
                ProgressView()
            case .ready:
                ScreenHeader(eyebrow: "Collect EEG data", title: "Ready when you are",
                             message: "The headband is connected. Rest your forearms on the table, raise your chin slightly, and press Enter to begin.")
                keyLegend
            case .saving:
                ScreenHeader(title: "Saving your data", message: "This takes a moment. Keep the headband on until it finishes.")
                ProgressView()
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
            case .finished(saved: true):
                ScreenHeader(title: "Recording saved")
            default:
                session(recorder)
            }
        } leading: {
            if recorder.isRecording {
                HStack(spacing: 4) {
                    Text("Press")
                    KeyCap(key: "Esc").scaleEffect(0.8)
                    Text("to stop. Your data so far is saved.")
                }
                .font(.system(size: 14))
                .foregroundStyle(.secondary)
            } else if !recorder.isRunning {
                Button("Back") {
                    recorder.reset()
                    model.screen = .collectIntro
                }
            }
        } trailing: {
            trailing(recorder)
        }
        .focusable()
        .focusEffectDisabled()
        .focused($focused)
        .onAppear { focused = true }
        .onKeyPress(.space) {
            recorder.pressSpace()
            return .handled
        }
        .onKeyPress(.return) {
            recorder.pressEnter()
            return .handled
        }
        .onKeyPress(.escape) {
            guard recorder.canStop else { return .ignored }
            recorder.stopAndSave()
            return .handled
        }
        .onKeyPress(characters: CharacterSet(charactersIn: "bB"), phases: .down) { _ in
            recorder.pressBlink()
            return .handled
        }
        .onChange(of: recorder.phase, initial: true) { _, phase in
            // Keep Space, B, Enter and Esc working after a click or a dialog.
            focused = true
            if case .finished(saved: true) = phase { model.recordingEnded(context: context) }
        }
    }

    @ViewBuilder private func trailing(_ recorder: RecorderRunner) -> some View {
        switch recorder.phase {
        case .ready:
            Button("Cancel") { recorder.stopAndSave() }
            Button("Start") { recorder.begin() }
                .buttonStyle(.borderedProminent)
        case .connecting:
            Button("Cancel") { recorder.stopAndSave() }
        case .failed, .finished(saved: false):
            Button("Try Again") { model.startRecording() }
                .buttonStyle(.borderedProminent)
        default:
            if recorder.isRecording {
                Button("Stop and Save") { confirmStop = true }
                    .disabled(!recorder.canStop)
                    .confirmationDialog("Stop the recording now?", isPresented: $confirmStop) {
                        Button("Stop and Save", role: .destructive) { recorder.stopAndSave() }
                    } message: {
                        Text("Everything recorded so far is saved, and this session ends.")
                    }
            }
        }
    }

    @ViewBuilder private func session(_ recorder: RecorderRunner) -> some View {
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
        if recorder.connection == .signalLost {
            Label("Lost the headband signal. Sit still and check that the headband is on. Recording continues.",
                  systemImage: "exclamationmark.triangle.fill")
                .font(.system(size: 15, weight: .medium))
                .foregroundStyle(Palette.bad)
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Palette.bad.opacity(0.08), in: .rect(cornerRadius: 12))
        }
        CueStage(recorder: recorder)
            .frame(maxWidth: .infinity, minHeight: 340)
            .card(padding: 24)
        HStack(alignment: .top, spacing: 28) {
            PartProgress(title: "Part 1 · Imagine grabbing", done: recorder.imageryDone,
                         total: recorder.imageryTarget, detail: nil,
                         active: recorder.imageryDone < recorder.imageryTarget)
            PartProgress(title: "Part 2 · Blink on purpose", done: recorder.blinksDone,
                         total: recorder.blinkTarget, detail: nil,
                         active: recorder.imageryDone >= recorder.imageryTarget)
        }
        .card(padding: 20)
    }

    private var keyLegend: some View {
        HStack(spacing: 18) {
            Text("Keys").fontWeight(.semibold)
            HStack(spacing: 6) { KeyCap(key: "Space"); Text("done imagining") }
            HStack(spacing: 6) { KeyCap(key: "B"); Text("blink") }
            HStack(spacing: 6) { KeyCap(key: "Esc"); Text("stop and save") }
        }
        .card(padding: 14)
    }
}

private extension RecorderRunner.Cue {
    var color: Color {
        switch self {
        case .left: Color(red: 0.0, green: 0.45, blue: 0.85)
        case .right: Palette.good
        }
    }
    var arrowLabel: String { self == .left ? "← LEFT" : "RIGHT →" }
}

/// The large cue area: what the participant should do right now.
private struct CueStage: View {
    var recorder: RecorderRunner

    var body: some View {
        VStack(spacing: 18) {
            switch recorder.phase {
            case .listening(let cue):
                listening(title: "Round \(recorder.round) of \(recorder.rounds) · \(cue.title) hand", color: cue.color)
            case .imagining(let cue):
                ZStack {
                    Circle().fill(cue.color.opacity(0.1))
                    Circle().strokeBorder(cue.color, lineWidth: 8)
                    Text(cue.arrowLabel).font(.system(size: 52, weight: .heavy)).foregroundStyle(cue.color)
                }
                .frame(width: 220, height: 220)
                Text("Imagine grabbing the cup with your \(cue.rawValue) hand.")
                    .font(.system(size: 20, weight: .semibold))
                TimelineView(.periodic(from: .now, by: 0.1)) { context in
                    let remaining = max(0, (recorder.imageryReadyAt ?? context.date).timeIntervalSince(context.date))
                    if remaining > 0 {
                        Text("Keep imagining for \(Int(remaining.rounded(.up))) more seconds…")
                    } else {
                        HStack(spacing: 8) {
                            Text("Press")
                            KeyCap(key: "Space")
                            Text("when you finish imagining")
                        }
                    }
                }
                .font(.system(size: 16))
                .foregroundStyle(.secondary)
            case .resting(let until):
                TimelineView(.periodic(from: .now, by: 0.1)) { context in
                    Text("\(max(0, Int(until.timeIntervalSince(context.date).rounded(.up))))")
                        .font(.system(size: 96, weight: .bold)).monospacedDigit()
                        .foregroundStyle(.secondary)
                }
                Text("Rest. Relax and get ready for the next one.")
                    .font(.system(size: 20, weight: .semibold))
                    .foregroundStyle(.secondary)
            case .blinkIntro:
                listening(title: "Part 2 · Blink on purpose", color: Palette.adjust)
            case .blinking:
                Text("\(recorder.blinksDone) of \(recorder.blinkTarget)")
                    .font(.system(size: 72, weight: .bold)).monospacedDigit()
                    .foregroundStyle(Palette.adjust)
                TimelineView(.periodic(from: .now, by: 0.1)) { context in
                    let fresh = recorder.lastBlinkAt.map { context.date.timeIntervalSince($0) < 0.6 } ?? false
                    Label("Blink recorded", systemImage: "checkmark.circle.fill")
                        .font(.system(size: 18, weight: .semibold))
                        .foregroundStyle(Palette.good)
                        .opacity(fresh ? 1 : 0)
                }
                HStack(spacing: 8) {
                    Text("Blink once, on purpose, and press")
                    KeyCap(key: "B")
                    Text("at the same moment")
                }
                .font(.system(size: 18, weight: .semibold))
                if let notice = recorder.notice {
                    Text(notice).font(.system(size: 15)).foregroundStyle(Palette.bad)
                }
                HStack(spacing: 6) {
                    Text("Press")
                    KeyCap(key: "Enter").scaleEffect(0.8)
                    Text("to finish early")
                }
                .font(.system(size: 14))
                .foregroundStyle(.secondary)
            default:
                ProgressView()
                Text("Starting the recording…").font(.system(size: 18)).foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .animation(.easeOut(duration: 0.15), value: recorder.phase)
    }

    private func listening(title: String, color: Color) -> some View {
        VStack(spacing: 14) {
            Image(systemName: "speaker.wave.2.fill")
                .font(.system(size: 54))
                .foregroundStyle(color)
                .symbolEffect(.variableColor.iterative)
            Text("Listen to the instruction")
                .font(.system(size: 24, weight: .bold))
            Text(title).font(.system(size: 16)).foregroundStyle(.secondary)
        }
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
                        DoneStat(label: "Imagery trials", value: "\(session.imageryTrials) / \(model.recorder.imageryTarget)")
                        DoneStat(label: "Blinks", value: "\(session.blinks) / \(model.recorder.blinkTarget)")
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
                Button("Try Real-time Control") {
                    model.finishVisit()
                    model.openRealtime()
                }
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
