# BCI Navigator for macOS: Spec

Design reference: the "Round 2" rows of the design canvas at https://claude.ai/artifact/G4UU5N511Hxw6CJs7H5e1g

## 1. Purpose

A native macOS app that replaces the command-line workflow for the BCI-VR Navigation project. A participant signs in with their full name, fits and checks the Muse 2 headband, and records an EEG session without typing any `python` commands. The research team uses a separate admin mode to see every participant.

It replaces the legacy PyQt6 GUI (`src/pilot_data_collection/muse_eeg_gui.py`). Replacing the real-time PyQt6 GUI (`all_ml_models/realtime_feedback/realtime_feedback.py`) comes in a later version.

### Project description

EEG-based control system for VR navigation using the Muse 2 headband. Decodes left/right motor imagery and intentional blinks from 4-channel EEG to enable hands-free VR navigation in Unity, with no physical controllers required.

## 2. Design principles

- Easy to use: one obvious next action on every screen.
- Native macOS look: sidebar plus detail, standard controls.
- Large, readable type in user mode: 15 to 17 pt body, 34 to 36 pt headings.
- Avoid: web-app look, iOS-style full-width buttons, custom title bars.

## 3. Platform

- Target: macOS 26+, SwiftUI, Liquid Glass materials.
- Window: min 900x600, default 1200x800, remembers size and position.
- Light mode only. The app forces the light appearance.
- Accent color is system blue.

## 4. Modes

| | User mode | Admin mode |
|---|---|---|
| Who | The participant | The research team |
| Sees other participants | No | Yes, all of them |
| Sees session logs and file names | No | Yes |
| Entry | Default on launch | "Admin…" button on the Sign in sidebar, or View > Admin Mode (⇧⌘A) |

## 5. Layout

- **Sidebar**: a vertical step timeline (numbered circles joined by a line). Each step shows done (green check), current (filled blue), to do (outlined), or coming soon (dashed, disabled). The signed-in user's first name sits at the bottom with Sign Out.
- **Main area**: content for the current step, centered in a column 640 to 800 pt wide. Primary action bottom right, secondary bottom left.

User-mode steps: Sign in → Calibration → Collect EEG data → Real-time control (coming soon).

Navigation: every screen has a Back button to the previous screen. Sidebar steps are also clickable once signed in: Sign in goes to Home, Calibration to the calibration overview, and Collect EEG data to its instructions once calibration is done. The sidebar is locked while recording.

## 6. User mode screens

### 6.1 Sign in

- First name and last name fields. The app matches on the full name, case-insensitive.
- A known name goes to Home. An unknown name creates a profile with the next free subject number and goes to First sign-in.
- No list of other participants is ever shown.
- The user is addressed by first name everywhere.

### 6.2 First sign-in

- "Nice to meet you, {first name}", with a note to sign in with the same full name next time.
- Optional "About you": age and writing hand (Left, Right, Both). Only the research team sees these. Age accepts digits only and must be 1 to 120; otherwise an inline error shows and Continue is disabled.
- "What to expect today": Calibration, Record EEG data, Stop any time (progress saves after each step).
- Back deletes the profile that was just created and returns to Sign in, so a mistyped name leaves nothing behind.

### 6.3 Home

- "Hi, {first name}".
- Sessions completed and the date of the last one.
- The current session card with its saved progress: a bar with percent complete (0% before calibration, 50% after it), and one read-only line each for Calibration and Collect EEG data with a status tag (Not started, Next, Done). No circles or checkboxes, so nothing looks clickable. The primary button resumes at the first unfinished step.

### 6.4 Calibration overview

- Why calibration matters, in one sentence.
- The headband fitting video, "How to Fit Your Muse 2" (originally https://www.youtube.com/shorts/7UOlDKQPu8c, used with the owner's permission), bundled as `BCINavigator/Resources/MuseFitting.mp4` (H.264, 53 s) and played natively with AVKit in a sheet, so it works offline. The thumbnail is a frame from the video.
- Three steps: put on the headband, turn it on and connect, check the four sensors.
- Headband choice: "Which headband are you wearing?" with one card per lab headband, labelled by the last four characters of its serial, which is printed on the band: **15C3** (`Muse-15C3`) and **12A6** (`Muse-12A6`). The choice is remembered for next time and saved on the session.
- Tip: move hair away from the forehead and ears; take off glasses that press on the band.
- Primary button: Connect Headband {code}, disabled until a headband is chosen.

### 6.5 Calibration (sensor check)

- Head diagram with the four sensors, each green (good), yellow (adjust), or red (no contact), with a text status as well as color.
- Sensor list in plain language: "Behind left ear (TP9)", and so on.
- **Fit alert**: whenever any sensor is not green, a red banner at the top says "Headband not fitted correctly" and gives a one-line fix for the worst sensor. "Show Me How" scrolls to the fix card.
- Fix card: the fitting video plus numbered steps for the failing sensor.
- "Start Collecting EEG Data" is enabled only when all four sensors are green. Passing marks calibration done for this session.
- **Headband connection status**: a badge at the top right of the screen and a line under the user's name in the sidebar ("Headband 15C3 · Connected") show Not connected, Connecting…, Connected, Signal lost, or Connection failed. Sensor readings only show while connected; otherwise each sensor reads "Waiting".
- If the connection fails, a red banner explains why and offers Try Again. If samples stop for 3 seconds, a "Lost the signal" banner shows while it waits; after 12 seconds it gives up and offers Try Again.
- **Backend**: `macos_app/backend/muse_signal_check.py --serial <Muse-XXXX>`, run with the configured Python. It connects over BrainFlow (`MUSE_2_BOARD`, 3 attempts, 15 s each), feeds the stream into the team's `src.signal_quality.QualityMonitor`, and prints one JSON line every 0.5 s with each electrode's status, reason codes and RMS. It releases the headband on SIGTERM and exits if the app quits.
- **Quality rule**: owned by `src/signal_quality/` and documented in `docs/signal-quality.md` (raw 2 s window; saturation, flat line, RMS over 100/200 µV on TP9 and TP10 and over 400/800 µV on AF7 and AF8, 50/60 Hz line noise, packet-counter sample loss; three poor readings before poor, three clean seconds to recover). The app maps its statuses to the screen as good → Good, fair → Adjust, poor → Poor signal, unknown → Waiting. A `line_noise` reason changes the fix tip to "move away from chargers and power cables". On top of that, the helper applies the Muse app's own pass rule (ported from `MuseSignalQualityMonitor` in com.interaxon.muse 55.1): a sensor counts as ready only after 3 s of uninterrupted good readings, and any reading that is not good resets it to zero. Until then its row shows "Hold still" with a fill bar, and Start stays disabled until all four are ready.
- The app needs Bluetooth permission (`NSBluetoothAlwaysUsageDescription`); macOS asks the first time calibration connects.

### 6.6 Collect EEG data: instructions

Condensed from `EXPERIMENT_PROCEDURE.md`, matched to what the recorder runs.

- Part 1, 20 trials: on "Grab left" or "Grab right", imagine grabbing the cup with that hand, then press Space.
- Part 2, 10 trials: blink once deliberately and press B at the same moment.
- Before starting: forearms on the table, a cup about 5 cm from each hand, chin slightly raised, head still.
- In the recording window: press Space to connect to the headband, then Enter to start.
- Esc stops at any time; data recorded so far is saved.

### 6.7 Collect EEG data: recording

- Launches `src/pilot_data_collection/psychopy_recording/muse_psychopy_recording_structured.py --name "<full name>" --number <N> [--serial <serial>]` with the repo folder as the working directory, so files land in `<repo>/data/subNN/`. PsychoPy opens its own cue window.
- The app releases any Muse connection it holds first, because the Muse 2 accepts one Bluetooth connection at a time.
- The app stops its headband helper and waits for it to exit (force-quitting after 5 s) before launching the recorder, then passes the chosen serial with `--serial`.
- Shows a recording indicator, elapsed time, and per-part progress parsed from the recorder's output (`Started trial:`, `Ended trial:`, `Blink marker added:`). Runs Python unbuffered so lines arrive live.
- The recorder prints `Recording saved to <file>` on success and `RECORDING INCOMPLETE: <reason>` on a partial recording; the app treats the second as a failure and shows the reason.
- Stop Recording interrupts the process (SIGINT). If the recorder exits without writing an `eeg_data_*.csv`, the session is not marked collected and the user can try again.

### 6.8 Collect EEG data: session saved

- "Session {n} saved", imagery and blink counts, recording length.
- Reminder to take off the headband. Done returns Home; Sign Out returns to Sign in.
- No file names in user mode.

## 7. Admin mode

- Sidebar: All Participants, Session Log, Settings, Exit Admin.
- All Participants: table of subject, full name, sessions, last session, saved progress. Import Existing scans `<repo>/data/` for `subNN/metadata.yaml` and adds missing participants.
- Session Log: every session recorded through the app with participant, date, headband, counts, and the `eeg_data_*.csv` / `trial_log_*.csv` files, with Show in Finder.
- Settings: repo folder, Python interpreter, default headband (15C3, 12A6, or none).

## 8. Data

- Local database: SwiftData. `Participant` (first name, last name, subject number, age, writing hand, created date) and `SessionRecord` (number, dates, calibration done, collection done, trial counts, file names).
- Each participant maps to `<repo>/data/subNN/`, the layout the Python pipeline already uses. The recorder writes `eeg_data_*.csv`, `trial_log_*.csv` and `metadata.yaml` there. The trial log schema is unchanged.
- The recorder rewrites `metadata.yaml` on every run, so the app keeps age and writing hand in its own database and does not edit `metadata.yaml`.
- Sessions completed = the count of `eeg_data_*.csv` files in the participant's folder, which covers sessions recorded before the app existed.

## 9. Python integration

The app runs the existing Python code as child processes and reads their output. It does not reimplement signal processing or ML in Swift.

| CLI today | App equivalent |
|---|---|
| `python .../muse_psychopy_recording_structured.py --name --number --serial` | Collect EEG data |
| `python src/pilot_data_collection/stream_save_muse.py` | Calibration sensor check (`macos_app/backend/muse_signal_check.py`) |
| `python -m pipeline personalize` / `realtime` | Real-time control, later version |

The shipping target bundles the Python runtime inside the `.app`. Until that build step exists, Settings points at a local interpreter (default `<repo>/.venv/bin/python`) and repo checkout.

## 10. Later versions

- Tune the quality thresholds against fresh fitting sessions.
- Bundled Python runtime.
- Real-time control: personalize step, live prediction (class, confidence), MI and blink threshold sliders, Unity WebSocket state (`ws://127.0.0.1:8765`).
- Model training and cross-subject analysis stay on the command line.
