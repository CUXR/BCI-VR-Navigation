import SwiftData
import SwiftUI

struct SignInView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.modelContext) private var context
    @State private var firstName = ""
    @State private var lastName = ""
    @FocusState private var focused: Bool

    private var canContinue: Bool {
        !firstName.trimmingCharacters(in: .whitespaces).isEmpty
            && !lastName.trimmingCharacters(in: .whitespaces).isEmpty
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 28) {
            VStack(alignment: .leading, spacing: 14) {
                Image(systemName: "waveform.path.ecg")
                    .font(.system(size: 32, weight: .semibold))
                    .foregroundStyle(.white)
                    .frame(width: 68, height: 68)
                    .background(Color.accentColor, in: .rect(cornerRadius: 17))
                    .accessibilityHidden(true)
                Text("Welcome to BCI Navigator").font(.system(size: 34, weight: .bold)).kerning(-0.6)
                Text("Hands-free VR navigation with the Muse 2 headband. Sign in with your full name to pick up where you left off.")
                    .font(.system(size: 16)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 14) {
                LabeledField(title: "First name", text: $firstName, contentType: .givenName)
                    .focused($focused)
                LabeledField(title: "Last name", text: $lastName, contentType: .familyName)
            }
            Text("First time here? Enter your name and we will create your profile.")
                .font(.system(size: 13)).foregroundStyle(.secondary)
            HStack {
                Spacer()
                Button("Continue", action: submit)
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                    .keyboardShortcut(.defaultAction)
                    .disabled(!canContinue)
            }
        }
        .onSubmit(submit)
        .frame(width: 480)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Palette.window.ignoresSafeArea())
        .onAppear { focused = true }
    }

    private func submit() {
        guard canContinue else { return }
        model.signIn(firstName: firstName, lastName: lastName, context: context)
    }
}

struct LabeledField: View {
    var title: String
    @Binding var text: String
    var contentType: NSTextContentType?

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.system(size: 13, weight: .semibold)).foregroundStyle(.secondary)
            TextField(title, text: $text)
                .textContentType(contentType)
                .labelsHidden()
                .textFieldStyle(.plain)
                .font(.system(size: 16))
                .padding(.horizontal, 12)
                .frame(height: 40)
                .background(.white, in: .rect(cornerRadius: 10))
                .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(.black.opacity(0.14)))
        }
    }
}

struct FirstTimeView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.modelContext) private var context
    @State private var ageText = ""
    @State private var hand: WritingHand?

    static let ageRange = 1...120

    /// Empty is allowed (age is optional); anything else must be a whole number in range.
    private var age: Int? { Int(ageText) }
    private var ageIsValid: Bool { ageText.isEmpty || age.map(Self.ageRange.contains) == true }

    var body: some View {
        if let participant = model.participant {
            ScreenScaffold(width: 720) {
                ScreenHeader(
                    eyebrow: "New profile",
                    title: "Nice to meet you, \(participant.firstName)",
                    message: "We created a profile for \(participant.fullName). Next time, sign in with the same full name and the app will remember your progress."
                )
                VStack(alignment: .leading, spacing: 16) {
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Text("About you").font(.system(size: 19, weight: .bold))
                        Text("Optional, only the research team sees this").foregroundStyle(.secondary).font(.system(size: 14))
                    }
                    Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 14) {
                        GridRow {
                            Text("Age").font(.system(size: 16))
                            HStack(spacing: 12) {
                                TextField("Optional", text: $ageText)
                                    .textFieldStyle(.roundedBorder)
                                    .font(.system(size: 16))
                                    .frame(width: 140)
                                    .onChange(of: ageText) { _, new in
                                        // Digits only, at most three of them.
                                        let digits = String(new.filter { $0.isASCII && $0.isNumber }.prefix(3))
                                        if digits != new { ageText = digits }
                                    }
                                if !ageIsValid {
                                    Label("Enter an age from \(Self.ageRange.lowerBound) to \(Self.ageRange.upperBound)", systemImage: "exclamationmark.circle.fill")
                                        .font(.system(size: 14))
                                        .foregroundStyle(Palette.bad)
                                }
                            }
                        }
                        GridRow {
                            Text("Writing hand").font(.system(size: 16))
                            Picker("Writing hand", selection: $hand) {
                                ForEach(WritingHand.allCases) { Text($0.label).tag(Optional($0)) }
                            }
                            .pickerStyle(.segmented)
                            .labelsHidden()
                            .frame(width: 260)
                        }
                    }
                }
                .card(padding: 22)
                VStack(alignment: .leading, spacing: 14) {
                    Text("What to expect today").font(.system(size: 19, weight: .bold))
                    HStack(alignment: .top, spacing: 14) {
                        ExpectCard(number: 1, title: "Calibration", text: "Put on the headband with the help of a short video, then make sure all four sensors touch your skin.")
                        ExpectCard(number: 2, title: "Record EEG data", text: "Imagine grabbing a cup with your left or right hand when asked, then blink on cue. About 5 to 10 minutes.")
                        ExpectCard(number: 3, title: "Stop any time", text: "Your progress saves after each step, so you can take a break and pick up where you left off.")
                    }
                }
            } leading: {
                Button("Back") { model.undoNewProfile(context: context) }
                    .help("Go back to sign in and remove this new profile")
            } trailing: {
                Button("Continue") {
                    participant.age = age
                    participant.writingHand = hand
                    try? context.save()
                    model.screen = .home
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(!ageIsValid)
            }
        }
    }
}

private struct ExpectCard: View {
    var number: Int
    var title: String
    var text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            NumberBadge(number: number)
            Text(title).font(.system(size: 16, weight: .semibold))
            Text(text).font(.system(size: 14)).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .card(padding: 16)
    }
}

struct HomeView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.modelContext) private var context

    var body: some View {
        if let participant = model.participant {
            let completed = model.completedSessions(for: participant)
            let open = participant.openSession
            ScreenScaffold(width: 700) {
                ScreenHeader(title: "Hi, \(participant.firstName)",
                             message: completed == 0 ? "Let's set up your first session." : "Thanks for coming back. Here is where you are.")
                HStack(spacing: 16) {
                    StatCard(label: "Sessions completed", value: "\(completed)", detail: "Each one calibrated and recorded")
                    StatCard(label: "Last session",
                             value: model.lastSessionDate(for: participant)?.formatted(date: .long, time: .omitted) ?? "None yet",
                             detail: "Calibration and data collection",
                             valueSize: 28)
                }
                let calibrated = open?.calibrationDone == true
                // Two steps per session; collection finishing closes the session, so it is never done here.
                let percent = calibrated ? 50 : 0
                VStack(alignment: .leading, spacing: 16) {
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Text("Session \(open?.number ?? completed + 1)").font(.system(size: 20, weight: .bold))
                        Text("Progress is saved automatically").font(.system(size: 14)).foregroundStyle(.secondary)
                        Spacer()
                        Text("\(percent)% complete").font(.system(size: 16, weight: .semibold)).monospacedDigit()
                    }
                    ProgressView(value: Double(percent), total: 100)
                        .progressViewStyle(.linear)
                        .controlSize(.large)
                        .accessibilityLabel("Session progress")
                        .accessibilityValue("\(percent) percent")
                    VStack(spacing: 12) {
                        ProgressLine(title: "Calibration", detail: "Fit headband, check sensors",
                                     status: calibrated ? .done : .next)
                        ProgressLine(title: "Collect EEG data", detail: "About 5 to 10 minutes",
                                     status: calibrated ? .next : .notStarted)
                    }
                }
                .card(padding: 22)
            } leading: {
                Button("Back") { model.signOut() }
                    .help("Sign out and go back to sign in")
            } trailing: {
                Button(open?.calibrationDone == true ? "Continue to Recording" : "Start Calibration") {
                    model.resumeSession(context: context)
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
            }
        }
    }
}

private struct StatCard: View {
    var label: String
    var value: String
    var detail: String
    var valueSize: CGFloat = 44

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label).font(.system(size: 13, weight: .semibold)).foregroundStyle(.secondary)
            Text(value).font(.system(size: valueSize, weight: .bold)).monospacedDigit()
                .frame(minHeight: 52, alignment: .leading)
            Text(detail).font(.system(size: 14)).foregroundStyle(.secondary)
        }
        .card()
    }
}

/// A read-only step line: name and detail on the left, a status tag on the right.
private struct ProgressLine: View {
    enum Status {
        case notStarted, next, done

        var label: String {
            switch self {
            case .notStarted: "Not started"
            case .next: "Next"
            case .done: "Done"
            }
        }

        var foreground: Color {
            switch self {
            case .notStarted: .secondary
            case .next: .accentColor
            case .done: Palette.good
            }
        }

        var background: Color {
            switch self {
            case .notStarted: Color(white: 0.93)
            case .next: Color.accentColor.opacity(0.12)
            case .done: Palette.doneFill
            }
        }
    }

    var title: String
    var detail: String
    var status: Status

    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 16, weight: .medium))
                Text(detail).font(.system(size: 14)).foregroundStyle(.secondary)
            }
            Spacer()
            Text(status.label)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(status.foreground)
                .padding(.horizontal, 10)
                .padding(.vertical, 4)
                .background(status.background, in: .capsule)
        }
        .accessibilityElement(children: .combine)
    }
}
