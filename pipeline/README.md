# Final Realtime Pipeline

This package orchestrates the end-to-end BCI flow:

1. `collect` — record EEG + Unity markers per participant under `data/subNN/`
2. `train` — train a base 4-class MI + blink bundle
3. `realtime` — personalise for one participant, then stream predictions to Unity

## Commands

From repo root:

- `python -m pipeline collect --participant 5 --name "Asheeb" --runs 3`
- `python -m pipeline train --variant 4class`
- `python -m pipeline personalize --participant 5`
- `python -m pipeline realtime --participant 5 --mi-threshold 0.58 --blink-threshold 0.62`
- `uv run python -m pipeline realtime --model ml_pipeline/models/realtime_models.pkl --skip-personalize --serial <your_Muse_serial>` (existing model bundle, no participant)

## WebSocket Protocols

### 1) Unity -> Python marker socket (`ws://127.0.0.1:8766`)

Handled by `pipeline.marker_server.MarkerServer`.

#### Marker frame

```json
{
  "type": "marker",
  "phase": "cue | start | end",
  "direction": "forward | backward | left_rotate | right_rotate",
  "trial_index": 17,
  "run_index": 0,
  "ts_unity": 12.345
}
```

Blink trial marker:

```json
{
  "type": "marker",
  "phase": "cue | start | end",
  "trial_type": "blink_intentional",
  "trial_index": 18,
  "run_index": 0,
  "ts_unity": 14.120
}
```

#### Control frame

```json
{
  "type": "control",
  "event": "run_start | run_end | session_end",
  "run_index": 0,
  "ts_unity": 12.000
}
```

### 2) Python -> Unity prediction/state socket (`ws://127.0.0.1:8765`)

Served by `eeg_to_meta.websocket_server.JsonWSServer`.

#### Prediction frame

```json
{
  "type": "prediction",
  "timestamp": 1711234567.89,
  "predicted_class": "mi_forward",
  "confidence": 0.73,
  "raw_probs": {
    "mi_forward": 0.73,
    "mi_backward": 0.11,
    "mi_rotate_left": 0.08,
    "mi_rotate_right": 0.08
  },
  "stable": true,
  "key_hint": "W"
}
```

`predicted_class` is one of:

- `mi_forward`
- `mi_backward`
- `mi_rotate_left`
- `mi_rotate_right`
- `intentional_blink`

`idle` is not broadcast (silence means idle).

#### State frame

```json
{
  "type": "state",
  "state": "personalizing_start | personalizing_end | realtime_ready",
  "participant": "sub05",
  "message": "Personalising the ML model",
  "timestamp": 1711234567.89
}
```

## Unity Wiring

The following scripts are in the archived Unity source at
`unity/environment_1/Assets/scripts/`. See the [standalone build guide](../releases/environment_1/README.md)
for running the packaged environment or restoring source:

- `MarkerWebSocketClient.cs` (collector marker uplink)
- `PredictionWebSocketClient.cs` (prediction + state downlink)
- `FreeNavController.cs` (movement driven by 4-class predictions)
- `PersonalizationOverlayController.cs` (UI overlay for state messages)

Scene/prefab assets:

- `Assets/Scenes/FreeNavScene.unity`
- `Assets/FreeNavRig.prefab`

### AutoMove scene (data collection)

1. Add `MarkerWebSocketClient` to a persistent object (or the same object as `AutoMoveDataCollectionController`).
2. Drag that component into `AutoMoveDataCollectionController.markerWebSocketClient`.
3. Set `sequenceRuns` on `AutoMoveDataCollectionController` to match CLI `--runs`.
4. Ensure Python collector is running before pressing Play.

### FreeNav scene (realtime)

1. Add `PredictionWebSocketClient` to the rig root.
2. Add `FreeNavController` and assign:
   - `predictionClient`: the component from step 1
   - `rigRoot`: XR Origin root transform
   - `characterController`: optional (if omitted, transform movement is used)
3. Add a Canvas overlay + TMP text and attach `PersonalizationOverlayController`:
   - assign `predictionClient`
   - assign `overlayCanvasGroup`
   - assign `statusText`

## Dependencies

Python:

- `pip install -r eeg_to_meta/requirements.txt`
- `pip install -r ml_pipeline/requirements.txt`

Unity:

- `com.endel.nativewebsocket` is required and added in `Packages/manifest.json`.

## Mock End-to-End Smoke Plan

Use this for local validation without a Muse:

1. Start collector in mock mode:
   - `python -m pipeline collect --participant 99 --name "Smoke" --runs 1 --mock`
2. In Unity, run the AutoMove scene with `MarkerWebSocketClient` wired to
   `AutoMoveDataCollectionController`.
3. Train base model on collected sessions:
   - `python -m pipeline train --variant 4class`
4. Personalise:
   - `python -m pipeline personalize --participant 99`
5. Launch realtime in mock mode:
   - `python -m pipeline realtime --participant 99 --mock --no-ws`

Expected outcomes:

- `data/sub99/session_*/` contains `eeg_data.csv`, `trial_log.csv`, `metadata.yaml`
- `ml_pipeline/models/realtime_models_4class.pkl` exists
- `ml_pipeline/models/participants/sub99.pkl` exists
- realtime loop logs stable `predicted_class` outputs and confidence values

