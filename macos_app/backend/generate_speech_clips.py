"""Render the app's spoken cues with OpenAI text-to-speech and save them as bundled audio clips.

The data collection app plays these clips instead of the macOS system voice, so every participant
hears the same human-sounding recording. Rerun this script whenever a phrase below changes; the app
matches clips to phrases by exact text through manifest.json, and any phrase without a clip falls
back to the system voice.

Usage (from the repo root):
    OPENAI_API_KEY=... python macos_app/backend/generate_speech_clips.py
"""

import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parents[1] / "BCINavigator" / "Resources" / "Speech"

MODEL = "gpt-4o-mini-tts"
VOICE = "cedar"
INSTRUCTIONS = (
    "You are a calm, warm research assistant guiding a participant through a brain recording. "
    "Speak naturally and clearly at a relaxed pace, like a real person sitting beside them. "
    "No dramatic emphasis."
)


def imagery_full(side: str) -> str:
    return (
        f"Now, imagine reaching out with your {side} hand to grab a cup on your {side} side. "
        f"Focus on the sensation of your {side} hand moving."
    )


# Clip name -> exact text. Must match the strings in RecorderRunner.swift.
PHRASES = {
    "cue_left_full": imagery_full("left"),
    "cue_right_full": imagery_full("right"),
    "cue_left_short": "Grab left",
    "cue_right_short": "Grab right",
    "blink_intro": (
        "Now we will record intentional blinks. Blink deliberately once, and press B at the same "
        "moment. Repeat until the counter reaches ten."
    ),
    "session_complete": "Experiment complete. Thank you for participating.",
}


def synthesize(text: str, api_key: str) -> bytes:
    body = json.dumps({
        "model": MODEL,
        "voice": VOICE,
        "input": text,
        "instructions": INSTRUCTIONS,
        "response_format": "wav",
    }).encode()
    request = urllib.request.Request(
        "https://api.openai.com/v1/audio/speech",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def main() -> None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        sys.exit("Set OPENAI_API_KEY first.")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {}
    with tempfile.TemporaryDirectory() as tmp:
        for name, text in PHRASES.items():
            wav = Path(tmp) / f"{name}.wav"
            wav.write_bytes(synthesize(text, api_key))
            m4a = OUT_DIR / f"{name}.m4a"
            subprocess.run(
                ["/usr/bin/afconvert", "-f", "m4af", "-d", "aac", "-b", "64000", str(wav), str(m4a)],
                check=True,
            )
            manifest[text] = m4a.name
            print(f"{m4a.name}: {text}")
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
