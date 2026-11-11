# Environment one: standalone builds

Unity Editor and Unity Hub are not needed to run these builds.
Download these packages from [GitHub Releases](https://github.com/CUXR/BCI/releases/latest).

- **Mac:** open `BCIIndoor-mac.dmg`, then drag **BCI Indoor.app** to Applications.
  Supports Apple Silicon and Intel. Locally signed; not Apple-notarized.
- **Windows x64:** extract `BCIIndoor-windows.zip` and run `BCIIndoor.exe`.
  Keep the executable, `UnityPlayer.dll`, and the data folders together.
- **Meta Quest (ARM64):** enable developer mode and install `BCIIndoor.apk`
  with `adb install -r BCIIndoor.apk`, then launch it from Unknown Sources.

## Desktop controls

WASD or arrow keys move. Click in the window to capture the mouse and look
around; Escape releases it. Close the window to quit. No headset or Python
server is required for keyboard/mouse exploration.

## BCI control

Python inference and the Muse connection are separate from these environment
builds. All three builds connect to `ws://127.0.0.1:8765`.
For desktop, run the prediction server on the same computer. For Quest,
connect USB and run `adb reverse tcp:8765 tcp:8765`; repeat after reconnecting.
This APK does not expose a Wi-Fi host selector.

From the repository root, test without Muse:

```sh
uv run python tools/smoke_predictions.py
```

The helper prints `Quest connected` for any connected build, including desktop.
Enter `forward`, `backward`, `left`, or `right`. Stop the helper with `q` before
starting live inference because both use port 8765. See the root README for
live EEG setup and model limitations.

## Source and rebuilding

These packages were rebuilt on November 11, 2026 from environment one at commit
`740939be4c46ddff9c31b23f6302e5660c8025d7`, including the spawn-area floor fix,
relaxed market decor, cleared spawn view, and south-facing spawn. Both desktop
builds use XR-disabled settings.

Source is in `unity/environment_1/`. Use Unity 2022.3.0f1 with Mac, Windows Mono,
and Android build support. Build entry points are `BCIIndoorBuild.Mac`,
`BCIIndoorBuild.Windows`, and `BCIIndoorBuild.Build` (Quest), using
`-buildTarget StandaloneOSX`, `Win64`, and `Android`, respectively.
Outputs go to `unity/environment_1/Build/`; package them into this directory.
The Mac app is ad-hoc signed before creating the DMG. Regenerate
`SHA256SUMS.txt` after packaging.

## Verification

All three Unity builds completed successfully. The rebuilt Mac app launched,
connected to a local prediction WebSocket server, received test prediction frames,
and remained running. The Mac app signature, DMG checksum, Windows ZIP integrity,
and APK signature were verified. Windows and Quest runtime testing remains
pending; no headset was used. The APK is a development build for local
sideloading, not a store release.
