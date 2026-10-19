import SwiftData
import SwiftUI

struct UserRootView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.modelContext) private var context

    var body: some View {
        NavigationSplitView {
            StepTimeline()
                .navigationSplitViewColumnWidth(min: 240, ideal: 264, max: 300)
        } detail: {
            Group {
                switch model.screen {
                case .signIn: SignInView()
                case .firstTime: FirstTimeView()
                case .home: HomeView()
                case .calibrationIntro: CalibrationIntroView()
                case .calibration: CalibrationView()
                case .collectIntro: CollectIntroView()
                case .recording: RecordingView()
                case .done: SessionDoneView()
                }
            }
            .font(.system(size: 15))
            .toolbarBackgroundVisibility(.hidden, for: .windowToolbar)
        }
        .navigationTitle("")
        .toolbar(removing: .title)
    }
}

/// Sidebar: numbered steps joined by a line, user chip at the bottom.
struct StepTimeline: View {
    @Environment(AppModel.self) private var model
    @Environment(\.modelContext) private var context

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(UserStep.allCases.enumerated()), id: \.element) { index, step in
                Button {
                    model.navigate(to: step, context: context)
                } label: {
                    StepRow(
                        number: index + 1,
                        title: step.title,
                        subtitle: model.subtitle(of: step),
                        state: model.state(of: step),
                        isLast: index == UserStep.allCases.count - 1
                    )
                    .contentShape(.rect)
                }
                .buttonStyle(.plain)
                .allowsHitTesting(model.canNavigate(to: step))
                .help(model.canNavigate(to: step) ? "Go to \(step.title)" : "")
            }
            Spacer()
            footer
        }
        .padding(.horizontal, 16)
        .padding(.top, 12)
        .padding(.bottom, 14)
    }

    @ViewBuilder private var footer: some View {
        if let participant = model.participant {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 10) {
                    Avatar(initial: String(participant.firstName.prefix(1)))
                    Text(participant.firstName).fontWeight(.semibold).lineLimit(1)
                    Spacer(minLength: 8)
                    Button("Sign Out") { model.signOut() }
                        .disabled(model.recorder.isRunning)
                }
                if let serial = model.headband.serial ?? model.session?.headband {
                    Divider()
                    HeadbandStatusLine(code: AppSettings.shortCode(serial), connection: model.headband.connection)
                }
            }
            .padding(12)
            .background(.quaternary.opacity(0.5), in: .rect(cornerRadius: 12))
        } else {
            Button {
                model.mode = .admin
            } label: {
                Label("Admin…", systemImage: "lock")
            }
            .buttonStyle(.borderless)
            .foregroundStyle(.secondary)
        }
    }
}

struct StepRow: View {
    var number: Int
    var title: String
    var subtitle: String
    var state: StepState
    var isLast: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            dot
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.system(size: 15, weight: state == .current ? .semibold : .regular))
                    .foregroundStyle(state == .soon ? .secondary : .primary)
                Text(subtitle).font(.system(size: 12)).foregroundStyle(.secondary)
            }
            .padding(.top, 5)
            .padding(.bottom, 24)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        // The connector runs from below this dot to the next row; as a background it never affects layout.
        .background(alignment: .topLeading) {
            if !isLast {
                Rectangle()
                    .fill(state == .done ? Palette.good.opacity(0.4) : Color(white: 0.9))
                    .frame(width: 2)
                    .padding(.top, 34)
                    .padding(.bottom, 4)
                    .padding(.leading, 14)
            }
        }
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder private var dot: some View {
        switch state {
        case .current:
            Text("\(number)").font(.system(size: 13, weight: .bold)).foregroundStyle(.white)
                .frame(width: 30, height: 30)
                .background(Color.accentColor, in: .circle)
                .background(Color.accentColor.opacity(0.15), in: .circle.inset(by: -4))
        case .done:
            Image(systemName: "checkmark").font(.system(size: 12, weight: .bold)).foregroundStyle(Palette.good)
                .frame(width: 30, height: 30)
                .background(Palette.doneFill, in: .circle)
        case .todo:
            Text("\(number)").font(.system(size: 13, weight: .bold)).foregroundStyle(.secondary)
                .frame(width: 30, height: 30)
                .overlay(Circle().strokeBorder(Color(white: 0.68), lineWidth: 1.5))
        case .soon:
            Circle().strokeBorder(Color(white: 0.68), style: StrokeStyle(lineWidth: 1.5, dash: [3, 3]))
                .frame(width: 30, height: 30)
        }
    }
}

struct Avatar: View {
    var initial: String
    var size: CGFloat = 32
    var body: some View {
        Text(initial.uppercased())
            .font(.system(size: size * 0.42, weight: .semibold))
            .foregroundStyle(.white)
            .frame(width: size, height: size)
            .background(Color.accentColor, in: .circle)
            .accessibilityHidden(true)
    }
}

/// "15C3 · Connected" with a colored dot, shown under the user's name.
struct HeadbandStatusLine: View {
    var code: String
    var connection: HeadbandConnection

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: "headphones").foregroundStyle(.secondary)
            Text(code).foregroundStyle(.secondary)
            Spacer(minLength: 6)
            Circle().fill(color).frame(width: 8, height: 8)
            Text(connection.label).fontWeight(.medium).foregroundStyle(color)
        }
        .font(.system(size: 13))
        .lineLimit(1)
        .accessibilityElement(children: .combine)
    }

    private var color: Color {
        switch connection {
        case .connected: Palette.good
        case .connecting, .signalLost: Palette.adjust
        case .failed: Palette.bad
        case .disconnected: Color(white: 0.68)
        }
    }
}
