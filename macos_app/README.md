# BCI Navigator (macOS)

A native app that guides a participant through one session: sign in, fit the Muse 2 headband with live per-sensor signal quality, then run the PsychoPy recorder. Press ⇧⌘A for the admin view (participants, session log, settings).

## Requirements

macOS 26, Xcode 26, [XcodeGen](https://github.com/yonaskolb/XcodeGen) (`brew install xcodegen`), and the repo's Python environment at `.venv` (`uv sync`).

## Build and run

```bash
cd macos_app
xcodegen generate
xcodebuild -project BCINavigator.xcodeproj -scheme BCINavigator -configuration Debug -derivedDataPath build build
open "build/Build/Products/Debug/BCI Navigator.app"
```

Behavior and design decisions are in `SPEC.md`.
