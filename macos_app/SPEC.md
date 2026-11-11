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

User-mode steps: Sign in → Calibration → Collect EEG data → Real-time control.

Navigation: every screen has a Back button to the previous screen. Sidebar steps are also clickable once signed in: Sign in goes to Home, Calibration to the calibration overview, Collect EEG data to its instructions once calibration is done, and Real-time control to its get-ready screen. The sidebar and Sign Out are locked while recording or while real-time control is live.

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
- **Skip Check** (debugging): a secondary button beside Back leaves the sensor check without four green sensors. On the way to data collection it marks calibration done and sets `sensorCheckSkipped` on the session, which the admin Session Log shows as a warning icon. On the way to real-time control the checklist still reads "Not checked".
- **Headband connection status**: a badge at the top right of the screen and a line under the user's name in the sidebar ("Headband 15C3 · Connected") show Not connected, Connecting…, Connected, Signal lost, or Connection failed. Sensor readings only show while connected; otherwise each sensor reads "Waiting".
- If the connection fails, a red banner explains why and offers Try Again. If samples stop for 3 seconds, a "Lost the signal" banner shows while it waits; after 12 seconds it gives up and offers Try Again.
- **Backend**: `macos_app/backend/muse_signal_check.py --serial <Muse-XXXX>`, run with the configured Python. It connects over BrainFlow (`MUSE_2_BOARD`, 3 attempts, 15 s each), feeds the stream into the team's `src.signal_quality.QualityMonitor`, and prints one JSON line every 0.5 s with each electrode's status, reason codes and RMS. It releases the headband on SIGTERM and exits if the app quits.
- **Quality rule**: owned by `src/signal_quality/` and documented in [`eeg_to_meta/signal-quality.md`](../eeg_to_meta/signal-quality.md) (filter/feature pipeline, coupled horseshoe decisions, optional learned noise gate, and packet-counter sample loss). The app maps research statuses to the screen as good → Good, fair → Adjust, poor → Poor signal, unknown → Waiting. The helper requires 3 s of uninterrupted good readings before a sensor counts as ready, and any other reading resets it. Until then its row shows "Hold still" with a fill bar, and Start stays disabled until all four are ready. BrainFlow does not expose a DRL/REF pair; the calibration screen explicitly labels reference contact as unverified and readiness as EEG-only. A reference-disconnection alert requires measured DRL/REF evidence, rather than four poor channels alone.
- The app needs Bluetooth permission (`NSBluetoothAlwaysUsageDescription`); macOS asks the first time calibration connects.

### 6.6 Collect EEG data: instructions

Condensed from `EXPERIMENT_PROCEDURE.md`, matched to what the recorder runs.

- Part 1, 20 trials: on "Grab left" or "Grab right", imagine grabbing the cup with that hand, then press Space.
- Part 2, 20 Walk + 20 Relax trials: forward walking imagery (see 6.7a).
- Part 3, 10 trials: blink once deliberately and press B at the same moment.
- Each part card has a trial slider: 2–100 imagery trials (equal per hand), 0–50 Walk trials (the same number of Relax trials is added; 0 skips Part 2), 1–50 blinks.
- Before starting: forearms on the table, a cup about 5 cm from each hand, chin slightly raised, head still, both feet flat and still.
- After Start Recording the app connects to the headband; Enter (or Start) begins the protocol.
- Esc stops at any time; data recorded so far is saved.

### 6.7 Collect EEG data: recording

- The whole experiment runs in the app window (SwiftUI). No PsychoPy window opens. The protocol matches the former PsychoPy recorder: 10 rounds of Left then Right imagery (spoken instruction, full sentence in round 1 and "Grab left/right" after, played from pre-recorded clips in `Resources/Speech` made with OpenAI `gpt-4o-mini-tts` voice cedar by `backend/generate_speech_clips.py`; a phrase with no clip falls back to the macOS `say` voice Samantha and logs it), the trial opens when the instruction ends and closes on Space, a 3 s rest countdown follows each trial, then a spoken blink instruction and the blink block (B marks a blink, Enter finishes early, it ends by itself at 10).
- Screens: Connecting, Ready (Enter or Start begins), then a cue stage with a recording indicator, elapsed time, the current cue (colored "← LEFT" / "RIGHT →" circle), rest countdown or blink counter, and progress bars for both parts. A "Lost the headband signal" banner shows if samples stop for 3 s; recording continues.
- **Backend**: `macos_app/backend/muse_session_recorder.py --name "<full name>" --number <N> [--serial <serial>]`, run with the repo folder as the working directory, so files land in `<repo>/data/subNN/`. It owns the BrainFlow connection and uses `src/pilot_data_collection/session_recorder.py` (the recorder class formerly inside the PsychoPy script) to write `eeg_data_*.csv`, `trial_log_*.csv`, `signal_quality_*.jsonl` and `metadata.yaml`. The app sends JSON commands on stdin (`start`, `start_trial`, `end_trial`, `blink`, `stop`), each with `t`, the Unix time of the cue onset or key press; Swift's `Date` and BrainFlow's timestamps share the system clock, so trial times line up with EEG samples. The helper answers with JSON events on stdout (`status`, `recording`, `trial_ended`, `blink`, `saved`, `incomplete`, `error`); progress counts come from these confirmations, not from key presses.
- The app stops its headband helper and waits for it to exit (force-quitting after 5 s) before launching the recorder, because the Muse 2 accepts one Bluetooth connection at a time.
- Esc, or Stop and Save (with a confirmation), ends the session and keeps everything recorded so far. The helper also saves if it receives SIGTERM/SIGINT, its stdin closes, or the app quits. `incomplete` (reader or disk failure) shows as a failure with the reason. If no `eeg_data_*.csv` was written, the session is not marked collected and the user can try again.

### 6.7a Part 2: forward walking imagery

Goal: every participant imagines the same movement, not their own idea of "moving forward".

1. **Script** (about 10 s): one fixed spoken script, the same for everyone: "Now imagine walking forward. Feel your own legs step, one step per tone. Don't picture gliding or the room moving. Keep your real legs still. Let's practice once." Trial cues are "Walk. One step per tone." and "Relax. Stay still." the first time, then one word. A walking guide (figure plus Left/Right feet lighting in turn) shows the rhythm.
2. **Practice and explain-back** (about 1–2 min): one unrecorded 4 s Walk trial with one tone per second (if practice is set above one, a silent 4 s Relax separates the walks). The participant then tells the researcher what they imagined. The researcher clicks **Matches** (own legs stepping, own viewpoint) or **Does Not Match** (gliding, room moving, watching themselves, no legs), which replays the script and practice. After 3 non-matching attempts the session continues and is flagged.
3. **Record and check** (about 3–4 min for 20 + 20): Walk and Relax trials alternate (Walk, Relax, Walk, …), each 4 s, with no break between them; Relax is the gap between Walks. Only Walk has step tones; Relax is silent. Both use the same screen (cue word over a fixation cross). The first Walk and Relax cues are full sentences, later ones one word. Afterwards the participant answers: what did you imagine (own legs stepping / gliding / scenery moving / watching myself / something else / lost focus) and vividness 1–5.

Trial rows: `motor_imagery_forward` (label `forward`) and `baseline_quiet` (label `baseline`), block 2. Explain-back results and the post-block answers go to the helper as `{"cmd": "imagery_report", "report": {...}}` and are saved in `imagery_check_<timestamp>.json`, with `use_for_training` true only when explain-back matched, the answer was own legs stepping, and vividness was 3 or more. The admin Session Log shows Walk trials with a warning icon when a session fails those checks.

### 6.8 Collect EEG data: session saved

- "Session {n} saved", imagery, Walk + Relax, and blink counts, recording length.
- Reminder to take off the headband. Done returns Home; Sign Out returns to Sign in.
- No file names in user mode.

### 6.9 Real-time control

Design reference: https://claude.ai/artifact/XeH32hFAF7HmQJvyQsWgFz

- Reached from the sidebar, a "Real-time Control" button on Home, or "Try Real-time Control" on the session-saved screen. It does not require a recorded session.
- **Get ready**: what each thought does (imagine left hand → Turn left, right hand → Turn right, walking → Move forward, deliberate blink → Select), the model choice (Group model initially selected; Train/Retrain Your Model fits the signed-in participant's saved left/right and blink recordings, then selects Your own model), and a checklist: headband sensors checked this visit, and the VR app (optional).
- The sensor check is recommended, not required. Without it the row reads "Not checked" and Check Sensors sits beside Start. Check Sensors runs the normal calibration screens; passing returns here and does not mark the recording session's calibration done. The check is remembered until sign-out.
- **Live**: four tiles (← Left, ↑ Forward, Right →, Select) light in the action's color when recognized; a blink keeps its tile lit for 0.8 s. A confidence bar shows the recognized action's confidence, or the leading hand's confidence while resting, with a tick at the level needed, plus "Steady" when the smoother agrees. Status chips show the headband and whether the VR app is connected. Sensitivity sliders (30 to 95%, steps of 5; defaults 55% turning, 60% blink, from `eeg_to_meta/config.py`) apply on the next 100 ms tick and are remembered. Recent lists the last actions with confidence and age. Stop releases the headband and returns to Get ready. Nothing is recorded.
- **Backend**: `macos_app/backend/realtime_control.py [--serial <serial>] --mi-threshold <0-1> --blink-threshold <0-1>`, after the app stops its calibration helper. It reuses `eeg_to_meta` (ring buffer, InferenceEngine, PredictionSmoother, and either the group model `ml_pipeline/models/realtime_models.pkl` or the selected personal model via `--model <path> --participant NN`) and serves Unity on `ws://127.0.0.1:8765` with the same frames as `python -m pipeline realtime`. To the app it prints JSON lines (`status`, `model`, `prediction` with `intent` left/right/forward/blink/idle; forward appears only with a model that has an `mi_forward` class, `confidence`, `stable`, `leaning`, `leaning_confidence`, `vr` with the client count, `warning`, `error`); it accepts `{"cmd": "thresholds", "mi", "blink"}` and `{"cmd": "stop"}` on stdin. If port 8765 is taken, it keeps running without the VR link and says so.

## 7. Admin mode

- Sidebar: All Participants, Session Log, Settings, Exit Admin.
- All Participants: table of subject, full name, sessions, last session, saved progress. Import Existing scans `<repo>/data/` for `subNN/metadata.yaml` and adds missing participants.
- Session Log: every session recorded through the app with participant, date, headband, counts, and the `eeg_data_*.csv` / `trial_log_*.csv` files, with Show in Finder.
- Settings: repo folder, Python interpreter, default headband (15C3, 12A6, or none).

## 8. Data

- Local database: SwiftData. `Participant` (first name, last name, subject number, age, writing hand, created date) and `SessionRecord` (number, dates, calibration done, collection done, trial counts, file names, forward imagery checks: explain-back result and attempts, reported strategy, vividness).
- Each participant maps to `<repo>/data/subNN/`, the layout the Python pipeline already uses. The recorder writes `eeg_data_*.csv`, `trial_log_*.csv`, `imagery_check_*.json` and `metadata.yaml` there. The trial log schema is unchanged.
- The recorder rewrites `metadata.yaml` on every run, so the app keeps age and writing hand in its own database and does not edit `metadata.yaml`.
- Sessions completed = the count of `eeg_data_*.csv` files in the participant's folder, which covers sessions recorded before the app existed.

## 9. Python integration

The app runs the existing Python code as child processes and reads their output. It does not reimplement signal processing or ML in Swift.

| CLI today | App equivalent |
|---|---|
| `python .../muse_psychopy_recording_structured.py --name --number --serial` | Collect EEG data (in-app UI plus `macos_app/backend/muse_session_recorder.py`) |
| `python src/pilot_data_collection/stream_save_muse.py` | Calibration sensor check (`macos_app/backend/muse_signal_check.py`) |
| `python -m pipeline realtime` | Real-time control (`macos_app/backend/realtime_control.py`) |
| `python macos_app/backend/train_personal_model.py --participant NN` | Train/retrain Your own model from app recordings |

The shipping target bundles the Python runtime inside the `.app`. Until that build step exists, Settings points at a local interpreter (default `<repo>/.venv/bin/python`) and repo checkout.

## 10. Later versions

- Tune the quality thresholds against fresh fitting sessions.
- Bundled Python runtime.
- Personal-model control accuracy still needs headband validation across sessions; training and model selection are integrated.
- Group-model training and cross-subject analysis stay on the command line.
