import AVKit
import SwiftUI

enum Palette {
    static let good = Color(red: 0.12, green: 0.56, blue: 0.27)
    static let adjust = Color(red: 0.72, green: 0.43, blue: 0.0)
    static let bad = Color(red: 0.77, green: 0.0, blue: 0.10)
    static let doneFill = Color(red: 0.89, green: 0.96, blue: 0.91)
    static let window = Color(red: 0.97, green: 0.97, blue: 0.98)

    static func color(_ quality: SensorQuality) -> Color {
        switch quality {
        case .good: good
        case .adjust: adjust
        case .poor: bad
        case .unknown: Color(white: 0.62)
        }
    }
}

extension View {
    /// White rounded panel used for cards in the main area.
    func card(padding: CGFloat = 20) -> some View {
        self
            .padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.white, in: .rect(cornerRadius: 16))
            .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(.black.opacity(0.08)))
    }
}

/// Centered content column with a footer row: secondary actions leading, primary trailing.
struct ScreenScaffold<Content: View, Leading: View, Trailing: View>: View {
    var width: CGFloat = 760
    @ViewBuilder var content: Content
    @ViewBuilder var leading: Leading
    @ViewBuilder var trailing: Trailing

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) { content }
                    .frame(maxWidth: width, alignment: .leading)
                    .padding(.top, 44)
                    .padding(.horizontal, 32)
                    .frame(maxWidth: .infinity)
            }
            HStack(spacing: 12) {
                leading
                Spacer()
                trailing
            }
            .controlSize(.large)
            .frame(maxWidth: width)
            .padding(.horizontal, 32)
            .padding(.vertical, 20)
        }
        .background(Palette.window.ignoresSafeArea())
    }
}

struct ScreenHeader: View {
    var eyebrow: String?
    var title: String
    var message: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let eyebrow {
                Text(eyebrow).font(.system(size: 14, weight: .semibold)).foregroundStyle(.tint)
            }
            Text(title).font(.system(size: 36, weight: .bold)).kerning(-0.6)
            if let message {
                Text(message).font(.system(size: 17)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

struct NumberBadge: View {
    var number: Int
    var body: some View {
        Text("\(number)")
            .font(.system(size: 14, weight: .bold))
            .foregroundStyle(.tint)
            .frame(width: 30, height: 30)
            .background(Color.accentColor.opacity(0.12), in: .circle)
    }
}

struct KeyCap: View {
    var key: String
    var body: some View {
        Text(key)
            .font(.system(size: 15, weight: .semibold))
            .padding(.horizontal, 12)
            .frame(minWidth: 40, minHeight: 32)
            .background(Color(white: 0.98), in: .rect(cornerRadius: 8))
            .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(.black.opacity(0.16)))
            .shadow(color: .black.opacity(0.12), radius: 0, y: 2)
    }
}

// MARK: - Headband video

/// The Muse 2 fitting video, bundled in the app (used with the owner's permission) so it plays offline.
enum FittingVideo {
    static let url = Bundle.main.url(forResource: "MuseFitting", withExtension: "mp4")
    static let poster = Bundle.main.url(forResource: "MuseFittingPoster", withExtension: "jpg")
        .flatMap { NSImage(contentsOf: $0) }
}

/// Thumbnail button showing a frame of the fitting video; opens the player in a sheet.
struct VideoButton: View {
    var width: CGFloat = 300
    var height: CGFloat = 220
    var caption = "How to wear the Muse 2"
    @State private var showing = false

    var body: some View {
        Button { showing = true } label: {
            ZStack {
                if let poster = FittingVideo.poster {
                    Image(nsImage: poster)
                        .resizable()
                        .scaledToFill()
                        .frame(width: width, height: height)
                        .clipped()
                        .overlay(Color.black.opacity(0.35))
                } else {
                    Color(white: 0.17)
                }
                VStack(spacing: 12) {
                    Image(systemName: "play.fill")
                        .font(.system(size: 22))
                        .frame(width: 60, height: 60)
                        .background(.white.opacity(0.25), in: .circle)
                    Text(caption).font(.system(size: 15, weight: .semibold))
                        .shadow(color: .black.opacity(0.5), radius: 3)
                }
                .foregroundStyle(.white)
            }
            .frame(width: width, height: height)
            .clipShape(.rect(cornerRadius: 16))
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Play: \(caption)")
        .sheet(isPresented: $showing) { VideoSheet() }
    }
}

struct VideoSheet: View {
    @Environment(\.dismiss) private var dismiss
    @State private var player = FittingVideo.url.map { AVPlayer(url: $0) }

    var body: some View {
        VStack(spacing: 0) {
            if let player {
                PlayerView(player: player)
                    // The video is portrait, 592 x 1280.
                    .frame(width: 296, height: 640)
                    .onAppear { player.play() }
                    .onDisappear { player.pause() }
            } else {
                ContentUnavailableView("Video not found", systemImage: "film",
                                       description: Text("MuseFitting.mp4 is missing from the app bundle."))
                    .frame(width: 296, height: 640)
            }
            HStack {
                Button("Replay") {
                    player?.seek(to: .zero)
                    player?.play()
                }
                .disabled(player == nil)
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.defaultAction)
            }
            .controlSize(.large)
            .padding(16)
        }
        .frame(width: 296)
    }
}

/// AppKit's AVPlayerView. SwiftUI's `VideoPlayer` aborts while initializing its type metadata
/// on macOS 27 (crash in _AVKit_SwiftUI, 2026-10-18), so the sheet hosts the AppKit view directly.
struct PlayerView: NSViewRepresentable {
    let player: AVPlayer

    func makeNSView(context: Context) -> AVPlayerView {
        let view = AVPlayerView()
        view.player = player
        view.controlsStyle = .inline
        view.videoGravity = .resizeAspect
        return view
    }

    func updateNSView(_ view: AVPlayerView, context: Context) {
        if view.player !== player { view.player = player }
    }
}

// MARK: - Head diagram

/// Top-down head (nose up) with the four Muse sensors.
struct HeadDiagram: View {
    var qualities: (Sensor) -> SensorQuality
    var size: CGFloat = 280

    private static let positions: [Sensor: CGPoint] = [
        .tp9: CGPoint(x: 62, y: 182), .af7: CGPoint(x: 104, y: 72),
        .af8: CGPoint(x: 196, y: 72), .tp10: CGPoint(x: 238, y: 182),
    ]

    var body: some View {
        let scale = size / 300
        ZStack {
            Canvas { context, _ in
                context.scaleBy(x: scale, y: scale)
                let outline = Color(white: 0.82)
                let head = Path(ellipseIn: CGRect(x: 42, y: 32, width: 216, height: 236))
                var nose = Path()
                nose.move(to: CGPoint(x: 138, y: 34))
                nose.addLine(to: CGPoint(x: 150, y: 16))
                nose.addLine(to: CGPoint(x: 162, y: 34))
                for ear in [CGRect(x: 30, y: 130, width: 20, height: 44), CGRect(x: 250, y: 130, width: 20, height: 44)] {
                    context.fill(Path(ellipseIn: ear), with: .color(.white))
                    context.stroke(Path(ellipseIn: ear), with: .color(outline), lineWidth: 2)
                }
                context.fill(nose, with: .color(.white))
                context.stroke(nose, with: .color(outline), style: StrokeStyle(lineWidth: 2, lineJoin: .round))
                context.fill(head, with: .color(.white))
                context.stroke(head, with: .color(outline), lineWidth: 2)
                var band = Path()
                band.move(to: CGPoint(x: 62, y: 182))
                band.addCurve(to: CGPoint(x: 150, y: 58), control1: CGPoint(x: 60, y: 112), control2: CGPoint(x: 90, y: 62))
                band.addCurve(to: CGPoint(x: 238, y: 182), control1: CGPoint(x: 210, y: 62), control2: CGPoint(x: 240, y: 112))
                context.stroke(band, with: .color(.gray.opacity(0.3)), style: StrokeStyle(lineWidth: 5, lineCap: .round))
            }
            ForEach(Sensor.allCases) { sensor in
                let quality = qualities(sensor)
                let point = Self.positions[sensor]!
                VStack(spacing: 4) {
                    Image(systemName: Self.symbol(quality))
                        .font(.system(size: 13 * scale * 1.2, weight: .heavy))
                        .foregroundStyle(.white)
                        .frame(width: 30 * scale, height: 30 * scale)
                        .background(Palette.color(quality), in: .circle)
                        .overlay {
                            if quality == .poor {
                                Circle().strokeBorder(Palette.bad, style: StrokeStyle(lineWidth: 2, dash: [4, 4]))
                                    .frame(width: 46 * scale, height: 46 * scale)
                            }
                        }
                    Text(sensor.rawValue)
                        .font(.system(size: 14, weight: quality == .good ? .semibold : .bold))
                        .foregroundStyle(quality == .good ? Color.primary.opacity(0.8) : Palette.color(quality))
                }
                .position(x: point.x * scale, y: point.y * scale + 12 * scale)
            }
        }
        .frame(width: size, height: size * 284 / 300)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(Sensor.allCases.map { "\($0.place) \(qualities($0).label)" }.joined(separator: ", "))
    }

    static func symbol(_ quality: SensorQuality) -> String {
        switch quality {
        case .good: "checkmark"
        case .adjust: "exclamationmark"
        case .poor: "xmark"
        case .unknown: "ellipsis"
        }
    }
}
