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

## Recorder lifecycle regression check

From the repo root, with Xcode selected:

```bash
xcrun swiftc -swift-version 6 -default-isolation MainActor \
  macos_app/BCINavigator/Models/*.swift macos_app/BCINavigator/Services/*.swift \
  macos_app/tests/RecorderLifecycleTests.swift -o /tmp/BCIRecorderLifecycleTests
/tmp/BCIRecorderLifecycleTests
```

This uses an in-memory participant database and a temporary Python recorder to check startup locking, duplicate launch prevention, output parsing, and session saving without a headband.
