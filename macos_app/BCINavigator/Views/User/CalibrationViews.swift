import SwiftData
import SwiftUI

struct CalibrationIntroView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.modelContext) private var context

    var body: some View {
        ScreenScaffold(width: 800) {
            ScreenHeader(
                eyebrow: "Calibration",
                title: "Let's get your headband ready",
                message: "Calibration makes sure the headband can read your brain signals clearly. You do it at the start of every session, and it takes a few minutes."
            )
            HStack(alignment: .top, spacing: 24) {
                VideoButton(width: 300, height: 330)
                VStack(spacing: 12) {
                    IntroStep(number: 1, title: "Put on the headband", text: "Watch the video, then rest the band on your forehead with the ear pieces behind your ears.")
                    IntroStep(number: 2, title: "Turn it on and connect", text: "Hold the power button until the lights move. The app finds your headband on its own.")
                    IntroStep(number: 3, title: "Check the four sensors", text: "Each sensor turns green when it touches your skin. If one does not, we show you how to fix it.")
                }
            }
            VStack(alignment: .leading, spacing: 12) {
                Text("Which headband are you wearing?").font(.system(size: 19, weight: .bold))
                Text("Find the four-character code printed on the inside of the band.")
                    .font(.system(size: 15)).foregroundStyle(.secondary)
                HStack(spacing: 14) {
                    ForEach(AppSettings.headbands, id: \.self) { serial in
                        HeadbandChoice(serial: serial, selected: model.selectedHeadband == serial) {
                            model.selectHeadband(serial, context: context)
                        }
                    }
                }
            }
            Label {
                Text("Before you start, move hair away from your forehead and from behind your ears, and take off glasses if they press on the band.")
                    .fixedSize(horizontal: false, vertical: true)
            } icon: {
                Image(systemName: "info.circle").foregroundStyle(.tint)
            }
            .font(.system(size: 15))
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.accentColor.opacity(0.08), in: .rect(cornerRadius: 14))
        } leading: {
            Button("Back") { model.screen = .home }
        } trailing: {
            if model.selectedHeadband == nil {
                Text("Choose your headband first").font(.system(size: 14)).foregroundStyle(.secondary)
            }
            Button(model.selectedHeadband.map { "Connect Headband \(AppSettings.shortCode($0))" } ?? "Connect Headband") {
                model.beginSensorCheck()
            }
            .buttonStyle(.borderedProminent)
            .keyboardShortcut(.defaultAction)
            .disabled(model.selectedHeadband == nil)
        }
    }
}

/// One selectable headband, labelled by the code printed on the band.
private struct HeadbandChoice: View {
    var serial: String
    var selected: Bool
    var action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 14) {
                Image(systemName: "headphones")
                    .font(.system(size: 22))
                    .foregroundStyle(selected ? Color.white : Color.accentColor)
                    .frame(width: 44, height: 44)
                    .background(selected ? Color.accentColor : Color.accentColor.opacity(0.12), in: .circle)
                VStack(alignment: .leading, spacing: 2) {
                    Text(AppSettings.shortCode(serial))
                        .font(.system(size: 22, weight: .bold, design: .monospaced))
                    Text("Muse 2 headband").font(.system(size: 13)).foregroundStyle(.secondary)
                }
                Spacer()
                if selected {
                    Text("Selected")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(.tint)
                }
            }
            .padding(16)
            .frame(maxWidth: .infinity)
            .background(.white, in: .rect(cornerRadius: 16))
            .overlay(
                RoundedRectangle(cornerRadius: 16)
                    .strokeBorder(selected ? Color.accentColor : Color.black.opacity(0.1), lineWidth: selected ? 2 : 1)
            )
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Headband \(AppSettings.shortCode(serial))")
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

private struct IntroStep: View {
    var number: Int
    var title: String
    var text: String

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            NumberBadge(number: number)
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.system(size: 17, weight: .semibold))
                Text(text).font(.system(size: 15)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .card(padding: 16)
    }
}

struct CalibrationView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.modelContext) private var context

    var body: some View {
        let headband = model.headband
        let code = headband.serial.map(AppSettings.shortCode) ?? ""
        let worst = headband.worstSensor
        VStack(spacing: 0) {
                Group {
                    switch headband.connection {
                    case .failed(let message):
                        ConnectionAlert(title: "Couldn't connect to headband \(code)", message: message,
                                        actionTitle: "Try Again") { model.retryHeadband() }
                    case .signalLost:
                        ConnectionAlert(title: "Lost the signal from headband \(code)",
                                        message: "Keep the headband close to this computer and check that it is still on. Reconnecting…",
                                        actionTitle: nil, action: {})
                    default:
                        if let worst {
                            FitAlert(message: headband.referenceProblem
                                     ? MuseConnection.referenceQuickFix
                                     : worst.quickFix(reasons: headband.reasons[worst] ?? []))
                        }
                    }
                }
                .frame(maxWidth: 780)
                .padding(.horizontal, 32)
                .padding(.top, 16)
                .transition(.move(edge: .top).combined(with: .opacity))

                ScreenScaffold(width: 780) {
                    HStack(alignment: .firstTextBaseline) {
                        Text("Calibration · Headband \(code)")
                            .font(.system(size: 14, weight: .semibold)).foregroundStyle(.tint)
                        Spacer()
                        ConnectionBadge(connection: headband.connection)
                    }
                    .padding(.bottom, -16)
                    ScreenHeader(title: title(headband, code: code), message: message(headband))
                    HStack(spacing: 32) {
                        HeadDiagram(qualities: { headband.quality($0) }, size: 270)
                        VStack(alignment: .leading, spacing: 14) {
                            ForEach(Sensor.allCases) { sensor in
                                let q = headband.quality(sensor)
                                HStack(spacing: 12) {
                                    Circle().fill(Palette.color(q)).frame(width: 12, height: 12)
                                    Text("\(sensor.place) (\(sensor.rawValue))")
                                    Spacer()
                                    VStack(alignment: .trailing, spacing: 1) {
                                        if q == .good && !headband.isReady(sensor) {
                                            Text("Hold still").fontWeight(.semibold).foregroundStyle(Palette.color(q))
                                            ProgressView(value: headband.progress(sensor)).frame(width: 80)
                                        } else {
                                            Text(q.label).fontWeight(.semibold).foregroundStyle(Palette.color(q))
                                        }
                                        if q != .good, let why = QualityReason.summary(headband.reasons[sensor] ?? []) {
                                            Text(why).font(.system(size: 13)).foregroundStyle(.secondary)
                                        }
                                    }
                                }
                                .font(.system(size: 17))
                                .help(headband.noise[sensor].map { String(format: "RMS %.0f µV", $0) } ?? "")
                            }
                        }
                    }
                    if let worst {
                        HStack(alignment: .top, spacing: 20) {
                            VideoButton(width: 200, height: 170)
                            VStack(alignment: .leading, spacing: 10) {
                                Text(headband.referenceProblem ? MuseConnection.referenceFixTitle : worst.fixTitle)
                                    .font(.system(size: 19, weight: .bold))
                                ForEach(Array((headband.referenceProblem ? MuseConnection.referenceFixSteps : worst.fixSteps).enumerated()),
                                        id: \.offset) { index, step in
                                    Text("\(index + 1).  \(step)").font(.system(size: 16))
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                        }
                        .card(padding: 18)
                    }
                } leading: {
                    Button("Back") {
                        model.headband.stop()
                        model.screen = .calibrationIntro
                    }
                } trailing: {
                    if !headband.allGood {
                        Text(headband.connection.isLive ? "All four sensors must be green" : "Waiting for the headband")
                            .font(.system(size: 14)).foregroundStyle(.secondary)
                    }
                    Button("Start Collecting EEG Data") { model.finishCalibration(context: context) }
                        .buttonStyle(.borderedProminent)
                        .keyboardShortcut(.defaultAction)
                        .disabled(!headband.allGood)
                }
            }
        .background(Palette.window.ignoresSafeArea())
        .animation(.default, value: worst)
        .animation(.default, value: headband.connection)
    }

    private func title(_ headband: MuseConnection, code: String) -> String {
        switch headband.connection {
        case .connecting: "Connecting to headband \(code)"
        case .failed, .disconnected: "Headband not connected"
        case .signalLost: "Waiting for the signal"
        case .connected:
            if headband.qualities.isEmpty { "Reading the sensors…" }
            else if headband.allGood { "All four sensors ready" }
            else { "\(headband.readyCount) of 4 sensors ready" }
        }
    }

    private func message(_ headband: MuseConnection) -> String? {
        switch headband.connection {
        case .connecting(let attempt):
            "Make sure the headband is on and its lights are moving, and keep it close to this computer. This can take up to 15 seconds\(attempt > 1 ? " (try \(attempt) of 3)" : "")."
        case .connected where headband.allGood:
            "Great fit. Stay still and continue when you are ready."
        default:
            nil
        }
    }
}

/// Small status capsule: is the headband connected right now?
struct ConnectionBadge: View {
    var connection: HeadbandConnection

    var body: some View {
        HStack(spacing: 6) {
            if case .connecting = connection {
                ProgressView().controlSize(.small)
            } else {
                Circle().fill(color).frame(width: 9, height: 9)
            }
            Text(connection.label).font(.system(size: 13, weight: .semibold))
        }
        .foregroundStyle(color)
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(color.opacity(0.12), in: .capsule)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Headband \(connection.label)")
    }

    private var color: Color {
        switch connection {
        case .connected: Palette.good
        case .connecting, .signalLost: Palette.adjust
        case .failed: Palette.bad
        case .disconnected: .secondary
        }
    }
}

/// Banner for connection problems, styled like the fit alert.
struct ConnectionAlert: View {
    var title: String
    var message: String
    var actionTitle: String?
    var action: () -> Void

    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: "antenna.radiowaves.left.and.right.slash")
                .font(.system(size: 15, weight: .bold))
                .foregroundStyle(.white)
                .frame(width: 34, height: 34)
                .background(Palette.bad, in: .circle)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 16, weight: .bold))
                Text(message).font(.system(size: 15))
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .layoutPriority(1)
            .foregroundStyle(Color(red: 0.42, green: 0.0, blue: 0.06))
            Spacer()
            if let actionTitle {
                Button(actionTitle, action: action).controlSize(.large)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .background(Color(red: 0.99, green: 0.93, blue: 0.93), in: .rect(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Palette.bad.opacity(0.3)))
        .accessibilityElement(children: .combine)
    }
}

/// Red banner shown while the headband is not fitted correctly.
struct FitAlert: View {
    var message: String

    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: "exclamationmark")
                .font(.system(size: 16, weight: .heavy))
                .foregroundStyle(.white)
                .frame(width: 34, height: 34)
                .background(Palette.bad, in: .circle)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text("Headband not fitted correctly").font(.system(size: 16, weight: .bold))
                Text(message).font(.system(size: 15))
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .layoutPriority(1)
            .foregroundStyle(Color(red: 0.42, green: 0.0, blue: 0.06))
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .background(Color(red: 0.99, green: 0.93, blue: 0.93), in: .rect(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Palette.bad.opacity(0.3)))
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isStaticText)
    }
}
