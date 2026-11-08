import SwiftUI

private extension RealtimeRunner.Intent {
    var color: Color {
        switch self {
        case .left: Color(red: 0.0, green: 0.38, blue: 0.80)
        case .right: Color(red: 0.71, green: 0.28, blue: 0.05)
        case .blink, .idle: Color(red: 0.11, green: 0.11, blue: 0.12)
        }
    }

    var actionName: String {
        switch self {
        case .left: "Turn left"
        case .right: "Turn right"
        case .blink: "Select"
        case .idle: "Rest"
        }
    }
}

// MARK: - Get ready

struct RealtimeIntroView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        ScreenScaffold(width: 760) {
            ScreenHeader(eyebrow: "Real-time control", title: "Move in VR with your mind",
                         message: "The app reads your headband a few times a second and sends what it recognizes to the VR app. Nothing is recorded.")
            HStack(alignment: .top, spacing: 14) {
                MappingCard(thought: "Imagine your left hand", action: "Turn left", note: "Like the \"Grab left\" trials.", color: RealtimeRunner.Intent.left.color)
                MappingCard(thought: "Imagine your right hand", action: "Turn right", note: "Like the \"Grab right\" trials.", color: RealtimeRunner.Intent.right.color)
                MappingCard(thought: "Blink once, on purpose", action: "Select", note: "Normal blinking is ignored.", color: .primary)
            }
            VStack(alignment: .leading, spacing: 12) {
                Text("Which model reads your signal?").font(.system(size: 17, weight: .semibold))
                ModelOption(title: "Group model", detail: "Trained on earlier participants' recordings. Works right away.", selected: true)
                ModelOption(title: "Your own model", detail: "Trained on your recordings. Coming soon.", selected: false)
                    .disabled(true)
            }
            .card(padding: 20)
            VStack(alignment: .leading, spacing: 12) {
                Text("Before you start").font(.system(size: 17, weight: .semibold))
                ChecklistRow(done: model.calibratedThisVisit,
                             text: "Headband \(model.selectedHeadband.map(AppSettings.shortCode) ?? "") is on and all four sensors are green",
                             status: model.calibratedThisVisit ? "Ready" : "Not checked",
                             statusColor: model.calibratedThisVisit ? Palette.good : Palette.adjust)
                ChecklistRow(done: false, text: "Open the VR app on the headset. It connects by itself.",
                             status: "Optional", statusColor: Palette.adjust)
            }
            .card(padding: 20)
        } leading: {
            Button("Back") { model.screen = .home }
        } trailing: {
            if !model.calibratedThisVisit {
                Text("Skipping the sensor check can make control less reliable.")
                    .font(.system(size: 14)).foregroundStyle(.secondary)
                Button("Check Sensors") { model.checkSensorsForRealtime() }
            } else {
                Text("You can try it without the VR app.")
                    .font(.system(size: 14)).foregroundStyle(.secondary)
            }
            Button("Start Real-time Control") { model.startRealtime() }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
        }
    }
}

private struct MappingCard: View {
    var thought: String
    var action: String
    var note: String
    var color: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(thought).font(.system(size: 13, weight: .semibold)).foregroundStyle(.secondary)
            Text(action).font(.system(size: 22, weight: .bold)).foregroundStyle(color)
            Text(note).font(.system(size: 14)).foregroundStyle(.secondary)
        }
        .card(padding: 18)
        .accessibilityElement(children: .combine)
    }
}

private struct ModelOption: View {
    var title: String
    var detail: String
    var selected: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: selected ? "largecircle.fill.circle" : "circle")
                .foregroundStyle(selected ? Color.accentColor : .secondary)
                .font(.system(size: 16))
                .padding(.top, 2)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).fontWeight(.semibold)
                Text(detail).font(.system(size: 14)).foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }
        .foregroundStyle(selected ? .primary : .secondary)
        .padding(14)
        .background(selected ? Color.accentColor.opacity(0.05) : .clear, in: .rect(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12)
            .strokeBorder(selected ? Color.accentColor : .black.opacity(0.1), lineWidth: selected ? 2 : 1))
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

private struct ChecklistRow: View {
    var done: Bool
    var text: String
    var status: String
    var statusColor: Color

    var body: some View {
        HStack(spacing: 12) {
            if done {
                Image(systemName: "checkmark").font(.system(size: 11, weight: .bold)).foregroundStyle(Palette.good)
                    .frame(width: 24, height: 24).background(Palette.doneFill, in: .circle)
            } else {
                Circle().strokeBorder(Color(white: 0.68), lineWidth: 1.5).frame(width: 24, height: 24)
            }
            Text(text)
            Spacer()
            Text(status).font(.system(size: 14, weight: .semibold)).foregroundStyle(statusColor)
        }
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Live

struct RealtimeLiveView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        let runner = model.realtime
        ScreenScaffold(width: 760) {
            switch runner.phase {
            case .idle, .starting:
                ScreenHeader(title: "Getting ready", message: "Releasing the headband from the sensor check.")
                ProgressView()
            case .connecting(let attempt):
                ScreenHeader(title: "Connecting to the headband",
                             message: attempt > 1
                                 ? "Still trying (attempt \(attempt) of 3). Keep the headband on, turned on, and nearby."
                                 : "This takes a few seconds. Keep the headband on and sit still.")
                ProgressView()
            case .stopping, .stopped:
                ScreenHeader(title: "Stopping", message: "Releasing the headband.")
                ProgressView()
            case .failed(let message):
                ScreenHeader(title: "Real-time control stopped", message: message)
                if !runner.logTail.isEmpty {
                    ScrollView {
                        Text(runner.logTail.suffix(30).joined(separator: "\n"))
                            .font(.system(size: 12, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .frame(height: 220)
                    .card(padding: 14)
                }
            case .live:
                LivePanel(runner: runner, settings: model.settings)
            }
        } leading: {
            if !runner.isRunning {
                Button("Back") {
                    runner.reset()
                    model.screen = .realtimeIntro
                }
            } else {
                Text("Nothing is recorded. Stop any time.")
                    .font(.system(size: 14)).foregroundStyle(.secondary)
            }
        } trailing: {
            if case .failed = runner.phase {
                Button("Try Again") {
                    runner.reset()
                    model.startRealtime()
                }
                .buttonStyle(.borderedProminent)
            } else if runner.isRunning {
                Button("Stop", role: .destructive) { runner.stop() }
                    .disabled(runner.phase == .stopping)
                    .keyboardShortcut(.cancelAction)
            }
        }
        .onChange(of: runner.phase) { _, _ in model.realtimeEnded() }
    }
}

private struct LivePanel: View {
    @Environment(AppModel.self) private var model
    var runner: RealtimeRunner
    var settings: AppSettings

    var body: some View {
        HStack(alignment: .bottom, spacing: 12) {
            VStack(alignment: .leading, spacing: 6) {
                Text(runner.modelSubjects.map { "Real-time control · Group model (\($0) participants)" } ?? "Real-time control · Group model")
                    .font(.system(size: 14, weight: .semibold)).foregroundStyle(.tint)
                Text("Live control").font(.system(size: 36, weight: .bold)).kerning(-0.6)
            }
            Spacer()
            StatusChip(label: runner.connection == .signalLost ? "Signal lost" : "Headband",
                       color: runner.connection == .signalLost ? Palette.bad : Palette.good)
            StatusChip(label: runner.vrConnected ? "VR app" : "VR app not open",
                       color: runner.vrConnected ? Palette.good : Palette.adjust)
        }
        if let problem = runner.vrProblem {
            Label(problem, systemImage: "exclamationmark.triangle.fill")
                .font(.system(size: 14, weight: .medium))
                .foregroundStyle(Palette.adjust)
        }
        TimelineView(.periodic(from: .now, by: 0.1)) { context in
            RecognitionCard(runner: runner, settings: settings, now: context.date)
        }
        HStack(alignment: .top, spacing: 16) {
            SensitivityCard(settings: settings) { mi, blink in model.setThresholds(mi: mi, blink: blink) }
            RecentCard(recent: runner.recent)
        }
    }
}

private struct StatusChip: View {
    var label: String
    var color: Color

    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(color).frame(width: 8, height: 8)
            Text(label).font(.system(size: 13, weight: .medium))
        }
        .padding(.horizontal, 12)
        .frame(height: 30)
        .background(.white, in: .capsule)
        .overlay(Capsule().strokeBorder(.black.opacity(0.08)))
        .accessibilityElement(children: .combine)
    }
}

/// The three action tiles, the confidence bar, and a plain-language line about what is happening.
private struct RecognitionCard: View {
    var runner: RealtimeRunner
    var settings: AppSettings
    var now: Date

    /// A blink lasts one tick, so keep its tile lit long enough to see.
    private var showingBlink: Bool {
        runner.lastBlinkAt.map { now.timeIntervalSince($0) < 0.8 } ?? false
    }

    private var active: RealtimeRunner.Intent {
        if showingBlink { return .blink }
        return runner.intent == .left || runner.intent == .right ? runner.intent : .idle
    }

    var body: some View {
        let active = active
        let barValue = active == .blink ? runner.lastBlinkConfidence
            : active == .idle ? runner.leaningConfidence : runner.confidence
        let need = active == .blink ? settings.blinkThreshold : settings.miThreshold
        VStack(alignment: .leading, spacing: 18) {
            HStack(spacing: 12) {
                Tile(title: "← Left", detail: "Imagine left hand", intent: .left, on: active == .left)
                Tile(title: "Select", detail: "Blink on purpose", intent: .blink, on: active == .blink)
                Tile(title: "Right →", detail: "Imagine right hand", intent: .right, on: active == .right)
            }
            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text(headline(active)).font(.system(size: 17, weight: .semibold))
                    Spacer()
                    if active != .idle && (runner.stable || active == .blink) {
                        Text("Steady")
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundStyle(Palette.good)
                            .padding(.horizontal, 10).padding(.vertical, 3)
                            .background(Palette.doneFill, in: .capsule)
                    }
                    Text("\(Int((barValue * 100).rounded()))%")
                        .font(.system(size: 16, weight: .semibold)).monospacedDigit()
                }
                GeometryReader { proxy in
                    ZStack(alignment: .leading) {
                        Capsule().fill(Color(white: 0.9))
                        Capsule().fill(active == .idle ? Color(white: 0.68) : active.color)
                            .frame(width: proxy.size.width * min(max(barValue, 0), 1))
                        RoundedRectangle(cornerRadius: 1).fill(.primary)
                            .frame(width: 2, height: 20)
                            .offset(x: proxy.size.width * need - 1)
                    }
                }
                .frame(height: 12)
                .accessibilityElement()
                .accessibilityLabel("Confidence")
                .accessibilityValue("\(Int((barValue * 100).rounded())) percent, \(Int((need * 100).rounded())) percent needed")
                Text(helper(active, need: need))
                    .font(.system(size: 14)).foregroundStyle(.secondary)
            }
        }
        .card(padding: 22)
        .animation(.easeOut(duration: 0.15), value: active)
    }

    private func headline(_ active: RealtimeRunner.Intent) -> String {
        switch active {
        case .left: "Turning left"
        case .right: "Turning right"
        case .blink: "Select"
        case .idle: "Resting"
        }
    }

    private func helper(_ active: RealtimeRunner.Intent, need: Double) -> String {
        let percent = Int((need * 100).rounded())
        switch active {
        case .left, .right, .blink:
            return runner.vrConnected ? "Sent to the VR app." : "Recognized. Open the VR app to move with it."
        case .idle:
            if let leaning = runner.leaning, runner.leaningConfidence > 0 {
                return "Leaning \(leaning.rawValue) at \(Int((runner.leaningConfidence * 100).rounded()))%. Turning needs \(percent)%."
            }
            return "Nothing recognized. Relax, then imagine one hand reaching out to turn."
        }
    }
}

private struct Tile: View {
    var title: String
    var detail: String
    var intent: RealtimeRunner.Intent
    var on: Bool

    var body: some View {
        VStack(spacing: 6) {
            Text(title).font(.system(size: 30, weight: .heavy)).kerning(-0.4)
            Text(detail).font(.system(size: 14, weight: .medium))
        }
        .foregroundStyle(on ? Color.white : Color(white: 0.45))
        .frame(maxWidth: .infinity, minHeight: 148)
        .background(on ? intent.color : Palette.window, in: .rect(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(on ? intent.color : Color(white: 0.9), lineWidth: 2))
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(on ? .isSelected : [])
    }
}

private struct SensitivityCard: View {
    var settings: AppSettings
    var apply: (Double, Double) -> Void
    @State private var mi = 55.0
    @State private var blink = 60.0

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Sensitivity").font(.system(size: 17, weight: .semibold))
            slider("Turning needs", value: $mi)
            slider("Select (blink) needs", value: $blink)
            Text("Lower reacts more easily but makes more mistakes.")
                .font(.system(size: 13)).foregroundStyle(.secondary)
        }
        .card(padding: 20)
        .onAppear {
            mi = (settings.miThreshold * 100).rounded()
            blink = (settings.blinkThreshold * 100).rounded()
        }
        .onChange(of: mi) { apply(mi / 100, blink / 100) }
        .onChange(of: blink) { apply(mi / 100, blink / 100) }
    }

    private func slider(_ label: String, value: Binding<Double>) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(label)
                Spacer()
                Text("\(Int(value.wrappedValue))% sure").fontWeight(.semibold).monospacedDigit()
            }
            Slider(value: value, in: 30...95, step: 5)
                .accessibilityLabel(label)
                .accessibilityValue("\(Int(value.wrappedValue)) percent")
        }
    }
}

private struct RecentCard: View {
    var recent: [RealtimeRunner.Action]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Recent").font(.system(size: 17, weight: .semibold))
            if recent.isEmpty {
                Text("Actions the app recognizes show here.")
                    .font(.system(size: 14)).foregroundStyle(.secondary)
            }
            TimelineView(.periodic(from: .now, by: 1)) { context in
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(recent.prefix(4)) { action in
                        HStack(spacing: 10) {
                            Circle().fill(action.intent.color).frame(width: 10, height: 10)
                            Text(action.intent.actionName).fontWeight(.medium)
                            Spacer()
                            Text("\(Int((action.confidence * 100).rounded()))%")
                                .font(.system(size: 14)).foregroundStyle(.secondary).monospacedDigit()
                            Text(Self.ago(action.at, now: context.date))
                                .font(.system(size: 13)).foregroundStyle(.secondary).monospacedDigit()
                                .frame(width: 64, alignment: .trailing)
                        }
                        .accessibilityElement(children: .combine)
                    }
                }
            }
        }
        .card(padding: 20)
    }

    static func ago(_ date: Date, now: Date) -> String {
        let seconds = Int(now.timeIntervalSince(date))
        return seconds < 2 ? "now" : seconds < 60 ? "\(seconds) s ago" : "\(seconds / 60) min ago"
    }
}
