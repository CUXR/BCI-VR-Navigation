# BCI-VR Navigation Control

EEG-powered VR navigation using the Muse 2 headband and Unity. Imagine turning left, turning right, or walking forward to navigate the environment.

The native macOS app, BCI Navigator, guides you through headband setup, EEG recording, personal model training, and live control.

## Demos

**Real-time brainwave decoding** — EEG, band powers, and model feedback.

[<img src="docs/media/model-feedback.gif" alt="Real-time Muse EEG and model feedback" width="100%">](https://www.youtube.com/watch?v=xWrw_kHVDX8)

[Watch on YouTube](https://www.youtube.com/watch?v=xWrw_kHVDX8)

**VR navigation** — Quest indoor navigation and prediction output.

[<img src="docs/media/vr-navigation.gif" alt="Quest VR navigation and prediction output" width="100%">](https://www.youtube.com/watch?v=tk2hOVV74W4)

[Watch on YouTube](https://www.youtube.com/watch?v=tk2hOVV74W4)

## Control Scheme

| Brain Signal | Action in `FreeNavScene` | Model support |
|---|---|---|
| Left motor imagery | Rotate left | Group and personal models |
| Right motor imagery | Rotate right | Group and personal models |
| Forward walking imagery | Move forward | Personal model trained with eligible Walk trials |
| Intentional blink | Detected; interaction not yet mapped | Blink classifier |
| Idle / baseline | No action | Personal models can learn idle from Relax trials |

The separate four-direction pipeline also supports backward movement with a
compatible model. See [Classification Results](#classification-results) for
offline evaluation scores.

## Hardware

- **Muse 2 headband** — 4 dry EEG electrodes at 256 Hz via Bluetooth
- **Channels:** TP9 (left temporal), AF7 (left frontal), AF8 (right frontal), TP10 (right temporal)
- **Interface:** BrainFlow SDK

## Get Started with BCI Navigator

1. Download `BCINavigator-mac.dmg` from [GitHub Releases](https://github.com/CUXR/BCI/releases/latest) and install the app. Requires macOS 26 or later.
2. In this repository, run `uv sync --python 3.11`.
3. Open the app's admin view with ⇧⌘A. Set **Settings → Repo folder** to your checkout and **Python interpreter** to `<checkout>/.venv/bin/python`.
4. Sign in, connect the Muse 2, check sensor quality, and record a session.
5. Under **Real-time control → Get ready**, choose **Train Your Model** or use the **Group model**.
6. Start live control, then choose **Open VR environment** to launch BCI Indoor on your Mac.

Recording includes left/right imagery, forward Walk and Relax trials, and
deliberate blinks, with recorded voice cues. Forward trials are eligible for
training only when their block's imagery check approves them. Personal training
performance still requires live validation.

See the [app setup instructions](macos_app/README.md#download) for installation
and the [environment build guide](releases/environment_1/README.md) for the Mac,
Windows, and Quest packages.

## Alternative Research Workflow: PsychoPy and PyQt

Use this workflow to collect data with PsychoPy, train the historical classifiers,
and inspect predictions in the PyQt feedback GUI.

```bash
# 1. Setup (Python 3.11; install uv first: https://docs.astral.sh/uv/getting-started/installation/)
uv sync --python 3.11

# 2. Get the recorded data (NOT committed to this repo)
#    Download the Muse_Data zip from Google Drive, then unzip into data/:
#    https://drive.google.com/drive/folders/1bHQ3Cm5NYfPvOb-EGZXeE6YcODsiLqth?usp=sharing
unzip ~/Downloads/Muse_Data-*.zip -d data/
# See data/README.md for the expected layout.

# 3. (Optional) Collect new data — requires a Muse headband
#    --serial: Muse Bluetooth name (Muse-15C3 or Muse-12A6)
#    Omit --serial to get an in-app device selection screen
#    without hardware： add --simulate

uv run python src/pilot_data_collection/psychopy_recording/muse_psychopy_recording_structured.py --name test --number 9 --serial Muse-15C3

# 4. Train classifiers
uv run python all_ml_models/realtime_feedback/train_and_save_models.py

# 5. Run real-time feedback
uv run python all_ml_models/realtime_feedback/realtime_feedback.py              # with Muse
uv run python all_ml_models/realtime_feedback/realtime_feedback.py --serial Muse-15C3  # specific Muse
uv run python all_ml_models/realtime_feedback/realtime_feedback.py --simulate   # without hardware

# Optional: check the Muse connection by recording 10 seconds to temp/
uv run python src/pilot_data_collection/stream_save_muse.py
# Select a specific headband if more than one Muse is nearby:
uv run python src/pilot_data_collection/stream_save_muse.py --serial Muse-15C3
```

## Check Muse Signal Quality

Check TP9, AF7, AF8, and TP10 individually before recording:

```bash
uv run python -m src.signal_quality --serial 15C3
# Without a headband:
uv run python -m src.signal_quality --simulate --seconds 10
```

The backend reports signal quality separately from transport/recording errors.
The current recorder and Unity inference pipeline include live monitoring.
See [signal-quality setup, thresholds, and validation](eeg_to_meta/signal-quality.md).

## Run the Unity Navigation System

Standalone environment-one builds and instructions are in
[`releases/environment_1/`](releases/environment_1/README.md). The Mac and Windows
apps support keyboard/mouse navigation without a headset or Unity Editor.
The Quest APK uses VR tracking. Python inference remains a separate process.


With the Muse 2 on and the Quest app running `FreeNavScene`, connect the Quest
over USB and forward its prediction socket:

```bash
adb reverse tcp:8765 tcp:8765
```

From the repository root, start live EEG inference with the existing model:

```bash
uv run python -m pipeline realtime \
  --model ml_pipeline/models/realtime_models.pkl \
  --skip-personalize \
  --serial 15C3 \
  --mi-threshold 0.70 \
  --blink-threshold 0.85
```

### Quest connection

The packaged APK connects to `127.0.0.1:8765`. Use the USB `adb reverse`
command above to reach the prediction server on your computer. This APK does
not have a runtime Wi-Fi host selector. A direct LAN connection requires
changing the prediction host in `unity/environment_1/` and rebuilding; see the
[source and build instructions](releases/environment_1/README.md#source-and-rebuilding).
The desktop apps connect to the prediction server on the same computer.

`--serial` accepts either `15C3` or `Muse-15C3`. An explicit `--model` needs no
`--participant`. The included model is a legacy two-class bundle for left/right
turns. BCI Navigator personal models can add `mi_forward` and `idle` from eligible
Walk and Relax recordings. The separate [four-direction pipeline](pipeline/README.md)
uses `mi_forward`, `mi_backward`, `mi_rotate_left`, and `mi_rotate_right` for
forward/backward movement and turning.

`--mi-threshold 0.70` requires at least 70% model confidence before recognizing
a motor imagery direction. You can adjust it; the default is 0.55. It also
applies to the included left/right model.
`--blink-threshold 0.85` requires at least 85% blink probability before
recognizing an intentional blink; the default is 0.60.

The Python server sends at most one valid navigation command every 3 seconds.
The scene moves for up to 0.5 seconds after an accepted frame; unstable,
low-confidence, stale, or disconnected input does not sustain movement.

## System Architecture

BCI Navigator coordinates recording, personal training, and live inference through
Python helpers. Predictions stream over WebSocket port `8765` to Unity's
`PredictionWebSocketClient` and `FreeNavController` on desktop or Quest.

The alternative PyQt feedback workflow is shown below:

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Muse 2 EEG     │────>│  BrainFlow SDK   │────>│  Processing     │
│  4ch @ 256 Hz   │ BT  │  Ring Buffer     │     │  Pipeline       │
└─────────────────┘     └──────────────────┘     └────────┬────────┘
                                                          │
                        ┌─────────────────────────────────┤
                        │                                 │
                 ┌──────▼───────┐               ┌────────▼────────┐
                 │ Blink Detect │               │ Motor Imagery   │
                 │ LDA, 500ms   │               │ RF, 3s window   │
                 └──────┬───────┘               └────────┬────────┘
                        │                                 │
                        └─────────────┬───────────────────┘
                                      │
                              ┌───────▼───────┐
                              │  PyQt6 GUI    │
                              │  Real-time    │
                              │  Feedback     │
                              └───────────────┘
```

### Three Layers

1. **Hardware / Streaming** — BrainFlow interface for Muse 2. Streams EEG to CSV or rebroadcasts over multicast for multiple consumers.

2. **Experiment / Recording** — BCI Navigator provides guided in-app recording with voice cues; the alternative PsychoPy workflow uses visual cues. Both save paired EEG and trial-log CSVs. See [Data Collection](#data-collection) and [Experiment Procedure](EXPERIMENT_PROCEDURE.md).

3. **Analysis / ML** — Signal processing (MNE bandpass/notch filtering, artifact removal), feature extraction (band power, Hjorth, CSP, time-frequency), and classification (LDA, SVM, RF). Three pipeline versions tested across 4 subjects.

## Project Structure

```
macos_app/                             # BCI Navigator app and Python helpers
pipeline/                              # Collection, training, personalization, realtime CLI
ml_pipeline/                           # Models and feature pipeline used by Quest inference
eeg_to_meta/                           # Muse acquisition and prediction WebSocket server
unity/environment_1/                   # Unity source for the packaged navigation environment
releases/environment_1/                # Standalone Mac, Windows, and Quest builds

all_ml_models/
  training/                             # Offline ML training pipelines
    bandpower/
      train_motor_imagery.py            #   v1: band power + Hjorth + CSP
    timefreq/
      train_motor_imagery_v2_timefreq.py  # v2: + time-frequency features
    eegnet/
      train_motor_imagery_v3_eegnet.py  #   v3: + EEGNet-inspired features
  realtime_feedback/                    # Real-time feedback system
    train_and_save_models.py            #   Train & save models for real-time use
    realtime_feedback.py                #   PyQt6 GUI with live classification
  train_blink_detector.py              # Blink detection pipeline
  models/
    realtime_models.pkl                # Trained model bundle for real-time use

src/
  signal_processing/
    eeg_filters.py                     # MNE-based bandpass, notch, artifact removal
  pilot_data_collection/               # Pilot EEG data collection scripts
    psychopy_recording/
      muse_psychopy_recording_structured.py  # Data collection experiment
    stream_save_muse.py                # Basic Muse streaming to CSV
    rebroadcast_producer.py            # Multicast EEG streaming (producer)
    rebroadcast_consumer.py            # Multicast EEG streaming (consumer)
    muse_eeg_gui.py                    # Legacy PyQt6 GUI (superseded)
  realtime_monitoring/
    stream_realtime_bandpower.py       # Live band power computation
    visualize_data_live.py             # Real-time EEG + band power plots

data/                                  # Downloaded from Google Drive — see data/README.md
  README.md
  Muse_Data/
    Phase1/sub{NN}/                    # Per-subject session
      eeg_data_YYYYMMDD_HHMMSS.csv     #   Raw EEG (timestamp + 4 channels)
      trial_log_YYYYMMDD_HHMMSS.csv    #   Trial metadata
      metadata.yaml
    Phase2/sub{NN}/                    # Repeat sessions (currently sub03, sub05)

data_analysis/                         # See data_analysis/README.md
  _common.py                           #   Shared discovery + loading helpers
  raw/                                 #   Per-subject raw-signal views
    generate.py                        #     plot_01/02/04
  signal_level/                        #   PSD + band power, per/cross-subject
    generate.py                        #     plot_03/06/07/08 + cross_subject_*
  ml/
    results.md                         #   Classification results write-up
```

## Data Collection

### BCI Navigator

The app records left/right imagery, forward Walk and Relax trials, and deliberate
blinks. Defaults are 20 hand-imagery trials, 20 Walk + 20 Relax trials, and 10
blinks. The walking block includes instructions, practice, and an explain-back
check; Walk and Relax trials last four seconds.

Sessions save paired EEG and trial-log CSVs in `data/subNN/`, plus
`imagery_check_<timestamp>.json` for walking checks. Personal training uses approved
walking blocks and saves each participant's model to
`ml_pipeline/models/participants/subNN_app.pkl`. Hand-only recordings retain the
left/right model. See the [app guide](macos_app/README.md) for recording and training.

### PsychoPy

The PsychoPy workflow records two CSVs per session:

**EEG data** — continuous 4-channel EEG with timestamps aligned to BrainFlow's session clock:
```
timestamp, marker_code, marker_label, EEG_0(TP9), EEG_1(AF7), EEG_2(AF8), EEG_3(TP10)
```

**Trial log** — one row per trial with consistent schema:
```
trial_id, trial_type, label, start_time, end_time, event_marker, duration, session_id
```

Trial types: `motor_imagery_left`, `motor_imagery_right`, `blink_intentional`, `baseline_quiet`, `baseline_active`

Motor imagery epochs are interval-based (cue onset to spacebar press, 2–5s). Blink epochs are event-centered (500ms window around button press).

## Classification Results

Historical evaluation: 4 subjects (sub02–sub05), 77 MI epochs, 190 blink epochs. Full breakdown in [`data_analysis/ml/results.md`](data_analysis/ml/results.md).

### Blink Detection

| Evaluation | Config | Classifier | Accuracy |
|---|---|---|---|
| Within-subject | all 4 channels | LDA | **88.7%** |
| Cross-subject (LOSO) | all 4 channels | LDA | **86.0%** |

### Motor Imagery (Left vs Right)

| Evaluation | Pipeline | Config | Classifier | Accuracy |
|---|---|---|---|---|
| Within-subject | v1 Band Power | all 4 channels | SVM-rbf | **61.0%** |
| Cross-subject (LOSO) | v3 EEGNet | temporal only | RF | **62.2%** |

Three feature pipelines were compared:
- **v1** — Band power + Hjorth + lateralization asymmetry + CSP
- **v2** — v1 + sliding-window time-frequency (ERD/ERS dynamics)
- **v3** — v2 + EEGNet-inspired fixed spatial/temporal features

Key findings: Blink detection outperformed motor imagery in this offline evaluation. Motor imagery is near chance with 4 subjects — needs more data or paradigm refinement. These scores do not establish live Quest performance.

The newer saved [pipeline evaluation](ml_pipeline/results/training_results.json) includes 11 subject IDs, 54 MI epochs, and 565 blink epochs. Random Forest MI scores are 52.39% within-subject and 45.66% LOSO; LDA blink scores are 89.41% and 82.07%, respectively. These are binary MI/blink offline results, not four-class navigation results.

## Real-Time Feedback System

The real-time GUI (`all_ml_models/realtime_feedback/realtime_feedback.py`) provides live brain state feedback:

- **Blink detection** — 500ms sliding window, LDA classifier, ~1s cooldown
- **Motor imagery** — 3s sliding window, Random Forest with CSP features
- **Band powers** — live delta/theta/mu/alpha/beta/gamma visualization
- **Raw EEG** — 4-channel waveform display (last 5 seconds)

Adjustable confidence thresholds via the GUI. Blink detection has priority over motor imagery each update cycle (5 Hz).

## WebSocket System

The WebSocket connection carries model predictions from Python to Unity so
EEG processing runs on the computer while navigation runs on the Quest.
Raw EEG stays in the Python pipeline; the prediction socket sends class labels,
confidence scores, and pipeline state events.

| Socket | Direction | Purpose |
|---|---|---|
| `8765` — predictions | Python → Unity | Navigation predictions and lifecycle events |
| `8766` — markers | Unity → Python | Trial markers and run/session boundaries during data collection |

[`JsonWSServer`](eeg_to_meta/websocket_server.py) broadcasts one UTF-8 JSON
message per WebSocket frame to connected clients. Unity's
`PredictionWebSocketClient`
connects to `ws://<host>:8765` and passes predictions to `FreeNavController`.
With automatic connection enabled, it retries disconnected connections every
2 seconds. See [Run the Unity Navigation System](#run-the-unity-navigation-system)
above for USB and Wi-Fi setup.

### Prediction Messages

Example left-turn prediction:

```json
{
  "type": "prediction",
  "timestamp": 1711234567.89,
  "predicted_class": "left_motor_imagery",
  "confidence": 0.93,
  "raw_probs": {"left": 0.93, "right": 0.07},
  "stable": true,
  "key_hint": "LeftArrow"
}
```

- **Class and confidence** — `predicted_class` identifies the action; `confidence` is a score from 0 to 1. Legacy left/right labels rotate the player. BCI Navigator personal models can add `mi_forward`; the separate four-direction models use `mi_forward`, `mi_backward`, `mi_rotate_left`, and `mi_rotate_right`.
- **Stability and timing** — navigation predictions must be stable and meet the confidence threshold. The server sends at most one navigation command every 3 seconds and omits `idle` frames. Unity rejects stale timestamps and stops movement when an accepted command expires.
- **Diagnostics** — `raw_probs` contains per-class probabilities. `key_hint` is advisory; navigation uses the class label directly, without injecting keyboard input.
- **Pipeline state** — messages with `type: "state"` report lifecycle transitions such as `personalizing_start`, `personalizing_end`, and `realtime_ready` separately from predictions.

### Test Without Muse EEG

After configuring the Quest connection, run the synthetic prediction server
from the repository root:

```bash
uv run python tools/smoke_predictions.py
# For Wi-Fi, add: --host 0.0.0.0
```

Wait for `Quest connected`, then enter `forward`, `backward`, `left`, or `right`.
Use `low` and `unstable` to check that rejected predictions do not move the
player. This checks transport and Unity movement independently of EEG decoding.
Enter `q` before starting live inference; both servers use port `8765`.

### Keyboard Shortcuts

| Key | Action |
|---|---|
| Space | Start / stop classification |
| Q | Quit |

## Preprocessing Pipeline

Desktop MI preprocessing is applied per-channel:

1. NaN/Inf sanitization
2. Moving artifact removal (rolling z-score, 3-sigma threshold, 0.5s window)
3. Linear detrend
4. Notch filter (60 Hz powerline removal)
5. Bandpass filter (1–40 Hz, 4th-order Butterworth)

The Quest runtime uses separate MI (1–40 Hz) and blink (0.5–10 Hz) bandpasses, with 2-second MI and 0.5-second blink windows; see [`eeg_to_meta/config.py`](eeg_to_meta/config.py).

## Dependencies

Dependencies are declared in `pyproject.toml` and resolved in `uv.lock`. Install them with `uv sync --python 3.11`.

- `brainflow` — EEG acquisition
- `mne` — Signal filtering
- `scikit-learn` — ML classifiers
- `PyQt6`, `pyqtgraph` — Real-time GUI
- `psychopy` — Experiment paradigm
- `numpy`, `scipy`, `pandas` — Numerics
- `torch` — EEGNet features (v3 pipeline only)

## Team

| Name | Role |
|---|---|
| Ashee | BCI Lead |
| Spencer | BCI Lead |
| Alan | BCI Sub-team member |
| Michelle | BCI Sub-team member |
| James | BCI Sub-team member |

Cornell XR Project Team
