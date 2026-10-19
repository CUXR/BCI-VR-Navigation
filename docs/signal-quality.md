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

## Initial thresholds

These defaults are provisional engineering thresholds. Confirm them using clean
and deliberately degraded live recordings before treating the labels as a
validated contact detector. `QualityConfig` exposes all thresholds to Python
callers; thresholds are included in every report for reproducibility.

Checks use the latest two seconds of **raw** EEG, refreshed every 0.5 seconds:

| Check | Default | Result |
|---|---|---|
| ADC saturation | More than 1% of samples have absolute amplitude ≥999 µV | Immediately poor |
| Flat line | Standard deviation <0.5 µV | Immediately poor |
| Large amplitude | Mean-centered RMS >100 µV / >200 µV | Fair / poor when persistent |
| Line noise | 50/60 Hz ±2 Hz power divided by 1–90 Hz power >25% / >50% | Fair / poor when persistent |
| Invalid values | NaN or infinity | Unknown for the affected channel |
| Decoder range | Absolute raw amplitude >1001 µV | Unknown; reject before squaring |
| Estimated missing samples | More than 1% / 5% of expected samples | Stream fair / unknown |
| No new samples | More than 0.5 seconds since the last received batch | Stream and channels unknown |

The RMS is computed after subtracting the mean, so an ordinary DC offset does
not inflate amplitude. Line-noise ratios use linear Welch PSD; this module
does not reuse the older offline `log10(power)`/decibel threshold comparison.
Saturation is checked before detrending, filtering, or interpolation. Raw CSV
values are preserved, including invalid values and clipped samples.

Amplitude and line-noise failures need three poor candidates in the last four
updates to become poor. A warning appears sooner. Recovery requires three
seconds of clean assessments after the rolling window clears. This prevents
rapid status changes. A brief frontal blink does not alone establish poor
contact; repeated blinks or movement can still make an interval unusable.
The evaluator does not classify the artifact's cause or suppress blink events.

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
