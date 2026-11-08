# BCI Navigator (macOS)

A native app that guides a participant through one session: sign in, fit the Muse 2 headband with live per-sensor signal quality, record the EEG session with in-app cues (no PsychoPy window), and try real-time control with the group model or a model trained on their own recordings. Press ⇧⌘A for the admin view (participants, session log, settings).

## Requirements

macOS 26, Xcode 26, [XcodeGen](https://github.com/yonaskolb/XcodeGen) (`brew install xcodegen`), and the repo's Python environment at `.venv` (`uv sync`).

## Build and run

```bash
cd macos_app
xcodegen generate
xcodebuild -project BCINavigator.xcodeproj -scheme BCINavigator -configuration Debug -derivedDataPath build build
open "build/Build/Products/Debug/BCI Navigator.app"
```

Before recording, use the sliders in the two part cards to choose 2–100 imagery trials (equal numbers per hand) and 1–50 blinks. Defaults are 20 imagery trials and 10 blinks.

Behavior and design decisions are in `SPEC.md`.

## Personal model training

On **Real-time control → Get ready**, choose **Train Your Model**. Training runs locally
using the signed-in participant's paired `eeg_data_<timestamp>.csv` and
`trial_log_<timestamp>.csv` files in `data/subNN/` and `data/Muse_Data/<phase>/subNN/`.
It reuses `pipeline.personalize`'s Random Forest hand classifier and LDA blink classifier
through the left/right recording adapter in `pipeline/personalize_app.py`.
The separate four-direction collector workflow is unchanged.

After training, **Your own model** is selected and is passed to the real-time helper.
**Retrain Your Model** includes newly saved recordings. Cancel or a failed retrain
preserves the previous bundle; **Group model** remains available. Each participant's
bundle is saved as `ml_pipeline/models/participants/subNN_app.pkl` and rediscovered
on later visits. These files are ignored by Git.

At least two usable trials per hand, two deliberate blinks, and two non-blink windows
are required. Saved MI trials are not rejected for signal quality during personal
training; duration and EEG coverage requirements still apply. Blink windows are
centered on the recorded keypress; training skips ASR to match live preprocessing.
Hand cues require three seconds before Space can finish a trial. Saving waits for
the final blink's post-event EEG. If training cannot proceed, the message lists
saved and usable counts, short trials, and missing EEG coverage.
This minimum makes fitting possible; it does not establish reliable control or an
improvement over the group model. Bundle cross-validation scores are exploratory
within-recording estimates, not held-out-session validation. Test with the headband.

The Python environment also needs the ML dependencies (NumPy, SciPy, pandas,
scikit-learn, and MNE). To train outside the UI:

```bash
.venv/bin/python macos_app/backend/train_personal_model.py --participant 5
```

## Regression checks

From the repo root, with Xcode selected:

```bash
xcrun swiftc -swift-version 6 -default-isolation MainActor \
  macos_app/BCINavigator/Models/*.swift macos_app/BCINavigator/Services/*.swift \
  macos_app/tests/RecorderLifecycleTests.swift -o /tmp/BCIRecorderLifecycleTests
/tmp/BCIRecorderLifecycleTests
```

Real-time control has the same kind of check: compile `macos_app/tests/RealtimeRunnerTests.swift` the same way and run it. It uses a fake `realtime_control.py` to check the optional sensor check, navigation lock, prediction handling, the action log, live sensitivity changes, and stop.

Compile `macos_app/tests/PersonalModelTrainerTests.swift` the same way to check training,
cancellation, failed retraining, participant isolation, and selected-model launch.
Run `.venv/bin/python -m unittest discover -s tests -p test_personalize_app.py` for the
recording adapter, atomic save, and trained-bundle inference compatibility checks.

To try the real-time helper without a headband: `.venv/bin/python macos_app/backend/realtime_control.py --mock`.

This uses an in-memory participant database and a fake recording helper to run the full protocol with custom counts (6 imagery trials, 4 blinks), after checking the 20/10 defaults, Esc partway through, and cancel before start, checking startup locking, single launch, timestamps on every command, and session saving without a headband.

To try the real helper without a headband: `.venv/bin/python macos_app/backend/muse_session_recorder.py --name test --number 99 --simulate`, then type JSON commands such as `{"cmd": "start"}` (files go to `simulated_data/sub99/`).
