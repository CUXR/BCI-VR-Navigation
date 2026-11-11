import AVFoundation

/// Plays one pre-recorded spoken cue from `Resources/Speech`. The clips are human-sounding
/// recordings made by `macos_app/backend/generate_speech_clips.py`; `manifest.json` maps each
/// exact phrase to its file.
final class SpokenClip: NSObject, AVAudioPlayerDelegate {
    private static let manifest: [String: String] = {
        guard let url = Bundle.main.url(forResource: "manifest", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let map = try? JSONDecoder().decode([String: String].self, from: data)
        else { return [:] }
        return map
    }()

    private let player: AVAudioPlayer
    private var done: CheckedContinuation<Void, Never>?

    /// Nil when no clip was recorded for this exact text.
    init?(text: String) {
        guard let file = Self.manifest[text],
              let url = Bundle.main.url(forResource: file, withExtension: nil),
              let player = try? AVAudioPlayer(contentsOf: url)
        else { return nil }
        self.player = player
        super.init()
        player.delegate = self
        player.prepareToPlay()
    }

    /// Returns when the clip ends or `stop()` is called.
    func play() async {
        await withCheckedContinuation { continuation in
            done = continuation
            if !player.play() { finish() }
        }
    }

    func stop() {
        player.stop()
        finish()
    }

    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor in self.finish() }
    }

    nonisolated func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        Task { @MainActor in self.finish() }
    }

    private func finish() {
        let current = done
        done = nil
        current?.resume()
    }
}
