# App backend

A local service that runs the app's calibration, training and realtime stages behind a localhost HTTP + WebSocket API. It holds the **single** Muse connection for the whole session, so the headset is never reconnected between steps.

```
macOS app ──HTTP/WS 127.0.0.1:8000──► backend ──BLE──► Muse 2
                                         └──WS :8765──► Quest (Unity FreeNavController)
```

## Run

```bash
uv sync --python 3.11

# No headset: a simulated Muse that reacts to calibration cues
uv run python -m backend --board simulated --no-auth

# Real headset
uv run python -m backend --serial Muse-15C3
```

| Flag | Default | |
|---|---|---|
| `--board` | `muse` | `muse`, `synthetic` (BrainFlow random signal, 250 Hz) or `simulated` |
| `--port` | `8000` | API port; the API binds to `127.0.0.1` only |
| `--data-root` | `data/app` | Where recordings and models are stored |
| `--unity-ws-host` / `--unity-ws-port` | `127.0.0.1` / `8765` | Quest socket. Use `0.0.0.0` for Wi-Fi, or `adb reverse tcp:8765 tcp:8765` for USB |
| `--no-auth` | off | Disables the token. Development only |

**Auth.** The app should launch the backend as a child process and set `BCI_BACKEND_TOKEN=<random>` in its environment. Without that variable, the backend generates a token and prints `BCI_BACKEND_TOKEN=...` to stdout. Send the token as `Authorization: Bearer <token>` on HTTP requests and as `?token=<token>` on the WebSocket. This blocks other local software, and web pages open in a browser, from reading EEG or driving the headset.

**Bluetooth permission.** On macOS, the process that opens BLE needs Bluetooth permission (`NSBluetoothAlwaysUsageDescription`). Check early that the permission applies when the app launches the backend as a child process.

## App flow

```
POST /session           → connect headset (stage: connected)
  [connection quality check — separate work, see below]
POST /calibration/start → backend runs the cue schedule (stage: calibrating)
  … calibration_cue events … calibration_finished
POST /train             → 202, runs in background
  … training_finished { report }   → show results; recalibrate if not ready
POST /realtime/start    → predictions to Quest + app (stage: realtime)
POST /realtime/stop
DELETE /session
```

Only one stage that uses the live stream runs at a time. Requests that don't fit the current stage return **409** with a reason.

## Endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/health` | — | `{ok, board}` (no auth) |
| GET | `/status` | — | Full state: stage, board, calibration progress, sessions recorded, training, latest model + report, realtime |
| GET | `/classes` | — | `{available, default: ["left","right","blink"]}` |
| POST | `/session` | `{user_id, serial?}` | Status. **502** if the headset can't connect |
| DELETE | `/session` | — | Stops everything and releases the headset |
| POST | `/calibration/start` | see below | `{session_id, plan, n_trials, estimated_duration_s}` |
| POST | `/calibration/stop` | — | Saves completed trials, drops the trial in progress |
| POST | `/train` | `{classes?, mi_stride_s?, mi_offset_s?}` | **202**; result comes as the `training_finished` event or from `/status` |
| GET | `/model` | — | `{path, report}` of the latest model; **404** if none |
| POST | `/realtime/start` | `{mi_threshold?, blink_threshold?, unity_ws_host?, unity_ws_port?}` | `{model_path, unity_ws}` |
| POST | `/realtime/stop` | — | |
| POST | `/debug/simulate` | `{intent: "left"\|"right"\|"blink"\|null}` | Only with `--board simulated` |

`user_id` should be the stable OAuth subject ID. It is hashed before use, and only the hash appears on disk.

### Calibration request

```json
{
  "classes": ["left", "right", "blink"],
  "trials_per_class": 15,          // or "duration_s": 300 — not both
  "cue_s": 1.5, "imagine_s": 4.0, "blink_s": 2.0,
  "rest_s": 3.0, "rest_jitter_s": 1.0
}
```

All fields are optional; the defaults are shown. `imagine_s` must be at least 2.5 s: 0.5 s of reaction time plus one 2 s model window. With the defaults, 15 trials per class takes about 6 minutes. Trials come in shuffled rounds that contain every class once. Each trial runs:

```
cue (get ready: LEFT) → start (imagine / blink once) → end → rest
```

Recalibrating adds a new session. Training always uses **all** of the user's sessions, so "record more and retrain" works without extra steps.

### Training report (`training_finished.report`, `GET /model`)

```json
{
  "mi_classes": ["left_motor_imagery", "right_motor_imagery"],
  "mi_trials_recorded": {"left_motor_imagery": 15, "right_motor_imagery": 15},
  "mi_trials_kept":     {"left_motor_imagery": 14, "right_motor_imagery": 15},
  "mi_windows_rejected": {"too_short": 0, "bad_channels": 2, "amplitude": 5, "flat": 0},
  "blink_epochs": 15, "non_blink_epochs": 60,
  "mi_cv_accuracy": 0.63, "mi_cv_per_class": {"left_motor_imagery": 0.6, "right_motor_imagery": 0.66},
  "mi_chance": 0.5, "blink_cv_accuracy": 0.91,
  "checks": [{"name": "mi_accuracy", "passed": true, "detail": "cross-validated 63% (need 60%, chance 50%)"}, …],
  "ready": true
}
```

`ready` means every check passed. The checks are: at least 8 clean trials per class, motor imagery at least 10 points above chance, at least 8 blinks, and blink accuracy of at least 80%. Thresholds are in `pipeline/personalize.py` (`READY_*`). A model is saved even when it's not ready, so the app can still let the user try it. Use `checks[].detail` to tell the user what to redo.

Training fails (`training_finished` with `status: "failed"` and an `error`) when there isn't enough clean data to fit both models, because realtime needs both.

## Events — `WS /events?token=…`

Each message is JSON with `type` and `ts`. The first message is always a full `status` snapshot.

| type | Payload |
|---|---|
| `session_connecting` / `session_opened` / `session_error` / `session_closed` | board info / `message` |
| `calibration_started` | `session_id, plan, n_trials, estimated_duration_s` |
| `calibration_cue` | `trial_index, n_trials, cls` (`left`/`right`/`blink`/`rest`), `phase` (`cue`/`start`/`end`), `duration_s`, `progress` |
| `calibration_finished` | `status` (`completed`/`cancelled`/`failed`), `trials_completed`, `duration_s` |
| `training_started` / `training_finished` | `status`, `report` or `error` |
| `realtime_started` / `realtime_stopped` | `classes, unity_ws, thresholds` |
| `prediction` | `label, confidence, stable, raw_probs, unity_clients` (about 5 per second; blinks are sent immediately) |

The frontend only needs to draw cues: render `calibration_cue` with `phase: "cue"` as "get ready", `phase: "start"` as "imagine now" or "blink now", and `cls: "rest"` as a rest screen.

## On-disk layout

```
data/app/users/<sha256(user_id)[:16]>/
  sessions/session_<ts>/{eeg_data.csv, trial_log.csv, metadata.yaml}   # same format as pipeline.collector
  models/<ts>/{model.pkl, report.json}                                 # latest successful run is used
```

`data/` is gitignored.

## Connection quality check (for whoever builds it)

The headset stream is owned by `SessionManager.source` (`backend/board.py::EEGSource`). Register a sink to read it without opening a second BLE connection:

```python
def sink(chunk):              # runs on the poll thread — keep it fast
    chunk.timestamps          # (n,) unix seconds
    chunk.eeg                 # (4, n) µV, order TP9, AF7, AF8, TP10
manager.source.add_sink(sink) # remove_sink(sink) when done
```

Suggested shape: a `GET /quality` endpoint, plus `quality` events published through `bus.publish("quality", channels={...})` while the user is on the quality screen.

## Tests

```bash
uv run pytest tests/ -q
```
