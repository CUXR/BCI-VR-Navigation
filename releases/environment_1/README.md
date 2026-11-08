# Environment one: standalone builds

Unity Editor and Unity Hub are not needed to run these builds.

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

The requested `unity/` source directory is removed after packaging. An archive
of both environments, including the desktop build changes, is preserved outside
this repository on the build machine; its location is recorded in a local-only
`source-backup.txt`. The archive is not included in this release. The original
Unity projects also remain available in Git history before this release commit.
Extract it into the repository root to restore `unity/`. Environment one uses
Unity 2022.3.0f1 with Mac, Windows Mono, and Android build support.

Build entry points are `BCIIndoorBuild.Mac`, `BCIIndoorBuild.Windows`, and
`BCIIndoorBuild.Build` (Quest), using `-buildTarget StandaloneOSX`, `Win64`,
and `Android`, respectively. Outputs go to this directory.

## Verification

All three Unity builds completed successfully. The Mac app was launched and
visually checked, keyboard movement worked, and its prediction WebSocket
connected to a local test server. The DMG checksum, Windows ZIP integrity,
and APK signature were verified. Windows and Quest runtime testing remains
pending; no headset was connected. Android packaging warned that it could
not strip debug metadata from the bundled Oculus library and included the
library unchanged. The APK is for local sideloading, not a store release.
