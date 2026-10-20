# Muse 2 signal quality

The backend in `src/signal_quality/` evaluates raw TP9 (left ear), AF7 (left
forehead), AF8 (right forehead), and TP10 (right ear) samples independently.
It reports `good`, `fair`, `poor`, or `unknown`, with measurements and reason
codes. It estimates signal usability; it does not measure contact impedance.
The shared FpZ reference can affect several channels, so a noisy channel does
not conclusively identify a faulty physical contact.

## Check headset fit

Run from the repository root with the existing Python dependencies installed:

```bash
uv run python -m src.signal_quality --serial 15C3 --output /tmp/muse-quality.jsonl
```

Wear the Muse, close competing Muse applications, and make the headset available
over Bluetooth. The command connects through BrainFlow's native Muse 2 driver;
it needs no classifier, PsychoPy window, or Quest. Stop with Ctrl-C. Omit
`--serial` to let BrainFlow discover a headband. Use `--mains 50` in a 50 Hz
environment (default: 60 Hz).

Without hardware:

```bash
uv run python -m src.signal_quality --simulate --seconds 10
uv run python -m src.signal_quality \
  --replay data/Muse_Data/Phase2/sub03/eeg_data_20260308_144200.csv \
  --output /tmp/replayed-quality.jsonl
```

Replay accepts `timestamp` with either `EEG_0..EEG_3` or named channels, skips
legacy marker-only rows, and processes the CSV in batches. `--sfreq` sets the
replay sampling rate (default: 256 Hz). Live checking reads the sampling rate
from BrainFlow, including the synthetic board's different rate.

## Signal quality check algorithm

`src/signal_quality/quality.py` evaluates signal usability through a multi-stage
filtering, feature extraction, and hysteresis pipeline:

1. **Per-sample buffering and IIR filtering (`process_eeg`)**:
   - Maintains 3-second circular buffers (`768 x 4` at 256 Hz) for raw EEG, NaN
     flags, and filtered EEG, triggering `process_idle` every
     `step_samples = round(srate / 20) * 2` (`26` samples at 256 Hz, ~9.85 Hz).
   - Filters each channel with a stateful Direct Form II Transposed (`filter_data_dfiit`)
     cascade: 6th-order Butterworth lowpass at 36 Hz (`filter_36_lp_order6`)
     followed by a 2nd-order Butterworth highpass at 2 Hz (`filter_2_hp_order2_fdesign`),
     plus a parallel 2–30 Hz cascade (`filter_30_lp_order6`).
2. **Feature extraction (`calc_var_feats`)**:
   - Over the latest `0.5 s` (`128` samples at 256 Hz) of the 2–36 Hz signal,
     computes per-channel population standard deviation `std_p` (`ddof=0`),
     log sample variance `log10(var_s)` (`ddof=1`), and Pearson kurtosis `kurt`.
3. **Per-sensor decision trees (`dt_ear`, `dt_front`)**:
   - **Ear (`TP9`, `TP10` — `dt_ear`)**:
     - `std_p >= 123.0 µV` → `4` (`poor`)
     - `46.0 <= std_p < 123.0 µV` → `2` (`fair`)
     - `5.0421 <= std_p < 46.0 µV` → `1` (`good`)
     - `std_p < 5.0421 µV` and `log10_var >= 0.8221` ($\text{var}_s \ge 6.64\ \mu\text{V}^2$) → `1` (`good`)
     - `std_p < 5.0421 µV` and `log10_var < 0.8221` → `4` (`poor` / `flat_line`)
   - **Forehead (`AF7`, `AF8` — `dt_front`)**:
     - `std_p >= 118.9837 µV` → `4` (`poor`)
     - `46.8163 <= std_p < 118.9837 µV` → `2` (`fair`)
     - `15.9952 <= std_p < 46.8163 µV` with previous sub-window variance
       `prev_sub_win_var >= 265.8586 µV²` and `kurt >= 2.8146` → `4` (`poor` / `kurtosis_artifact`)
     - Otherwise → `1` (`good`)
4. **State machine, smoothing, and 3-epoch hold**:
   - Tracks 15-epoch and 60-epoch raw horseshoe history (`raw_hist_15`, `raw_hist_60`),
     per-channel `good_streak`/`bad_streak`, and the 5-state `sensor_state` machine:
     4 consecutive good epochs (`good_streak > 3`) latch `sensor_state = 1.0` (`good`),
     holding through brief excursions until `prop_bad_15 > 0.6` or `prop_good_60 < 0.1`;
     `sensor_state = 4.0` caps transient bad epochs to `2` (`fair`) until `prop_bad_15 > 0.7`.
   - Applies the 3-epoch post-transition hold (`hold_counter`), the 1-second ear
     quietness gate (`good_data_check`: 1 s variance `< 275.0 µV²`, `sub_win_var` over
     the latest `43` samples, capping ear channels to `fair` if `no_good_ear_counter >= 15`),
     `is_nan_alert` (`>= 6%` NaNs in 3 s buffer), `is_noise_alert` (`>= 50%` noisy
     epochs in last 100 epochs), and `drlref_classification` (`|DRL - REF| <= 200000`
     and `var(|DRL - REF|) <= 87035000` over 2 s at 32 Hz for Muse 2).

Loss estimation uses Muse's 16-bit packet counter when provided, allowing
12 samples per packet and normal counter wraparound. Otherwise it uses
timestamp gaps and identifies that estimate in the report. Timestamp jitter
and BrainFlow's timestamp smoothing limit that estimate. The available Muse
packet metadata is global; it cannot establish per-electrode packet loss or
prove that all four channel notifications were synchronized by the driver.

Transport failure makes channel quality unknown rather than blaming contacts.
`reader_error`, `recording_error`, `board_backlog_samples`, recording backlog,
and rejected recording rows are reported separately. A delayed quality worker
also makes the live snapshot unknown, even while acquisition continues.

## Integration and output

- The PsychoPy recorder starts its **single** BrainFlow reader when streaming
  begins, before recording. Electrode status appears in the terminal and on the
  pre-experiment fitting screen. Recording uses incremental CSV writes and adds
  `signal_quality_<timestamp>.jsonl`. The existing EEG and trial-log column
  schemas remain intact.
- `python -m pipeline collect ...` writes incremental `eeg_data.csv` and a
  `signal_quality.jsonl` sidecar in the session directory. Metadata records
  whether recording completed. Recording or reader failure ends the session
  with a nonzero exit code.
- `python -m pipeline realtime ...` / `eeg_to_meta/main.py` use the existing
  reader to feed both inference and quality monitoring. Quality is logged and
  sent as separate `type: "signal_quality"` WebSocket frames at approximately
  2 Hz, including during idle periods. The current Unity prediction client
  ignores these advisory frames; a rebuilt client is needed when using this
  backend with an older installed APK. Navigation prediction messages retain
  their existing format and cooldown.

Example report fields (additional thresholds and health fields are also sent):

```json
{
  "type": "signal_quality",
  "version": 1,
  "timestamp": 1792330000.0,
  "source": "muse",
  "window_start": 1792329998.0,
  "window_end": 1792330000.0,
  "stream_status": "ok",
  "stream_reasons": [],
  "loss_estimate_method": "packet_counter",
  "missing_fraction": 0.0,
  "channels": {
    "TP9": {"status": "poor", "reasons": ["saturation"], "clipped_fraction": 0.12},
    "AF7": {"status": "good", "reasons": []},
    "AF8": {"status": "good", "reasons": []},
    "TP10": {"status": "good", "reasons": []}
  }
}
```

Acquisition only copies a bounded two-second quality window. Spectral analysis
and quality-sidecar writes run on a separate worker. The CSV writer uses a
64-batch queue; acquisition does not wait for disk. Queue exhaustion or a disk
error is explicit and marks recording incomplete. Already queued rows are
drained when possible. A disk failure can leave a partial final batch, so the
written-row count is a count of successfully flushed batches. This does not
guarantee lossless recording under arbitrary stalls or a process crash.

Quality is advisory in this version: it does not remove channels, modify model
input, automatically tune thresholds, or block trials/predictions. Tune
`QualityConfig` from a confirmed clean baseline and labeled poor-contact
intervals, then validate before enabling an automatic recording/inference gate.
Automatic baseline adaptation would risk accepting a poorly fitted headset as
normal.

## Validation

```bash
uv run python -m unittest discover -s tests -v
uv run python -m unittest discover -s eeg_to_meta -p 'test_*.py' -v
```

Tests cover individual-channel clipping, flat lines, invalid/huge values,
50/60 Hz interference, brief blinks, persistence and recovery, packet wrap and
loss, timestamp errors, bounded buffers, slow consumers, disk failure, and
recording shutdown. Integration tests exercise the mock collector and the
PsychoPy backend with BrainFlow's synthetic board, without opening a GUI.

Local replay on 2026-10-18 processed 16 recordings, 1,043,868 EEG rows, and
8,163 analysis updates. This checks replay compatibility and fault handling;
the recordings have no verified contact labels, so this does not measure
detection accuracy. Live Muse/Bluetooth, GUI appearance, and rebuilt Unity
behavior still need validation.

For live validation, record a clean baseline, loosen each of the four contacts
separately, refit, blink, move, and disconnect/reconnect. Compare reported
channel/reason and recovery against those marked intervals. Run an extended
recording to inspect backlog, sample continuity, and bounded memory.

References: [BrainFlow Muse decoder](https://github.com/brainflow-dev/brainflow/blob/5.23.0/src/board_controller/muse/muse.cpp),
[BrainFlow buffer API](https://brainflow.readthedocs.io/en/stable/UserAPI.html),
[Muse electrode documentation](https://musehealth.ai/pages/faq).
