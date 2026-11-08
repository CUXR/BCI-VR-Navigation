"""EEG preprocessing with ASR and spectral channel quality assessment.

Implements the two-winner methods from Delorme & Martin (2021):
  1. Channel rejection  — spectral thresholding (5-55 Hz, 25 dB)
  2. Continuous cleaning — Artifact Subspace Reconstruction (cutoff 20)

Provides separate preprocessing paths for motor imagery and blink epochs.
"""

import numpy as np
from scipy import signal as sp_signal
from scipy.signal import welch
from scipy.ndimage import uniform_filter

import mne
mne.set_log_level("ERROR")

from config import (
    TARGET_SFREQ, N_CHANNELS, CH_NAMES,
    SPECTRAL_THRESHOLD_DB, SPECTRAL_FREQ_RANGE,
    ASR_CUTOFF_MI, MI_BANDPASS, BLINK_BANDPASS, NOTCH_FREQ,
    CLIP_THRESHOLD_UV, FLAT_THRESHOLD_UV,
)

# ── ASR availability ──────────────────────────────────────────────

_HAS_ASR = False
try:
    import asrpy
    _HAS_ASR = True
except ImportError:
    try:
        from meegkit.asr import ASR as _MeegkitASR
        _HAS_ASR = True
    except ImportError:
        pass


# ═══════════════════════════════════════════════════════════════════
# Channel quality scoring (Delorme paper §II-E, Table 1)
# ═══════════════════════════════════════════════════════════════════

def compute_channel_spectra(data_2d, sfreq, fmin=None, fmax=None):
    """Compute mean log-power spectral density per channel.

    Args:
        data_2d: (n_channels, n_samples) array
        sfreq: sampling frequency
        fmin, fmax: frequency range (default from config)

    Returns:
        spectra: (n_channels,) mean log10(µV²)/Hz in [fmin, fmax]
    """
    fmin = fmin or SPECTRAL_FREQ_RANGE[0]
    fmax = fmax or SPECTRAL_FREQ_RANGE[1]
    nperseg = min(int(sfreq * 2), data_2d.shape[1])
    if nperseg < int(sfreq * 0.25):
        return np.full(data_2d.shape[0], np.nan)

    spectra = np.zeros(data_2d.shape[0])
    for ch in range(data_2d.shape[0]):
        freqs, psd = welch(data_2d[ch], fs=sfreq, nperseg=nperseg)
        mask = (freqs >= fmin) & (freqs <= fmax)
        if np.any(mask) and np.any(psd[mask] > 0):
            spectra[ch] = np.mean(np.log10(psd[mask] + 1e-12))
        else:
            spectra[ch] = np.nan
    return spectra


def score_channel_quality(data_2d, sfreq, threshold_db=None):
    """Score each channel: True = good, False = bad.

    Uses spectral thresholding in 5-55 Hz as validated by Delorme paper.
    Also checks for clipping and flat-line.

    Args:
        data_2d: (n_channels, n_samples)
        sfreq: sampling frequency
        threshold_db: log10(µV²)/Hz threshold (default from config)

    Returns:
        good_mask: (n_channels,) boolean
        quality_info: dict with per-channel details
    """
    threshold_db = threshold_db or SPECTRAL_THRESHOLD_DB
    n_ch, n_samples = data_2d.shape

    spectra = compute_channel_spectra(data_2d, sfreq)

    good_mask = np.ones(n_ch, dtype=bool)
    info = {}

    for ch in range(n_ch):
        ch_data = data_2d[ch]
        std = np.std(ch_data)
        clip_frac = np.mean(np.abs(ch_data) >= CLIP_THRESHOLD_UV)
        is_flat = std < FLAT_THRESHOLD_UV

        spectral_bad = (not np.isnan(spectra[ch])) and (spectra[ch] > threshold_db)
        clip_bad = clip_frac > 0.01  # >1% clipped
        flat_bad = is_flat

        good_mask[ch] = not (spectral_bad or clip_bad or flat_bad)

        info[CH_NAMES[ch]] = {
            "spectral_power_db": float(spectra[ch]) if not np.isnan(spectra[ch]) else None,
            "std_uv": float(std),
            "clip_fraction": float(clip_frac),
            "is_flat": bool(is_flat),
            "is_good": bool(good_mask[ch]),
        }

    return good_mask, info


def score_channel_quality_epoch(epoch_2d, sfreq):
    """Quick per-epoch channel quality check.

    Returns boolean mask (n_channels,) — True = usable in this epoch.
    """
    n_ch = epoch_2d.shape[0]
    good = np.ones(n_ch, dtype=bool)
    for ch in range(n_ch):
        data = epoch_2d[ch]
        ptp = np.ptp(data)
        std = np.std(data)
        clip_frac = np.mean(np.abs(data) >= CLIP_THRESHOLD_UV)

        if std < FLAT_THRESHOLD_UV or clip_frac > 0.05 or ptp > 800:
            good[ch] = False
    return good


# ═══════════════════════════════════════════════════════════════════
# Core filtering
# ═══════════════════════════════════════════════════════════════════

def _bandpass_filter(data_1d, sfreq, low, high, order=4):
    """Butterworth bandpass on a 1-D signal."""
    nyq = sfreq / 2.0
    lo = max(low / nyq, 0.001)
    hi = min(high / nyq, 0.999)
    b, a = sp_signal.butter(order, [lo, hi], btype="band")
    return sp_signal.filtfilt(b, a, data_1d)


def _notch_filter(data_1d, sfreq, freq=None, quality=30.0):
    """IIR notch filter for powerline noise."""
    freq = freq or NOTCH_FREQ
    b, a = sp_signal.iirnotch(freq, quality, sfreq)
    return sp_signal.filtfilt(b, a, data_1d)


def interpolate_nans(data_1d):
    """Linear interpolation of NaN and empty values."""
    arr = data_1d.copy().astype(np.float64)
    nans = np.isnan(arr) | np.isinf(arr)
    if not np.any(nans):
        return arr
    valid = np.where(~nans)[0]
    if len(valid) < 2:
        arr[nans] = 0.0
        return arr
    arr[nans] = np.interp(np.where(nans)[0], valid, arr[valid])
    return arr


def interpolate_clipped(data_1d, threshold=None):
    """Replace clipped samples (at ADC rails) with linear interpolation."""
    threshold = threshold or CLIP_THRESHOLD_UV
    arr = data_1d.copy()
    clipped = np.abs(arr) >= threshold
    if not np.any(clipped):
        return arr
    valid = np.where(~clipped)[0]
    if len(valid) < 2:
        return arr
    arr[clipped] = np.interp(np.where(clipped)[0], valid, arr[valid])
    return arr


def moving_artifact_removal(data_1d, sfreq, threshold_std=3.0):
    """Statistical z-score artifact removal with interpolation."""
    window_size = int(sfreq * 0.5)
    if len(data_1d) < window_size:
        return data_1d

    rolling_mean = uniform_filter(data_1d, size=window_size, mode="constant")
    rolling_var = uniform_filter(data_1d ** 2, size=window_size, mode="constant") - rolling_mean ** 2
    rolling_std = np.sqrt(np.maximum(rolling_var, 0))

    z_scores = np.abs(data_1d - rolling_mean) / (rolling_std + 1e-8)
    artifact_mask = z_scores > threshold_std

    if np.any(artifact_mask):
        valid = np.where(~artifact_mask)[0]
        if len(valid) > 1:
            return np.interp(np.arange(len(data_1d)), valid, data_1d[valid])
    return data_1d


# ═══════════════════════════════════════════════════════════════════
# ASR (Artifact Subspace Reconstruction)
# ═══════════════════════════════════════════════════════════════════

def apply_asr(data_2d, sfreq, cutoff=None):
    """Apply Artifact Subspace Reconstruction to multi-channel data.

    Args:
        data_2d: (n_channels, n_samples) raw EEG
        sfreq: sampling frequency
        cutoff: ASR threshold (default from config)

    Returns:
        cleaned: (n_channels, n_samples)
    """
    cutoff = cutoff or ASR_CUTOFF_MI

    if not _HAS_ASR:
        return data_2d

    try:
        if "asrpy" in dir():
            import asrpy
        else:
            import asrpy
        info = mne.create_info(
            ch_names=[CH_NAMES[i] for i in range(data_2d.shape[0])],
            sfreq=sfreq,
            ch_types="eeg",
        )
        raw = mne.io.RawArray(data_2d, info, verbose=False)
        asr = asrpy.ASR(sfreq=sfreq, cutoff=cutoff)
        asr.fit(raw)
        raw_clean = asr.transform(raw)
        return raw_clean.get_data()
    except Exception:
        pass

    try:
        from meegkit.asr import ASR as _MeegkitASR
        asr = _MeegkitASR(sfreq=sfreq, cutoff=cutoff)
        # meegkit expects (n_samples, n_channels)
        data_t = data_2d.T.copy()
        _, sample_mask = asr.fit(data_t)
        cleaned_t = asr.transform(data_t)
        return cleaned_t.T
    except Exception:
        return data_2d


# ═══════════════════════════════════════════════════════════════════
# Full preprocessing pipelines
# ═══════════════════════════════════════════════════════════════════

def preprocess_mi_epoch(epoch_2d, sfreq, *, use_asr=True):
    """Full preprocessing for a motor imagery epoch.

    Pipeline:
      1. NaN/clip interpolation
      2. Detrend
      3. Notch filter (60 Hz)
      4. Bandpass 1-40 Hz
      5. Moving artifact removal
      6. ASR (if available)

    Args:
        epoch_2d: (n_channels, n_samples)
        sfreq: sampling frequency

    Returns:
        cleaned: (n_channels, n_samples)
    """
    n_ch, n_samp = epoch_2d.shape
    out = np.zeros_like(epoch_2d, dtype=np.float64)

    for ch in range(n_ch):
        sig = epoch_2d[ch].astype(np.float64)
        sig = interpolate_nans(sig)
        sig = interpolate_clipped(sig)
        sig = sp_signal.detrend(sig)
        if n_samp > int(sfreq * 0.5):
            sig = _notch_filter(sig, sfreq)
            sig = _bandpass_filter(sig, sfreq, MI_BANDPASS[0], MI_BANDPASS[1])
        sig = moving_artifact_removal(sig, sfreq, threshold_std=3.0)
        out[ch] = sig

    if n_ch >= 2 and n_samp >= int(sfreq * 1.0) and _HAS_ASR and use_asr:
        try:
            out = apply_asr(out, sfreq, cutoff=ASR_CUTOFF_MI)
        except Exception:
            pass

    return out


def preprocess_blink_epoch(epoch_2d, sfreq):
    """Full preprocessing for a blink detection epoch.

    Pipeline:
      1. NaN interpolation (no clip interpolation — blinks are large)
      2. Detrend
      3. Bandpass 0.5-10 Hz
      NO ASR — blinks would be removed

    Args:
        epoch_2d: (n_channels, n_samples)
        sfreq: sampling frequency

    Returns:
        cleaned: (n_channels, n_samples)
    """
    n_ch, n_samp = epoch_2d.shape
    out = np.zeros_like(epoch_2d, dtype=np.float64)

    for ch in range(n_ch):
        sig = epoch_2d[ch].astype(np.float64)
        sig = interpolate_nans(sig)
        sig = sp_signal.detrend(sig)
        if n_samp > int(sfreq * 0.25):
            try:
                sig = _bandpass_filter(sig, sfreq, BLINK_BANDPASS[0], BLINK_BANDPASS[1])
            except Exception:
                pass
        out[ch] = sig

    return out


def preprocess_continuous(data_2d, sfreq, mode="mi"):
    """Preprocess continuous multi-channel data.

    Args:
        data_2d: (n_channels, n_samples)
        sfreq: sampling frequency
        mode: "mi" for motor imagery, "blink" for blink detection

    Returns:
        cleaned: (n_channels, n_samples)
        channel_quality: dict from score_channel_quality
    """
    _, quality_info = score_channel_quality(data_2d, sfreq)

    if mode == "mi":
        cleaned = preprocess_mi_epoch(data_2d, sfreq)
    else:
        cleaned = preprocess_blink_epoch(data_2d, sfreq)

    return cleaned, quality_info
