"""Muse EEG signal quality check (SQC) and stream transport monitoring.

Implements the multi-stage signal quality pipeline:
- Buffer & IIR filter bank: 3-second circular buffers and Direct Form II Transposed
  2-36 Hz and 2-30 Hz Butterworth filter cascades.
- Feature extraction: 0.5 s variance/kurtosis features (`calc_var_feats`), Hamming
  power spectral density (`calc_psd`, `do_psd`), and 1 s time-domain features (`calc_isgood_feats`).
- Per-sensor decision trees: ear (`dt_ear` for TP9/TP10) and forehead (`dt_front` for AF7/AF8)
  classifiers, hysteresis state machine, and 3-epoch transition hold.
- Noise & alert gates: 1 s quietness gate (`good_data_check`), logistic epoch classifier
  (`is_epoch_good`), `is_noise_alert`, and `is_nan_alert`.
- Reference contact check: DRL/REF differential amplitude and variance classifier (`drlref_classification`).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import json
import logging
from pathlib import Path
import threading
import time

import numpy as np
from scipy.signal import butter, lfilter

CHANNELS = ("TP9", "AF7", "AF8", "TP10")
EAR = ("TP9", "TP10")
FRONTAL = ("AF7", "AF8")
HORSESHOE_TO_STATUS = {1.0: "good", 2.0: "fair", 4.0: "poor"}
DOT_WEIGHTS_5 = np.array([0.05, 0.10, 0.15, 0.20, 0.50], dtype=np.float64)

log = logging.getLogger(__name__)


# ── Buffer & IIR Filter Bank: Direct Form II Transposed ────────────────────


def filter_2_hp_order2_fdesign(srate: float = 256.0) -> tuple[np.ndarray, np.ndarray]:
    """2nd-order Butterworth highpass filter at 2 Hz."""
    if srate == 220.0:
        b = np.array([0.960388005421737, -1.920776010843474, 0.960388005421737], dtype=np.float64)
        a = np.array([1.0, -1.919205941625069, 0.922346080061879], dtype=np.float64)
        return b, a
    if srate == 256.0:
        b = np.array([0.96588528974407, -1.93177057948814, 0.96588528974407], dtype=np.float64)
        a = np.array([1.0, -1.930606427219668, 0.932934731756612], dtype=np.float64)
        return b, a
    b, a = butter(2, 2.0 / (srate / 2.0), btype="high")
    return np.asarray(b, dtype=np.float64), np.asarray(a, dtype=np.float64)


def filter_36_lp_order6(srate: float = 256.0) -> tuple[np.ndarray, np.ndarray]:
    """6th-order Butterworth lowpass filter at 36 Hz."""
    b, a = butter(6, 36.0 / (srate / 2.0), btype="low")
    return np.asarray(b, dtype=np.float64), np.asarray(a, dtype=np.float64)


def filter_30_lp_order6(srate: float = 256.0) -> tuple[np.ndarray, np.ndarray]:
    """6th-order Butterworth lowpass filter at 30 Hz."""
    b, a = butter(6, 30.0 / (srate / 2.0), btype="low")
    return np.asarray(b, dtype=np.float64), np.asarray(a, dtype=np.float64)


def filter_45_65_notch_order6(srate: float = 220.0) -> tuple[np.ndarray, np.ndarray]:
    """Bandstop 45-65 Hz Butterworth filter."""
    b, a = butter(3, [45.0 / (srate / 2.0), 65.0 / (srate / 2.0)], btype="bandstop")
    return np.asarray(b, dtype=np.float64), np.asarray(a, dtype=np.float64)


def filter_data_dfiit(
    z: np.ndarray,
    x: np.ndarray,
    b: np.ndarray,
    a: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Direct Form II Transposed IIR filter.

    Accepts 1D (n_samples,) with z of shape (order,) or 2D (n_channels, n_samples)
    with z of shape (n_channels, order). Returns (filtered_output, updated_z).
    """
    x_arr = np.asarray(x, dtype=np.float64)
    z_arr = np.asarray(z, dtype=np.float64)
    if x_arr.size == 0:
        return x_arr.copy(), z_arr.copy()
    y, z_out = lfilter(b, a, x_arr, axis=-1, zi=z_arr)
    return y, z_out


class CircBuffer2D:
    """Fixed-capacity 2D circular buffer with 1-based write cursor."""

    def __init__(self, length: int, n_channels: int = 4, init_value: float = 0.0):
        self.length = int(length)
        self.n_channels = int(n_channels)
        self.init_value = float(init_value)
        self.data = np.full((self.length, self.n_channels), self.init_value, dtype=np.float64)
        # 1-based write index (1..length)
        self.write_idx = 1

    def reset(self) -> None:
        self.data.fill(self.init_value)
        self.write_idx = 1

    def update_row(self, row: np.ndarray) -> None:
        self.data[self.write_idx - 1] = row
        self.write_idx += 1
        if self.write_idx > self.length:
            self.write_idx = 1

    def update_chunk(self, chunk_ch_first: np.ndarray) -> None:
        """Append (n_channels, n_samples) into the circular buffer."""
        samples = np.asarray(chunk_ch_first, dtype=np.float64).T  # (n_samples, n_channels)
        n = samples.shape[0]
        if n == 0:
            return
        if n >= self.length:
            self.data[:] = samples[-self.length:]
            self.write_idx = 1
            return
        pos = self.write_idx - 1
        first = min(n, self.length - pos)
        self.data[pos : pos + first] = samples[:first]
        rem = n - first
        if rem > 0:
            self.data[:rem] = samples[first:]
        self.write_idx = ((pos + n) % self.length) + 1

    def latest(self, window_len: int) -> np.ndarray:
        """Extract latest `window_len` rows in chronological order."""
        w = min(int(window_len), self.length)
        # write_idx points to the next slot to overwrite (oldest sample);
        # write_idx - 1 is the most recently written row.
        end = self.write_idx - 1
        if end >= w:
            return self.data[end - w : end].copy()
        return np.vstack((self.data[self.length - (w - end) :], self.data[:end]))


class CircBuffer1D:
    """Fixed-capacity 1D circular buffer with 1-based write cursor."""

    def __init__(self, length: int, init_value: float = 0.0):
        self.length = int(length)
        self.init_value = float(init_value)
        self.data = np.full(self.length, self.init_value, dtype=np.float64)
        self.write_idx = 1

    def reset(self) -> None:
        self.data.fill(self.init_value)
        self.write_idx = 1

    def update(self, value: float) -> None:
        self.data[self.write_idx - 1] = float(value)
        self.write_idx += 1
        if self.write_idx > self.length:
            self.write_idx = 1

    def latest(self, window_len: int) -> np.ndarray:
        w = min(int(window_len), self.length)
        end = self.write_idx - 1
        if end >= w:
            return self.data[end - w : end].copy()
        return np.concatenate((self.data[self.length - (w - end) :], self.data[:end]))


# ── Feature Extraction ─────────────────────────────────────────────────────


def calc_var_feats(eeg_window: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute (std_p, log10_var, kurtosis) on a (N, 4) or (4, N) window.

    Per channel c in 0..3:
    - `std_p[c]`: population standard deviation (ddof=0) of the 0.5 s (128-sample) 2-36 Hz window
    - `log10_var[c]`: base-10 log of sample variance (ddof=1)
    - `kurt[c]`: Pearson kurtosis (normal = 3.0), E[(x - mu)^4] / (E[(x - mu)^2])^2
    """
    arr = np.asarray(eeg_window, dtype=np.float64)
    if arr.ndim != 2:
        raise ValueError("Expected 2D array for calc_var_feats")
    if arr.shape[0] == 4 and arr.shape[1] != 4:
        arr = arr.T  # convert (4, N) -> (N, 4)
    n = arr.shape[0]
    std_p = np.zeros(4, dtype=np.float64)
    log10_var = np.full(4, -np.inf, dtype=np.float64)
    kurt = np.full(4, np.nan, dtype=np.float64)
    if n == 0:
        return std_p, log10_var, kurt

    for c in range(4):
        col = arr[:, c]
        mean_c = float(np.mean(col))
        d = col - mean_c
        d2 = d * d
        var_p = float(np.mean(d2))
        std_p[c] = float(np.sqrt(var_p)) if var_p > 0.0 else 0.0
        var_s = float(np.sum(d2) / (n - 1)) if n > 1 else var_p
        log10_var[c] = float(np.log10(var_s)) if var_s > 0.0 else -np.inf
        if var_p > 0.0:
            m4 = float(np.mean(d2 * d2))
            kurt[c] = m4 / (var_p * var_p)
    return std_p, log10_var, kurt


def calc_isgood_feats(eeg_window: np.ndarray) -> np.ndarray:
    """Compute 28 time-domain features (7 per channel) on a 1 s window.

    Per channel c in 0..3:
        [var_s, max - min, max, min, mean, kurtosis - 3.0, skewness]
    """
    arr = np.asarray(eeg_window, dtype=np.float64)
    if arr.shape[0] == 4 and arr.shape[1] != 4:
        arr = arr.T
    n = arr.shape[0]
    feats = np.zeros(28, dtype=np.float64)
    if n == 0:
        return feats
    for c in range(4):
        col = arr[:, c]
        max_v = float(np.max(col))
        min_v = float(np.min(col))
        mean_v = float(np.mean(col))
        d = col - mean_v
        d2 = d * d
        var_p = float(np.mean(d2))
        var_s = float(np.sum(d2) / (n - 1)) if n > 1 else var_p
        if var_p > 0.0:
            std_p = float(np.sqrt(var_p))
            kurt_excess = float(np.mean(d2 * d2) / (var_p * var_p)) - 3.0
            skew = float(np.mean(d2 * d) / (std_p * var_p))
        else:
            kurt_excess = -3.0
            skew = 0.0
        base = c * 7
        feats[base : base + 7] = (var_s, max_v - min_v, max_v, min_v, mean_v, kurt_excess, skew)
    return feats


def _hamming_symmetric(n: int) -> np.ndarray:
    """Symmetric Hamming window."""
    if n <= 0:
        return np.empty(0, dtype=np.float64)
    if n == 1:
        return np.ones(1, dtype=np.float64)
    idx = np.arange(n, dtype=np.float64)
    return 0.54 - 0.46 * np.cos(2.0 * np.pi * idx / (n - 1.0))


def do_psd(
    channel_window: np.ndarray,
    srate: float = 256.0,
    nfft: int = 512,
    normalize: bool = False,
    norm_bins_1based: np.ndarray | None = None,
) -> np.ndarray:
    """Single-channel Hamming-windowed FFT power spectrum."""
    x = np.asarray(channel_window, dtype=np.float64).ravel()
    n = x.size
    n_bins = nfft // 2 + 1
    if n == 0:
        return np.zeros(max(n_bins, 257), dtype=np.float64)
    centered = x - float(np.mean(x))
    win = _hamming_symmetric(n)
    windowed = centered * win
    fft_vals = np.fft.rfft(windowed, n=nfft) / float(n)
    psd = np.abs(fft_vals) ** 2
    if normalize:
        if norm_bins_1based is None:
            norm_bins_1based = np.arange(1, min(118, psd.size + 1), dtype=np.int64)
        denom = float(np.sum(psd[np.asarray(norm_bins_1based, dtype=np.int64) - 1]))
        if denom > 0.0:
            psd = psd / denom
    if psd.size < 257:
        padded = np.zeros(257, dtype=np.float64)
        padded[: psd.size] = psd
        return padded
    return psd


def calc_psd(
    eeg_window: np.ndarray,
    nan_window: np.ndarray | None = None,
    srate: float = 256.0,
    nfft: int = 512,
    normalize: bool = False,
) -> np.ndarray:
    """Compute 129x4 PSD matrix across 4 channels."""
    arr = np.asarray(eeg_window, dtype=np.float64)
    if arr.shape[0] == 4 and arr.shape[1] != 4:
        arr = arr.T
    if nan_window is not None:
        nan_arr = np.asarray(nan_window, dtype=np.float64)
        if nan_arr.shape[0] == 4 and nan_arr.shape[1] != 4:
            nan_arr = nan_arr.T
    else:
        nan_arr = np.zeros_like(arr)

    raw_psd = np.zeros((257, 4), dtype=np.float64)
    for c in range(4):
        valid = arr[nan_arr[:, c] == 0.0, c]
        raw_psd[:, c] = do_psd(valid, srate=srate, nfft=nfft, normalize=normalize)[:257]

    if srate == 256.0:
        return raw_psd[0:257:2, :].copy()  # 129 bins (0..128 Hz at 1 Hz steps)
    return raw_psd[:129, :].copy()


# ── Per-Sensor Decision Trees ──────────────────────────────────────────────


def dt_ear(std_p: float, log10_var: float, config: QualityConfig | None = None) -> float:
    """Decision tree for ear electrodes TP9 and TP10.

    Returns tree score: 2.0 (good), 1.0 (ok/fair), or 0.0 (bad/poor).
    """
    bad_std = 123.0 if config is None else config.ear_bad_std_uv
    ok_std = 46.0 if config is None else config.ear_good_std_uv
    min_log_var = 0.8221 if config is None else config.ear_min_log10_var

    if std_p >= bad_std:
        return 0.0
    elif std_p >= ok_std:
        return 1.0
    elif std_p >= 11.2808:
        return 2.0
    elif std_p >= 5.0421:
        return 2.0
    elif log10_var >= min_log_var:
        return 2.0
    else:
        return 0.0


def dt_front(
    std_p: float,
    prev_subwin_var: float,
    kurt: float,
    config: QualityConfig | None = None,
) -> float:
    """Decision tree for forehead electrodes AF7 and AF8.

    Returns tree score: 2.0 (good), 1.0 (ok/fair), or 0.0 (bad/poor).
    """
    check_std = 15.9952 if config is None else config.front_check_std_uv
    bad_std = 118.9837 if config is None else config.front_bad_std_uv
    ok_std = 46.8163 if config is None else config.front_good_std_uv
    subwin_var_thresh = 265.8586 if config is None else config.front_subwin_var_uv2
    kurt_thresh = 2.8146 if config is None else config.front_kurtosis_max

    if std_p >= check_std:
        if std_p >= bad_std:
            return 0.0
        elif std_p >= ok_std:
            return 1.0
        elif prev_subwin_var >= subwin_var_thresh:
            if kurt >= kurt_thresh:
                return 0.0
            else:
                return 2.0
        else:
            return 2.0
    else:
        return 2.0


def _tree_to_horseshoe(tree_score: float) -> float:
    """Map decision tree score (2, 1, 0) to horseshoe code (1=good, 2=ok, 4=bad)."""
    if tree_score == 2.0:
        return 1.0
    if tree_score == 1.0:
        return 2.0
    if tree_score == 0.0:
        return 4.0
    return tree_score


# ── Noise & Alert Gates ────────────────────────────────────────────────────


def good_data_check(channel_window: np.ndarray, threshold: float = 275.0) -> tuple[float, float]:
    """Check 1-second 2-36 Hz window variance and 1/6-second tail variance.

    Returns:
        (is_quiet, sub_win_var) where:
        - is_quiet = 1.0 if sample variance of full window < threshold else 0.0
        - sub_win_var = sample variance of the last round(N / 6) samples
    """
    x = np.asarray(channel_window, dtype=np.float64).ravel()
    n = x.size
    if n <= 1:
        return 1.0, 0.0
    std_full = float(np.std(x, ddof=1))
    is_quiet = 1.0 if (std_full * std_full) < threshold else 0.0
    sub_len = int(np.rint(n / 6.0))
    if sub_len <= 1:
        sub_var = 0.0
    else:
        std_sub = float(np.std(x[n - sub_len :], ddof=1))
        sub_var = std_sub * std_sub
    return is_quiet, sub_var


def is_epoch_good(
    psd_129x4: np.ndarray,
    isgood_feats_28: np.ndarray,
    feat_std_544: np.ndarray,
    feat_coeff_544: np.ndarray,
    intercept_4: np.ndarray,
    feat_mean_544: np.ndarray,
    threshold: float = 0.5,
    remap_center: np.ndarray | None = None,
    remap_slope: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Logistic regression + sigmoid remapping epoch quality classifier."""
    psd_flat = np.asarray(psd_129x4, dtype=np.float64).T.reshape(129, 4)
    psd_log = psd_flat.copy()
    pos = psd_log > 0.0
    psd_log[pos] = np.log10(psd_log[pos])
    feats_7x4 = np.asarray(isgood_feats_28, dtype=np.float64).reshape(4, 7).T
    design = np.vstack((psd_log, feats_7x4))  # (136, 4)
    std_mat = np.asarray(feat_std_544, dtype=np.float64).reshape(136, 4)
    mean_mat = np.asarray(feat_mean_544, dtype=np.float64).reshape(136, 4)
    coeff_mat = np.asarray(feat_coeff_544, dtype=np.float64).reshape(136, 4)
    z = np.sum(coeff_mat * ((design - mean_mat) / std_mat), axis=0) + np.asarray(intercept_4, dtype=np.float64)
    probs = 1.0 / (1.0 + np.exp(-z))
    if remap_center is not None and remap_slope is not None:
        probs = 1.0 / (1.0 + np.exp(-np.asarray(remap_slope) * (probs - np.asarray(remap_center))))
    flags = probs > threshold
    return probs, flags


def is_nan_alert(nan_buffer: np.ndarray, threshold: float = 0.06) -> bool:
    """Return True if fraction of NaN samples in the 3-second buffer >= threshold."""
    arr = np.asarray(nan_buffer, dtype=np.float64)
    if arr.size == 0:
        return False
    return bool(float(np.mean(arr > 0.0)) >= threshold)


def is_noise_alert(
    good_epoch_hist: np.ndarray,
    window: int = 100,
    threshold: float = 0.5,
) -> tuple[bool, float]:
    """Return (alert, noise_fraction) over the latest `window` epochs."""
    arr = np.asarray(good_epoch_hist, dtype=np.float64).ravel()
    if arr.size == 0:
        return False, 0.0
    tail = arr[- min(int(window), arr.size) :]
    noise_fraction = 1.0 - float(np.mean(tail))
    return bool(noise_fraction >= threshold), noise_fraction


# ── Reference & DRL Contact Check ──────────────────────────────────────────


def drlref_classification(drl_window: np.ndarray, muse_model: int = 3) -> bool:
    """Classify DRL/REF reference contact quality over a 2-second window.

    Args:
        drl_window: shape (N, 2) or (2, N) containing [DRL, REF] samples.
        muse_model: 0 (Muse 2014), 1 (Muse 2016), 2 (Smith), 3 (Muse 2), 4/5 (Muse S).
    """
    arr = np.asarray(drl_window, dtype=np.float64)
    if arr.ndim != 2 or arr.size == 0:
        return True
    if arr.shape[0] == 2 and arr.shape[1] != 2:
        arr = arr.T
    if arr.shape[1] < 2:
        return True
    abs_diff = np.abs(arr[:, 0] - arr[:, 1])
    if muse_model in (0, 1):
        diff_thresh = 45162.0
    elif muse_model == 2:
        diff_thresh = 250000.0
    elif muse_model == 3:
        diff_thresh = 200000.0
    elif muse_model in (4, 5):
        diff_thresh = 25000.0
    else:
        diff_thresh = 200000.0
    var_diff = float(np.var(abs_diff, ddof=1)) if abs_diff.size > 1 else 0.0
    return bool(abs_diff[-1] <= diff_thresh and var_diff <= 87035000.0)


# ── Configuration ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class QualityConfig:
    """Configuration for the Muse signal quality check algorithm and stream transport checks."""

    sfreq: float = 256.0
    window_s: float = 2.0
    update_s: float = 0.5
    stale_s: float = 0.5
    recovery_s: float = 3.0
    mains_hz: int = 60
    muse_model: int = 3
    # Ear decision tree thresholds (TP9, TP10)
    ear_good_std_uv: float = 46.0
    ear_bad_std_uv: float = 123.0
    ear_min_log10_var: float = 0.8221
    # Forehead decision tree thresholds (AF7, AF8)
    front_check_std_uv: float = 15.9952
    front_good_std_uv: float = 46.8163
    front_bad_std_uv: float = 118.9837
    front_subwin_var_uv2: float = 265.8586
    front_kurtosis_max: float = 2.8146
    # Quietness gate and alert thresholds
    quiet_var_uv2: float = 275.0
    nan_alert_fraction: float = 0.06
    noise_alert_fraction: float = 0.50
    hold_epochs: int = 3
    good_streak_epochs: int = 4
    couple_channels: bool = False
    # Transport / raw sanity limits
    rail_uv: float = 999.0
    decoder_max_uv: float = 1001.0
    clip_fraction: float = 0.01
    flat_std_uv: float = 0.5
    loss_warn_fraction: float = 0.01
    loss_bad_fraction: float = 0.05

    def __post_init__(self):
        if self.sfreq <= 72.0 or self.mains_hz not in (50, 60):
            raise ValueError("Use 50/60 Hz mains and a sampling rate above 72 Hz (2x 36 Hz filter)")
        if min(self.window_s, self.update_s, self.stale_s, self.recovery_s) <= 0:
            raise ValueError("Window, update, stale and recovery durations must be positive")
        if not (0 < self.ear_good_std_uv < self.ear_bad_std_uv
                and 0 < self.front_check_std_uv < self.front_good_std_uv < self.front_bad_std_uv):
            raise ValueError("Decision tree std thresholds must be ordered and positive")
        if not (0 <= self.loss_warn_fraction < self.loss_bad_fraction <= 1
                and 0 <= self.clip_fraction <= 1
                and 0 < self.nan_alert_fraction <= 1
                and 0 < self.noise_alert_fraction <= 1):
            raise ValueError("Fraction thresholds must be ordered within [0, 1]")


# ── Stateful Signal Quality Engine ─────────────────────────────────────────


class SignalQualityEngine:
    """Stateful multi-stage signal quality evaluator for 4-channel Muse EEG.

    Processes 4-channel EEG (`TP9, AF7, AF8, TP10`) through 3-second circular
    buffers, 2-36 Hz and 2-30 Hz Direct Form II Transposed IIR cascades, and runs
    `process_idle()` every `step_samples = round(srate / 20.0) * 2` samples (26 at 256 Hz).
    """

    def __init__(self, config: QualityConfig | None = None):
        self.config = config or QualityConfig()
        self.muse_model = int(self.config.muse_model)
        self.srate = float(self.config.sfreq)
        self.win_length = max(6, int(np.rint(self.srate * 1.0)))
        self.var_win_length = max(2, int(np.rint(self.win_length / 2.0)))
        self.sub_win_length = max(2, int(np.rint(self.win_length / 6.0)))
        self.step_samples = max(1, int(np.rint(self.srate / 20.0)) * 2)
        self.buff_length = max(self.win_length, int(np.rint(self.srate * 3.0)))
        self.nfft = 256 if self.srate == 220.0 else 512

        self.drl_srate = 10.0 if self.muse_model == 0 else 32.0
        self.drl_win_length = int(np.rint(self.drl_srate * 2.0))
        self.drl_buff_length = int(np.rint(self.drl_srate * 3.0))
        self.drl_step_samples = max(1, int(np.rint(self.drl_srate / 10.0)))

        self._b_lp36, self._a_lp36 = filter_36_lp_order6(self.srate)
        self._b_lp30, self._a_lp30 = filter_30_lp_order6(self.srate)
        self._b_hp2, self._a_hp2 = filter_2_hp_order2_fdesign(self.srate)

        self.reset()

    def reset(self) -> None:
        """Reset all buffers, filter states, and state machine counters to initial values."""
        self.win_step_counter = float(self.var_win_length)
        self.drl_step_counter = 1.0
        self.epochs_processed = 0

        # Circular buffers for raw and filtered signals
        self.eeg_buffer = CircBuffer2D(self.buff_length, 4, 0.0)
        self.nan_buffer = CircBuffer2D(self.buff_length, 4, 0.0)
        self.eeg2_36_buffer = CircBuffer2D(self.buff_length, 4, 0.0)
        self.eeg2_30_buffer = CircBuffer2D(self.buff_length, 4, 0.0)
        self.drl_buffer = CircBuffer2D(self.drl_buff_length, 2, 0.0)

        # DFIIT IIR filter state vectors z: shape (4, order)
        self._z_lp36 = np.zeros((4, len(self._b_lp36) - 1), dtype=np.float64)
        self._z_hp2_36 = np.zeros((4, len(self._b_hp2) - 1), dtype=np.float64)
        self._z_lp30 = np.zeros((4, len(self._b_lp30) - 1), dtype=np.float64)
        self._z_hp2_30 = np.zeros((4, len(self._b_hp2) - 1), dtype=np.float64)

        # Latest feature values
        self.std_uv = np.zeros(4, dtype=np.float64)
        self.log10_var = np.full(4, -np.inf, dtype=np.float64)
        self.kurtosis = np.full(4, np.nan, dtype=np.float64)
        self.prev_sub_win_var = np.zeros(4, dtype=np.float64)
        self.full_win_var = np.zeros(4, dtype=np.float64)
        self.isgood_feats = np.zeros(28, dtype=np.float64)
        self.psd = np.zeros((129, 4), dtype=np.float64)

        # Per-sensor decision history and hysteresis state
        self.raw_hist_15 = CircBuffer2D(15, 4, 0.0)
        self.raw_hist_60 = CircBuffer2D(60, 4, 0.0)
        self.prop_bad_15 = np.zeros(4, dtype=np.float64)
        self.prop_ok_15 = np.zeros(4, dtype=np.float64)
        self.prop_good_15 = np.zeros(4, dtype=np.float64)
        self.prop_bad_60 = np.zeros(4, dtype=np.float64)
        self.prop_good_60 = np.zeros(4, dtype=np.float64)

        self.sensor_state = np.zeros(4, dtype=np.float64)
        self.good_streak = np.zeros(4, dtype=np.float64)
        self.bad_streak = np.zeros(4, dtype=np.float64)
        self.smooth_score = np.zeros(4, dtype=np.float64)

        self.held_hist_60 = CircBuffer2D(60, 4, 0.0)
        self.coupled_hist_60 = CircBuffer2D(60, 4, 0.0)
        self.good_epoch_hist_300 = CircBuffer1D(300, 1.0)
        self.hold_reset_val = float(self.config.hold_epochs)
        self.hold_counter = np.full(4, self.hold_reset_val, dtype=np.float64)
        self.no_good_ear_counter = 0.0
        self.dot_score_5 = np.zeros(4, dtype=np.float64)
        self.continuous_score = np.ones(4, dtype=np.float64)
        self.all_good_30_latched = 0.0

        self.h_raw = np.zeros(4, dtype=np.float64)
        self.h_sensor = np.zeros(4, dtype=np.float64)
        self.h_coupled = np.zeros(4, dtype=np.float64)

        # Noise gate state
        self.sub_win_var_hist = CircBuffer2D(3, 4, 0.0)
        self.quiet_hist_15 = CircBuffer2D(15, 4, 1.0)
        self.quiet_hist_30 = CircBuffer2D(30, 4, 1.0)
        self.ear_quiet_hist_75 = CircBuffer1D(75, 1.0)
        self.last_ear_quiet_flag = True
        self.noise_score = 0.0
        self.is_nan_alert = False
        self.is_noise_alert = False
        self.noise_fraction_100 = 0.0

        # Reference DRL & auxiliary inputs
        self.is_drl_connect = True
        self.drl_ever_fed = False
        self.acc = np.zeros(3, dtype=np.float64)
        self.blink_hist_15 = CircBuffer1D(15, 0.0)
        self.last_blink_detected = False

    def set_muse_model(self, muse_model: int) -> None:
        """Update device model and reset state."""
        self.muse_model = int(muse_model)
        self.srate = 220.0 if self.muse_model == 0 else float(self.config.sfreq)
        self.win_length = max(6, int(np.rint(self.srate * 1.0)))
        self.var_win_length = max(2, int(np.rint(self.win_length / 2.0)))
        self.sub_win_length = max(2, int(np.rint(self.win_length / 6.0)))
        self.step_samples = max(1, int(np.rint(self.srate / 20.0)) * 2)
        self.buff_length = max(self.win_length, int(np.rint(self.srate * 3.0)))
        self.nfft = 256 if self.srate == 220.0 else 512
        self.drl_srate = 10.0 if self.muse_model == 0 else 32.0
        self.drl_win_length = int(np.rint(self.drl_srate * 2.0))
        self.drl_buff_length = int(np.rint(self.drl_srate * 3.0))
        self.drl_step_samples = max(1, int(np.rint(self.drl_srate / 10.0)))
        self._b_lp36, self._a_lp36 = filter_36_lp_order6(self.srate)
        self._b_lp30, self._a_lp30 = filter_30_lp_order6(self.srate)
        self._b_hp2, self._a_hp2 = filter_2_hp_order2_fdesign(self.srate)
        self.reset()

    def process_drl(self, drl_ref: np.ndarray) -> bool:
        """Feed [DRL, REF] samples (shape (2, n) or (n, 2) or (2,))."""
        arr = np.asarray(drl_ref, dtype=np.float64)
        if arr.ndim == 1:
            if arr.size != 2:
                raise ValueError("Expected 2 DRL/REF values [drl, ref]")
            arr = arr.reshape(2, 1)
        elif arr.ndim == 2:
            if arr.shape[0] != 2 and arr.shape[1] == 2:
                arr = arr.T
            if arr.shape[0] != 2:
                raise ValueError("Expected DRL/REF shape (2, n) or (n, 2)")
        else:
            raise ValueError("Expected 1D or 2D DRL/REF array")

        self.drl_ever_fed = True
        n = arr.shape[1]
        idx = 0
        while idx < n:
            step = min(n - idx, max(1, int(self.drl_step_counter)))
            self.drl_buffer.update_chunk(arr[:, idx : idx + step])
            self.drl_step_counter -= float(step)
            idx += step
            if self.drl_step_counter <= 0.0:
                self.drl_step_counter = float(self.drl_step_samples)
                win = self.drl_buffer.latest(self.drl_win_length)
                self.is_drl_connect = drlref_classification(win, self.muse_model)
        return self.is_drl_connect

    def process_eeg(
        self,
        eeg: np.ndarray,
        *,
        acc: np.ndarray | None = None,
        blink: bool | None = None,
    ) -> None:
        """Process 4-channel EEG samples (shape (4, n) or (4,))."""
        arr = np.asarray(eeg, dtype=np.float64)
        if arr.ndim == 1:
            if arr.size != 4:
                raise ValueError("Expected 4 EEG channels")
            arr = arr.reshape(4, 1)
        elif arr.ndim != 2 or arr.shape[0] != 4:
            raise ValueError("Expected EEG shape (4, n)")

        if acc is not None:
            acc_arr = np.asarray(acc, dtype=np.float64).ravel()
            if acc_arr.size >= 3:
                self.acc = acc_arr[:3].copy()
        if blink is not None:
            self.last_blink_detected = bool(blink)

        n = arr.shape[1]
        if n == 0:
            return

        # Step between process_idle epochs in exact increments of win_step_counter
        idx = 0
        while idx < n:
            step = min(n - idx, max(1, int(self.win_step_counter)))
            chunk = arr[:, idx : idx + step]
            invalid = ~np.isfinite(chunk)
            out_of_range = invalid | (np.abs(chunk) > self.config.decoder_max_uv)

            self.eeg_buffer.update_chunk(chunk)
            self.nan_buffer.update_chunk(invalid.astype(np.float64))

            # Guard recursive IIR filter states from NaN/Inf/1e300 overflow
            safe_chunk = np.where(out_of_range, 0.0, chunk)
            lp36, self._z_lp36 = filter_data_dfiit(self._z_lp36, safe_chunk, self._b_lp36, self._a_lp36)
            bp2_36, self._z_hp2_36 = filter_data_dfiit(self._z_hp2_36, lp36, self._b_hp2, self._a_hp2)
            self.eeg2_36_buffer.update_chunk(bp2_36)

            lp30, self._z_lp30 = filter_data_dfiit(self._z_lp30, safe_chunk, self._b_lp30, self._a_lp30)
            bp2_30, self._z_hp2_30 = filter_data_dfiit(self._z_hp2_30, lp30, self._b_hp2, self._a_hp2)
            self.eeg2_30_buffer.update_chunk(bp2_30)

            self.win_step_counter -= float(step)
            idx += step
            if self.win_step_counter <= 0.0:
                self.win_step_counter = float(self.step_samples)
                self.process_idle()

    def process_idle(self) -> None:
        """Per-epoch SQC evaluation."""
        self.epochs_processed += 1

        # 1. NaN alert over the 3-second buffer
        self.is_nan_alert = is_nan_alert(self.nan_buffer.data, self.config.nan_alert_fraction)

        # 2. Variance/kurtosis features on latest 0.5 s (128 samples at 256 Hz) of 2-36 Hz (or 2-30 Hz for model 2)
        var_source = self.eeg2_30_buffer if self.muse_model == 2 else self.eeg2_36_buffer
        var_win = var_source.latest(self.var_win_length)
        self.std_uv, self.log10_var, self.kurtosis = calc_var_feats(var_win)

        # 3. Blink circular buffer update
        self.blink_hist_15.update(1.0 if self.last_blink_detected else 0.0)

        # 4. Read previous epoch's sub-window variance from sub_win_var_hist
        prev_row_idx = self.sub_win_var_hist.write_idx - 2
        if prev_row_idx < 0:
            prev_row_idx = self.sub_win_var_hist.length - 1
        self.prev_sub_win_var = self.sub_win_var_hist.data[prev_row_idx].copy()

        # 5. Per-sensor decision trees, hysteresis, hold, and coupling
        self._calc_horseshoe()

        # 6. Quietness gate and noise score
        self._calc_noise()

        # 7. Noise alert over the latest 100 epochs
        self.is_noise_alert, self.noise_fraction_100 = is_noise_alert(
            self.good_epoch_hist_300.latest(100),
            window=100,
            threshold=self.config.noise_alert_fraction,
        )

    def _calc_horseshoe(self) -> bool:
        """Per-sensor decision trees, hysteresis state machine, hold, and coupling."""
        c = self.config
        # Step 1: Decision trees dt_ear (ch 0, 3) and dt_front (ch 1, 2)
        tree_scores = np.array(
            [
                dt_ear(float(self.std_uv[0]), float(self.log10_var[0]), c),
                dt_front(float(self.std_uv[1]), float(self.prev_sub_win_var[1]), float(self.kurtosis[1]), c),
                dt_front(float(self.std_uv[2]), float(self.prev_sub_win_var[2]), float(self.kurtosis[2]), c),
                dt_ear(float(self.std_uv[3]), float(self.log10_var[3]), c),
            ],
            dtype=np.float64,
        )
        # When DRL/REF is not streamed (e.g. BrainFlow 4-ch EEG or CSV replay), catch dead 0-uV flatlines on frontal channels
        if not self.drl_ever_fed:
            for ch_idx in (1, 2):
                if self.std_uv[ch_idx] < c.flat_std_uv and self.log10_var[ch_idx] < c.ear_min_log10_var:
                    tree_scores[ch_idx] = 0.0

        h = np.array([_tree_to_horseshoe(s) for s in tree_scores], dtype=np.float64)
        self.h_raw = h.copy()

        # Step 2: Update 15-epoch and 60-epoch raw horseshoe history buffers
        self.raw_hist_15.update_row(h)
        self.raw_hist_60.update_row(h)

        self.prop_bad_15 = np.mean(self.raw_hist_15.data > 2.0, axis=0)
        self.prop_ok_15 = np.mean(self.raw_hist_15.data == 2.0, axis=0)
        self.prop_good_15 = np.mean(self.raw_hist_15.data < 2.0, axis=0)
        self.prop_bad_60 = np.mean(self.raw_hist_60.data > 2.0, axis=0)
        self.prop_good_60 = np.mean(self.raw_hist_60.data < 2.0, axis=0)

        # Step 3: Per-channel streak counters and 5-state machine (0, 1, 2, 3, 4)
        acc_still = bool(np.all(self.acc < 305176735.857309))
        acc_mid = bool(np.any(self.acc > 762941839.643272))
        acc_low = bool(np.all(self.acc < 1525883679.28654))
        streak_needed = float(c.good_streak_epochs - 1)  # default > 3.0 (4 epochs)

        for ch in range(4):
            val = int(h[ch])
            if val == 1:
                self.good_streak[ch] += 1.0
                self.bad_streak[ch] = 0.0
                streak = self.good_streak[ch]
            elif val < 3:
                self.good_streak[ch] = 0.0
                self.bad_streak[ch] = 0.0
                streak = 0.0
            else:
                self.good_streak[ch] = 0.0
                self.bad_streak[ch] += 1.0
                streak = 0.0

            if streak > streak_needed:
                self.sensor_state[ch] = 1.0
            else:
                if (self.prop_good_60[ch] < 0.1 or self.prop_bad_15[ch] > 0.6) and self.sensor_state[ch] == 1.0:
                    self.sensor_state[ch] = 0.0
                else:
                    if self.prop_bad_60[ch] > 0.8 and acc_still and self.sensor_state[ch] == 0.0:
                        self.sensor_state[ch] = 2.0
                    elif self.prop_bad_15[ch] > 0.7 and acc_still and self.sensor_state[ch] == 4.0:
                        self.sensor_state[ch] = 0.0
                    elif self.prop_bad_15[ch] > 0.5 and acc_mid and self.sensor_state[ch] == 2.0:
                        self.sensor_state[ch] = 3.0
                    elif (acc_low or self.prop_bad_15[ch] <= 0.5) and self.sensor_state[ch] == 3.0:
                        self.sensor_state[ch] = 0.0
                    elif self.prop_bad_15[ch] < 0.3 and self.sensor_state[ch] == 0.0:
                        self.sensor_state[ch] = 4.0

            state = self.sensor_state[ch]
            if state == 4.0 and val > 2:
                h[ch] = 2.0
            if state != 2.0:
                self.smooth_score[ch] = min(0.5 * float(self.prop_ok_15[ch]) + float(self.prop_good_15[ch]) + 0.1, 1.0)
            if state == 1.0:
                h[ch] = 1.0

        # Step 4: Global overrides (all sensors in state 2 or 3, DRL disconnect, ear quietness gate)
        if np.all(self.sensor_state == 2.0):
            h[:] = 4.0
        if np.all(self.sensor_state == 3.0):
            h[0] = 4.0 if self.acc[0] <= 114441275946.491 else 2.0
            h[1] = 4.0 if self.acc[1] <= 114441275946.491 else 2.0
            h[2] = 4.0 if self.acc[2] <= 114441275946.491 else 2.0
            xz_norm = float(np.sqrt(self.acc[0] ** 2 + self.acc[2] ** 2))
            h[3] = 4.0 if xz_norm <= 114441275946.491 else 2.0

        if not self.is_drl_connect:
            h[:] = 4.0

        if not self.last_ear_quiet_flag:
            self.no_good_ear_counter += 1.0
        else:
            self.no_good_ear_counter = 0.0
        if self.no_good_ear_counter >= 15.0:
            if h[0] == 1.0:
                h[0] = 2.0
            if h[3] == 1.0:
                h[3] = 2.0

        # Step 5: 3-epoch transition hold
        prev_idx = self.held_hist_60.write_idx - 2
        if prev_idx < 0:
            prev_idx = self.held_hist_60.length - 1
        prev_held = self.held_hist_60.data[prev_idx].copy()

        if np.any(self.blink_hist_15.data != 0.0):
            ear_quiet_mean = float(np.mean(self.ear_quiet_hist_75.data))
            if ear_quiet_mean > 0.2:
                for ch in range(4):
                    if prev_held[ch] == 1.0 and self.hold_counter[ch] == 0.0:
                        self.hold_counter[ch] = 1.0

        for ch in range(4):
            cnt = self.hold_counter[ch]
            if cnt <= 0.0:
                held = h[ch]
                if h[ch] != prev_held[ch]:
                    self.hold_counter[ch] = self.hold_reset_val
            else:
                held = prev_held[ch]
                h[ch] = held
                self.hold_counter[ch] = cnt - 1.0

        self.held_hist_60.update_row(h)
        self.h_sensor = h.copy()

        # Step 6: Cross-channel coupling
        coupled = h.copy()
        coupled[1] = min(h[1], h[2])
        coupled[2] = coupled[1]
        if h[0] < 4.0 and h[3] < 4.0:
            ear_min = min(h[0], h[3])
            coupled[0] = ear_min
            coupled[3] = ear_min
        self.h_coupled = coupled.copy()
        self.coupled_hist_60.update_row(coupled)

        # 30-epoch all-good latch
        last_30 = self.coupled_hist_60.latest(30)
        if np.all(last_30 == 1.0) and self.all_good_30_latched == 0.0:
            self.all_good_30_latched = 1.0
        if not self.is_drl_connect:
            self.all_good_30_latched = 0.0

        # Good epoch indicator (> 2 channels coupled == 1.0) pushed to 300-epoch buffer
        is_good_epoch = int(np.sum(coupled == 1.0)) > 2
        self.good_epoch_hist_300.update(1.0 if is_good_epoch else 0.0)

        # 5-epoch weighted dot score and continuous quality score
        last_5_held = self.held_hist_60.latest(5)
        mapped_5 = last_5_held - 1.0
        mapped_5 = np.where(mapped_5 == 3.0, 2.0, mapped_5)
        mapped_5 = np.maximum(mapped_5, 0.0) / 2.0
        self.dot_score_5 = DOT_WEIGHTS_5 @ mapped_5

        for ch in range(4):
            c_val = coupled[ch]
            if c_val == 4.0 and self.is_drl_connect:
                self.continuous_score[ch] = 0.0
            elif c_val == 2.0:
                score = 0.75 * float(self.prop_good_15[ch]) + 0.25
                self.continuous_score[ch] = float(np.clip(score, 0.0, 1.0))
            elif c_val == 1.0:
                self.continuous_score[ch] = 1.0
            else:
                self.continuous_score[ch] = 0.0

        return is_good_epoch

    def _calc_noise(self) -> None:
        """1-second window quietness gate and noise score."""
        thresh = 200.0 if self.muse_model == 2 else float(self.config.quiet_var_uv2)
        win_256 = self.eeg2_36_buffer.latest(self.win_length)
        is_quiet = np.zeros(4, dtype=np.float64)
        sub_win_var = np.zeros(4, dtype=np.float64)

        for ch in range(4):
            col = win_256[:, ch]
            is_quiet[ch], sub_win_var[ch] = good_data_check(col, threshold=thresh)
            std_full = float(np.std(col, ddof=1)) if col.size > 1 else 0.0
            self.full_win_var[ch] = std_full * std_full

        self.sub_win_var_hist.update_row(sub_win_var)

        drl_val = 1.0 if self.is_drl_connect else 0.0
        quiet_good = is_quiet * drl_val
        self.quiet_hist_15.update_row(quiet_good)
        self.quiet_hist_30.update_row(quiet_good)

        col_sums = np.sum(self.quiet_hist_15.data, axis=0)
        self.last_ear_quiet_flag = bool(col_sums[0] > 3.0 or col_sums[3] > 3.0)
        self.ear_quiet_hist_75.update(1.0 if self.last_ear_quiet_flag else 0.0)

        if np.any(self.h_coupled > 2.0):
            self.noise_score = 1.0
        else:
            min_ear_var = float(min(sub_win_var[0], sub_win_var[3]))
            if min_ear_var > 0.0:
                log_v = float(np.log10(min_ear_var))
                scaled = float(np.clip(log_v * 0.8724979824515197 - 1.8595474262951814, 0.0, 1.0))
                self.noise_score = scaled * scaled
            else:
                self.noise_score = 0.0


# ── Thread-Safe QualityMonitor ─────────────────────────────────────────────


class QualityMonitor:
    """One producer feeds batches; a worker scores bounded snapshots at 2 Hz.

    feed() never filters, computes spectra, writes files, or calls BrainFlow.
    evaluate() runs the stateful signal quality engine and is also
    available without a worker for deterministic replay/tests.
    Packet counters are Muse's 16-bit counter repeated for 12 EEG samples.
    """

    def __init__(self, config: QualityConfig | None = None, *, output=None, source="muse", muse_packets=True):
        self.config = config or QualityConfig()
        self.source = source
        self.muse_packets = muse_packets
        self.output = Path(output) if output is not None else None
        self._capacity = round(self.config.sfreq * self.config.window_s)
        self._sqc_capacity = max(self._capacity, round(self.config.sfreq * 3.0))
        self._eeg = np.empty((4, 0), dtype=np.float64)
        self._timestamps = np.empty(0, dtype=np.float64)
        self._packets = np.empty(0, dtype=np.float64)
        self._pending_eeg = np.empty((4, 0), dtype=np.float64)
        self._pending_drl = np.empty((2, 0), dtype=np.float64)
        self._reset_sqc = False
        self._arrival = None
        self._generation = 0
        self._scored_generation = -1
        self._sqc = SignalQualityEngine(self.config)
        self._last_reasons = {ch: [] for ch in CHANNELS}
        self._lock = threading.Lock()
        self._eval_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._health = {
            "reader_error": None,
            "recording_error": None,
            "recording_backlog_batches": 0,
            "rejected_recording_samples": 0,
            "board_backlog_samples": None,
            "quality_worker_error": None,
        }
        self._report = None
        self._reported_at = None
        self.output_error = None

    def feed(self, eeg, timestamps, packets=None, *, drl_ref=None, now=None):
        eeg = np.asarray(eeg, dtype=np.float64)
        timestamps = np.asarray(timestamps, dtype=np.float64)
        if eeg.ndim != 2 or eeg.shape[0] != 4 or timestamps.shape != (eeg.shape[1],):
            raise ValueError("Expected EEG (4, n) and timestamps (n,)")
        if packets is not None and np.shape(packets) != timestamps.shape:
            raise ValueError("Packet counters must match timestamps")
        if not eeg.shape[1]:
            return
        packet_values = (
            np.asarray(packets, dtype=np.float64)
            if packets is not None
            else np.full(timestamps.size, np.nan, dtype=np.float64)
        )
        drl_arr = None
        if drl_ref is not None:
            drl_arr = np.asarray(drl_ref, dtype=np.float64)
            if drl_arr.ndim == 1 and drl_arr.size == 2:
                drl_arr = drl_arr.reshape(2, 1)
            elif drl_arr.ndim == 2 and drl_arr.shape[0] != 2 and drl_arr.shape[1] == 2:
                drl_arr = drl_arr.T
            if drl_arr.ndim != 2 or drl_arr.shape[0] != 2:
                raise ValueError("Expected drl_ref (2, m) or (m, 2)")

        with self._lock:
            # Discard pre-disconnection history; recovery requires fresh data.
            arrival = time.monotonic() if now is None else now
            if self._arrival is not None and arrival - self._arrival > self.config.stale_s:
                self._eeg = self._eeg[:, :0]
                self._timestamps = self._timestamps[:0]
                self._packets = self._packets[:0]
                self._pending_eeg = self._pending_eeg[:, :0]
                self._pending_drl = self._pending_drl[:, :0]
                self._reset_sqc = True
            self._eeg = np.concatenate((self._eeg, eeg[:, -self._capacity :]), axis=1)[:, -self._capacity :]
            self._timestamps = np.concatenate((self._timestamps, timestamps[-self._capacity :]))[-self._capacity :]
            self._packets = np.concatenate((self._packets, packet_values[-self._capacity :]))[-self._capacity :]
            self._pending_eeg = np.concatenate(
                (self._pending_eeg, eeg[:, -self._sqc_capacity :]), axis=1
            )[:, -self._sqc_capacity :]
            if drl_arr is not None and drl_arr.shape[1]:
                drl_cap = self._sqc.drl_buff_length
                self._pending_drl = np.concatenate(
                    (self._pending_drl, drl_arr[:, -drl_cap:]), axis=1
                )[:, -drl_cap:]
            self._arrival = arrival
            self._generation += 1

    def feed_drl(self, drl_ref):
        """Feed Muse DRL/REF reference samples (shape (2, m) or (m, 2)) into the monitor."""
        arr = np.asarray(drl_ref, dtype=np.float64)
        if arr.ndim == 1 and arr.size == 2:
            arr = arr.reshape(2, 1)
        elif arr.ndim == 2 and arr.shape[0] != 2 and arr.shape[1] == 2:
            arr = arr.T
        if arr.ndim != 2 or arr.shape[0] != 2:
            raise ValueError("Expected drl_ref (2, m) or (m, 2)")
        if not arr.shape[1]:
            return
        with self._lock:
            drl_cap = self._sqc.drl_buff_length
            self._pending_drl = np.concatenate((self._pending_drl, arr[:, -drl_cap:]), axis=1)[:, -drl_cap:]
            self._generation += 1

    def set_health(self, **values):
        if values.keys() - self._health.keys():
            raise ValueError("Unknown acquisition health field")
        with self._lock:
            self._health.update(
                {key: value.item() if isinstance(value, np.generic) else value for key, value in values.items()}
            )

    @property
    def latest(self):
        with self._lock:
            report = deepcopy(self._report)
            arrival, reported_at = self._arrival, self._reported_at
        # A blocked sidecar must not leave a permanently green live indicator.
        if report is not None and self._thread is not None:
            now = time.monotonic()
            reason = None
            if arrival is not None and now - arrival > self.config.stale_s:
                reason = "stale_stream"
            elif reported_at is not None and now - reported_at > 2 * self.config.update_s:
                reason = "quality_worker_delayed"
            if reason:
                report["stream_status"], report["stream_reasons"] = "unknown", [reason]
                report["last_sample_age_s"] = now - arrival if arrival is not None else None
                for channel in report["channels"].values():
                    channel["status"], channel["reasons"] = "unknown", [reason]
        return report

    def _stream_health(self, ts, packets):
        c = self.config
        reasons = []
        if not np.isfinite(ts).all() or (ts <= 0).any() or (np.diff(ts) <= 0).any():
            return "unknown", ["invalid_timestamps"], None, "unavailable"
        if self.muse_packets and np.isfinite(packets).all():
            if (packets < 0).any() or (packets > 65535).any() or (packets != np.floor(packets)).any():
                return "unknown", ["invalid_packet_counter"], None, "packet_counter"
            # Repeated values inside a 12-sample packet are expected.
            boundaries = np.r_[0, np.flatnonzero(np.diff(packets)) + 1, len(packets)]
            if (np.diff(boundaries) > 12).any():
                return "unknown", ["repeated_packets"], None, "packet_counter"
            changes = np.diff(packets.astype(np.int64)) % 65536
            changes = changes[changes != 0]
            if (changes > 32768).any():
                return "unknown", ["out_of_order_packets"], None, "packet_counter"
            missing = int(np.maximum(changes - 1, 0).sum()) * 12
            method = "packet_counter"
        else:
            # Timestamps are only an estimate; Muse drivers may smooth BLE jitter.
            missing = int(np.maximum(np.rint(np.diff(ts) * c.sfreq) - 1, 0).sum())
            method = "timestamps"
        loss = missing / (len(ts) + missing)
        if loss > c.loss_bad_fraction:
            status, reasons = "unknown", ["sample_loss"]
        elif loss > c.loss_warn_fraction:
            status, reasons = "fair", ["sample_loss"]
        else:
            status = "ok"
        return status, reasons, loss, method

    def _channel_reasons(self, idx: int, name: str, raw_values: np.ndarray, h_val: float) -> list[str]:
        """Derive diagnostic reason codes matching the active decision branch."""
        c = self.config
        if h_val <= 1.0:
            self._last_reasons[name] = []
            return []

        reasons = []
        if not self._sqc.is_drl_connect:
            reasons.append("drl_disconnected")
        if self._sqc.is_nan_alert:
            reasons.append("nan_alert")

        std_p = float(self._sqc.std_uv[idx])
        log10_v = float(self._sqc.log10_var[idx])
        kurt = float(self._sqc.kurtosis[idx])
        prev_sub = float(self._sqc.prev_sub_win_var[idx])
        clipped = float(np.mean(np.abs(raw_values) >= c.rail_uv)) if raw_values.size else 0.0

        if name in EAR:
            if std_p >= c.ear_bad_std_uv:
                reasons.append("saturation" if clipped > c.clip_fraction else "high_amplitude")
            elif std_p >= c.ear_good_std_uv:
                reasons.append("high_amplitude")
            elif std_p < 5.0421 and log10_v < c.ear_min_log10_var:
                reasons.append("flat_line")
            elif self._sqc.no_good_ear_counter >= 15.0:
                reasons.append("ear_noise_gate")
        else:
            if not self._sqc.drl_ever_fed and std_p < c.flat_std_uv and log10_v < c.ear_min_log10_var:
                reasons.append("flat_line")
            elif std_p >= c.front_bad_std_uv:
                reasons.append("saturation" if clipped > c.clip_fraction else "high_amplitude")
            elif std_p >= c.front_good_std_uv:
                reasons.append("high_amplitude")
            elif std_p >= c.front_check_std_uv and prev_sub >= c.front_subwin_var_uv2 and kurt >= c.front_kurtosis_max:
                reasons.append("kurtosis_artifact")

        if not reasons and clipped > c.clip_fraction:
            reasons.append("saturation")
        if not reasons:
            reasons = self._last_reasons[name][:] or ["high_amplitude"]
        self._last_reasons[name] = reasons[:]
        return reasons

    def _channel(self, values: np.ndarray, name: str | None = None):
        c = self.config
        idx = CHANNELS.index(name) if name in CHANNELS else 0
        if not np.isfinite(values).all():
            return "unknown", ["invalid_values"], True, {"invalid_fraction": float(np.mean(~np.isfinite(values)))}
        # Reject impossible decoder output before squaring (including finite 1e300).
        if np.max(np.abs(values)) > c.decoder_max_uv:
            return "unknown", ["out_of_range"], True, {}

        h_arr = self._sqc.h_coupled if c.couple_channels else self._sqc.h_sensor
        h_val = float(h_arr[idx])
        status = HORSESHOE_TO_STATUS.get(h_val, "unknown")
        reasons = self._channel_reasons(idx, name or CHANNELS[idx], values, h_val)

        std_p = float(self._sqc.std_uv[idx])
        log10_v = float(self._sqc.log10_var[idx])
        kurt = float(self._sqc.kurtosis[idx])
        clipped = float(np.mean(np.abs(values) >= c.rail_uv))

        metrics = {
            "rms_uv": std_p,
            "std_uv": std_p,
            "raw_std_uv": float(np.std(values)),
            "log10_var": log10_v if np.isfinite(log10_v) else None,
            "kurtosis": kurt if np.isfinite(kurt) else None,
            "sub_window_var_uv2": float(self._sqc.prev_sub_win_var[idx]),
            "window_var_uv2": float(self._sqc.full_win_var[idx]),
            "peak_to_peak_uv": float(np.ptp(values)),
            "clipped_fraction": clipped,
            "invalid_fraction": 0.0,
            "horseshoe": h_val,
            "horseshoe_raw": float(self._sqc.h_raw[idx]),
            "horseshoe_coupled": float(self._sqc.h_coupled[idx]),
            "sensor_state": int(self._sqc.sensor_state[idx]),
            "quality_score": float(self._sqc.continuous_score[idx]),
        }
        return status, reasons, False, metrics

    def evaluate(self, *, now=None, wall_time=None):
        now = time.monotonic() if now is None else now
        c = self.config
        with self._eval_lock:
            with self._lock:
                eeg, ts, packets = self._eeg.copy(), self._timestamps.copy(), self._packets.copy()
                pending_eeg = self._pending_eeg.copy()
                pending_drl = self._pending_drl.copy()
                reset_sqc = self._reset_sqc
                self._pending_eeg = self._pending_eeg[:, :0]
                self._pending_drl = self._pending_drl[:, :0]
                self._reset_sqc = False
                arrival, generation, health = self._arrival, self._generation, self._health.copy()

            if reset_sqc:
                self._sqc.reset()
                for ch in CHANNELS:
                    self._last_reasons[ch] = []

            if pending_drl.shape[1]:
                self._sqc.process_drl(pending_drl)
            if pending_eeg.shape[1]:
                self._sqc.process_eeg(pending_eeg)

            status, reasons, loss, method = "unknown", ["warming_up"], None, "unavailable"
            fresh = arrival is not None and now - arrival <= c.stale_s
            if not fresh:
                reasons = ["stale_stream" if arrival is not None else "no_data"]
                self._sqc.reset()
                for ch in CHANNELS:
                    self._last_reasons[ch] = []
            elif len(ts) >= self._capacity:
                status, reasons, loss, method = self._stream_health(ts, packets)
            if health["reader_error"]:
                status, reasons = "unknown", reasons + ["reader_error"]
            if health["quality_worker_error"]:
                status, reasons = "unknown", reasons + ["quality_worker_error"]
            if health["recording_error"]:
                reasons = reasons + ["recording_error"]
                if status == "ok":
                    status = "fair"

            report = {
                "type": "signal_quality",
                "version": 1,
                "timestamp": time.time() if wall_time is None else wall_time,
                "source": self.source,
                "thresholds": asdict(c),
                "window_start": float(ts[0]) if len(ts) and np.isfinite(ts[0]) else None,
                "window_end": float(ts[-1]) if len(ts) and np.isfinite(ts[-1]) else None,
                "stream_status": status,
                "stream_reasons": reasons,
                "missing_fraction": loss,
                "loss_estimate_method": method,
                "last_sample_age_s": max(0.0, now - arrival) if arrival is not None else None,
                "samples_in_window": len(ts),
                "is_nan_alert": bool(self._sqc.is_nan_alert),
                "is_noise_alert": bool(self._sqc.is_noise_alert),
                "is_drl_connected": bool(self._sqc.is_drl_connect),
                "noise_score": float(self._sqc.noise_score),
                "health": health,
                "channels": {},
                "quality_output_error": self.output_error,
            }

            for i, ch in enumerate(CHANNELS):
                metrics = {}
                candidate, why = "unknown", reasons[:]
                if status != "unknown":
                    candidate, why, _, metrics = self._channel(eeg[i], ch)
                else:
                    self._last_reasons[ch] = []
                report["channels"][ch] = {"status": candidate, "reasons": why, **metrics}

            self._scored_generation = generation
            with self._lock:
                self._report = report
                self._reported_at = time.monotonic()
            return deepcopy(report)

    def start(self):
        if self._thread is not None:
            raise RuntimeError("Quality monitor already started")
        # Fail before acquisition if the requested sidecar cannot be opened.
        output = self.output.open("w") if self.output is not None else None
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(output,), daemon=True)
        self._thread.start()

    def _run(self, output):
        try:
            while True:
                stopping = self._stop.is_set()
                report = self.evaluate()
                if output is not None:
                    try:
                        output.write(json.dumps(report, allow_nan=False) + "\n")
                        output.flush()
                    except OSError as exc:
                        self.output_error = str(exc)
                        log.exception("Quality sidecar failed; live monitoring continues")
                        output.close()
                        output = None
                if stopping:
                    break
                self._stop.wait(self.config.update_s)
        except Exception as exc:
            self.set_health(quality_worker_error=str(exc))
            log.exception("Quality worker failed")
            with self._lock:
                self._report = {
                    "type": "signal_quality",
                    "version": 1,
                    "timestamp": time.time(),
                    "stream_status": "unknown",
                    "stream_reasons": ["quality_worker_error"],
                    "health": self._health.copy(),
                    "channels": {ch: {"status": "unknown", "reasons": ["quality_worker_error"]} for ch in CHANNELS},
                }
        finally:
            if output is not None:
                output.close()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise RuntimeError("Quality worker did not stop")


def format_quality(report):
    if report is None:
        return "Signal quality: warming up"
    channels = " | ".join(
        f"{ch}: {item['status']}" + (f" ({', '.join(item['reasons'])})" if item["reasons"] else "")
        for ch, item in report["channels"].items()
    )
    reasons = f" ({', '.join(report['stream_reasons'])})" if report["stream_reasons"] else ""
    return f"Stream: {report['stream_status']}{reasons} | {channels}"
