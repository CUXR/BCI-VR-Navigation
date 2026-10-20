# Signal-quality implementation and validation

The pipeline processes four-channel EEG through stateful filtering, spectral
and time-domain features, electrode decision trees, status smoothing, channel
coupling, and noise gating. It retains separate research safeguards and reports
reference-contact availability explicitly.

## Implemented behavior

- One-second raw PSD and seven notch-filtered features per electrode are
  computed at each internal analysis epoch.
- The default quietness gate requires low variance, horseshoe status below two,
  and a measured connected DRL/REF pair.
- The optional learned gate accepts explicit model parameters and a probability
  remapping callback. It combines three epoch probabilities before gating.
  Missing parameters are reported as unavailable when this mode is requested.
- Forehead statuses use their minimum code; ear statuses are coupled only when
  both codes are below four. Independent electrode results remain available.
- Clipping, flatline, decoder-range, and transport checks remain separate from
  the decision-tree history. EEG readiness requires three uninterrupted seconds
  of good research status.

## Data availability and limitations

BrainFlow 5.19.0 exposes Muse 2 EEG, motion, and PPG streams but no DRL/REF pair.
Current readers therefore report `reference_status: "unavailable"` and
`is_drl_connected: null`. EEG-only research operation remains available, and
its unverified reference state is shown on the calibration screen. An adapter
with actual DRL/REF samples can use `feed_drl()` or `feed(..., drl_ref=...)`.
The configured thresholds require compatible input units. Warmup, invalid
samples, and stale input are handled explicitly.

No trained model parameters or probability-remapping function are bundled.
Default operation uses the quietness gate. Filter coefficients use SciPy
Butterworth designs, except for fixed 220/256 Hz highpass coefficients; filter
state starts at zero. PSD uses a symmetric Hamming window, skips invalid
samples, and takes the latest spectrum. These choices have not established
clinical accuracy or equivalence to any external system.

Motion scaling and automatic blink detection are not calibrated. Batched input
is not guaranteed to preserve sample-level ordering between separate streams,
and pending EEG analysis is bounded at three seconds. The `--mains` setting is
metadata; the feature notch covers 45–65 Hz. There is no default raw-spectrum
mains-noise status override.

## Validation

On 2026-10-20, 48 quality/integration tests and seven realtime tests passed.
Checks cover feature extraction, channel ordering, coupling, noise response,
filter-state continuity, reference warmup/expiry/recovery, recording failures,
sidecar output, and readiness timing. Classifier tests use synthetic fixtures
and do not establish trained-model accuracy.

The macOS app build and recorder lifecycle regression passed. A four-second
synthetic CLI run completed successfully. Live Bluetooth contact accuracy,
GUI appearance, and rebuilt Unity behavior remain unverified.

```bash
uv run python -m unittest discover -s tests -v
uv run python -m unittest discover -s eeg_to_meta -p 'test_*.py' -v
```

See `macos_app/README.md` for build and lifecycle-check commands.
