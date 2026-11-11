"""Data loading, epoch extraction, and dataset management.

Discovers subject data from multiple directories, extracts epochs
with class-specific preprocessing, and builds train/test splits.
"""

import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from config import (
    DATA_DIRS, CH_COLS, CH_NAMES, TARGET_SFREQ,
    MI_TYPES, BLINK_TYPE, LABEL_MAP,
    MI_EPOCH_MIN_S, MI_EPOCH_FIXED_S, BLINK_EPOCH_S, BLINK_NEG_PER_MI,
    ARTIFACT_PTP_MI, ARTIFACT_PTP_BLINK, FLAT_THRESHOLD_UV,
    CHANNEL_BAD_FRACTION,
)
from preprocessing import (
    preprocess_mi_epoch, preprocess_blink_epoch,
    score_channel_quality, score_channel_quality_epoch,
    interpolate_nans,
)


# ═══════════════════════════════════════════════════════════════════
# Subject discovery
# ═══════════════════════════════════════════════════════════════════

def find_all_subjects():
    """Scan DATA_DIRS for subject folders containing EEG + trial data.

    Returns:
        dict {sub_id: [list of session dicts]}
        Each session dict has keys: eeg_path, trial_path, sub_dir, session_id
    """
    subjects = {}
    seen_sessions = set()  # (sub_id, session_id) to deduplicate across dirs

    for data_dir in DATA_DIRS:
        if not data_dir.exists():
            continue
        for sub_dir in sorted(data_dir.glob("sub*")):
            if not sub_dir.is_dir():
                continue
            eeg_files = sorted(sub_dir.glob("eeg_data_*.csv"))
            trial_files = sorted(sub_dir.glob("trial_log_*.csv"))

            if not eeg_files or not trial_files:
                continue

            sub_id = sub_dir.name
            if sub_id not in subjects:
                subjects[sub_id] = []

            for eeg_f, trial_f in zip(eeg_files, trial_files):
                session_id = eeg_f.stem.replace("eeg_data_", "")
                key = (sub_id, session_id)
                if key in seen_sessions:
                    continue
                seen_sessions.add(key)

                subjects[sub_id].append({
                    "eeg_path": eeg_f,
                    "trial_path": trial_f,
                    "sub_dir": sub_dir,
                    "session_id": session_id,
                })

    return subjects


def load_session(session_info):
    """Load a single session's EEG and trial data.

    Returns:
        eeg_df: DataFrame with timestamp, EEG_0..EEG_3
        trials_df: DataFrame with trial_type, start_time, end_time, etc.
        sfreq: estimated sampling frequency
    """
    eeg_df = pd.read_csv(session_info["eeg_path"])
    trials_df = pd.read_csv(session_info["trial_path"])

    for col in CH_COLS:
        eeg_df[col] = pd.to_numeric(eeg_df[col], errors="coerce")

    ts = eeg_df["timestamp"].values[:2000]
    dt = np.diff(ts)
    dt = dt[dt > 0]
    sfreq = 1.0 / np.median(dt) if len(dt) > 0 else TARGET_SFREQ

    return eeg_df, trials_df, sfreq


# ═══════════════════════════════════════════════════════════════════
# Epoch extraction helpers
# ═══════════════════════════════════════════════════════════════════

def _extract_segment(eeg_df, start_time, end_time, ch_indices=None):
    """Extract a time-bounded segment as (n_channels, n_samples) array."""
    if ch_indices is None:
        ch_indices = list(range(len(CH_COLS)))

    mask = (eeg_df["timestamp"] >= start_time) & (eeg_df["timestamp"] <= end_time)
    cols = [CH_COLS[i] for i in ch_indices]
    seg = eeg_df.loc[mask, cols].values.astype(np.float64)

    for i in range(seg.shape[1]):
        seg[:, i] = interpolate_nans(seg[:, i])

    return seg.T  # (n_channels, n_samples)


def _resample_if_needed(data_2d, sfreq_orig, sfreq_target=None):
    """Resample to target frequency if significantly different.

    Returns (None, sfreq_target) if the segment is too short to resample.
    """
    sfreq_target = sfreq_target or TARGET_SFREQ

    MIN_SAMPLES = 4
    if data_2d.shape[1] < MIN_SAMPLES:
        return None, sfreq_target

    if abs(sfreq_orig - sfreq_target) < 2:
        return data_2d, sfreq_target

    import mne
    mne.set_log_level("ERROR")
    n_ch = data_2d.shape[0]
    info = mne.create_info(
        ch_names=[f"ch{i}" for i in range(n_ch)],
        sfreq=sfreq_orig, ch_types="eeg",
    )
    raw = mne.io.RawArray(data_2d, info, verbose=False)
    raw.resample(sfreq_target, verbose=False)
    return raw.get_data(), sfreq_target


# ═══════════════════════════════════════════════════════════════════
# Motor imagery epoch extraction
# ═══════════════════════════════════════════════════════════════════

def extract_mi_epochs(eeg_df, trials_df, sfreq, ch_indices=None, *, use_asr=True,
                      reject_signal_quality=True, label_map=None):
    """Extract and preprocess motor imagery epochs.

    Args:
        eeg_df: raw EEG DataFrame
        trials_df: trial log DataFrame
        sfreq: sampling frequency
        ch_indices: which channels to use (default: all 4)
        reject_signal_quality: reject noisy/flat epochs (disabled for app personal training)
        label_map: trial_type -> int label for the trials to extract (default: left/right)

    Returns:
        epochs: list of (n_channels, n_samples) arrays — fixed length
        labels: list of int (0=left, 1=right)
        meta: list of dicts with trial_id, quality info
    """
    if ch_indices is None:
        ch_indices = list(range(len(CH_COLS)))

    if label_map is None:
        label_map = {t: LABEL_MAP[t] for t in MI_TYPES}
    mi_trials = trials_df[trials_df["trial_type"].isin(label_map)]
    fixed_samples = int(MI_EPOCH_FIXED_S * TARGET_SFREQ)
    min_samples = int(MI_EPOCH_MIN_S * sfreq)

    epochs = []
    labels = []
    meta = []

    for _, trial in mi_trials.iterrows():
        raw_seg = _extract_segment(eeg_df, trial["start_time"], trial["end_time"], ch_indices)

        if raw_seg.shape[1] < min_samples:
            continue

        raw_seg, actual_sfreq = _resample_if_needed(raw_seg, sfreq)
        if raw_seg is None:
            continue

        # Truncate/pad to fixed length
        if raw_seg.shape[1] > fixed_samples:
            raw_seg = raw_seg[:, :fixed_samples]
        elif raw_seg.shape[1] < fixed_samples:
            pad_width = fixed_samples - raw_seg.shape[1]
            raw_seg = np.pad(raw_seg, ((0, 0), (0, pad_width)), mode="edge")

        cleaned = preprocess_mi_epoch(raw_seg, actual_sfreq, use_asr=use_asr)

        # Per-epoch quality gate
        ch_quality = score_channel_quality_epoch(cleaned, actual_sfreq)
        n_good = np.sum(ch_quality)
        if reject_signal_quality and n_good < max(2, len(ch_indices) // 2):
            continue

        # PTP rejection on good channels only
        good_data = cleaned[ch_quality] if n_good else cleaned
        ptp = np.ptp(good_data, axis=1).max()
        if reject_signal_quality and ptp > ARTIFACT_PTP_MI:
            continue

        # Flat channel check
        stds = np.std(cleaned, axis=1)
        if reject_signal_quality and np.any(stds[ch_quality] < FLAT_THRESHOLD_UV):
            continue

        label = label_map[trial["trial_type"]]
        epochs.append(cleaned)
        labels.append(label)
        meta.append({
            "trial_id": trial.get("trial_id", ""),
            "trial_type": trial["trial_type"],
            "channel_quality": ch_quality.tolist(),
            "ptp_uv": float(ptp),
        })

    return epochs, labels, meta


# ═══════════════════════════════════════════════════════════════════
# Blink epoch extraction
# ═══════════════════════════════════════════════════════════════════

def extract_blink_epochs(eeg_df, trials_df, sfreq, ch_indices=None):
    """Extract and preprocess intentional blink epochs.

    Returns:
        epochs: list of (n_channels, n_samples) arrays
        meta: list of dicts
    """
    if ch_indices is None:
        ch_indices = list(range(len(CH_COLS)))

    blink_trials = trials_df[trials_df["trial_type"] == BLINK_TYPE]
    target_samples = int(BLINK_EPOCH_S * TARGET_SFREQ)

    epochs = []
    meta = []

    for _, trial in blink_trials.iterrows():
        raw_seg = _extract_segment(eeg_df, trial["start_time"], trial["end_time"], ch_indices)

        if raw_seg.shape[1] < 10:
            continue

        raw_seg, actual_sfreq = _resample_if_needed(raw_seg, sfreq)
        if raw_seg is None:
            continue

        # Pad/truncate to target length
        if raw_seg.shape[1] > target_samples:
            raw_seg = raw_seg[:, :target_samples]
        elif raw_seg.shape[1] < target_samples:
            pad_width = target_samples - raw_seg.shape[1]
            raw_seg = np.pad(raw_seg, ((0, 0), (0, pad_width)), mode="edge")

        cleaned = preprocess_blink_epoch(raw_seg, actual_sfreq)
        epochs.append(cleaned)
        meta.append({"trial_id": trial.get("trial_id", "")})

    return epochs, meta


def extract_nonblink_epochs(eeg_df, trials_df, sfreq, ch_indices=None, rng=None):
    """Sample negative (non-blink) epochs from MI trial periods.

    Returns:
        epochs: list of (n_channels, n_samples) arrays
    """
    if ch_indices is None:
        ch_indices = list(range(len(CH_COLS)))
    if rng is None:
        rng = np.random.default_rng(42)

    mi_trials = trials_df[trials_df["trial_type"].isin(MI_TYPES)]
    target_samples = int(BLINK_EPOCH_S * TARGET_SFREQ)

    epochs = []

    for _, trial in mi_trials.iterrows():
        raw_seg = _extract_segment(eeg_df, trial["start_time"], trial["end_time"], ch_indices)
        raw_seg, actual_sfreq = _resample_if_needed(raw_seg, sfreq)
        if raw_seg is None:
            continue

        if raw_seg.shape[1] < target_samples + 10:
            continue

        max_start = raw_seg.shape[1] - target_samples
        starts = []
        attempts = 0
        while len(starts) < BLINK_NEG_PER_MI and attempts < 50:
            s = rng.integers(0, max_start)
            if all(abs(s - ex) >= target_samples for ex in starts):
                starts.append(s)
            attempts += 1

        for s in starts:
            window = raw_seg[:, s : s + target_samples]
            cleaned = preprocess_blink_epoch(window, actual_sfreq)
            epochs.append(cleaned)

    return epochs


# ═══════════════════════════════════════════════════════════════════
# Full dataset builder
# ═══════════════════════════════════════════════════════════════════

def build_dataset(verbose=True):
    """Load all subjects and extract all epochs.

    Returns:
        dataset: dict with keys:
            mi_epochs, mi_labels, mi_subjects — motor imagery data
            blink_pos_epochs, blink_neg_epochs, blink_subjects — blink data
            subjects_info — per-subject quality summary
    """
    subjects = find_all_subjects()
    if not subjects and verbose:
        print("No subject data found in:", [str(d) for d in DATA_DIRS])
        return None

    if verbose:
        print(f"Found {len(subjects)} subjects: {list(subjects.keys())}")

    mi_epochs_all = []
    mi_labels_all = []
    mi_subjects_all = []

    blink_pos_all = []
    blink_pos_subjects = []
    blink_neg_all = []
    blink_neg_subjects = []

    subjects_info = {}

    for sub_id, sessions in subjects.items():
        sub_mi_epochs = []
        sub_mi_labels = []
        sub_blink_pos = []
        sub_blink_neg = []

        for sess in sessions:
            eeg_df, trials_df, sfreq = load_session(sess)

            # Session-level channel quality
            all_ch_data = eeg_df[CH_COLS].values.T.astype(np.float64)
            for i in range(all_ch_data.shape[0]):
                all_ch_data[i] = interpolate_nans(all_ch_data[i])
            _, ch_quality = score_channel_quality(all_ch_data, sfreq)

            # MI epochs
            epochs, labels, ep_meta = extract_mi_epochs(eeg_df, trials_df, sfreq)
            sub_mi_epochs.extend(epochs)
            sub_mi_labels.extend(labels)

            # Blink epochs
            blink_eps, _ = extract_blink_epochs(eeg_df, trials_df, sfreq)
            nonblink_eps = extract_nonblink_epochs(eeg_df, trials_df, sfreq)
            sub_blink_pos.extend(blink_eps)
            sub_blink_neg.extend(nonblink_eps)

            if verbose:
                n_left = sum(1 for l in labels if l == 0)
                n_right = sum(1 for l in labels if l == 1)
                bad_chs = [n for n, q in ch_quality.items() if not q["is_good"]]
                quality_str = f" [bad: {', '.join(bad_chs)}]" if bad_chs else " [all clean]"
                print(f"  {sub_id}/{sess['session_id']}: "
                      f"MI={len(epochs)} (L={n_left}, R={n_right}), "
                      f"Blink={len(blink_eps)}+/{len(nonblink_eps)}-"
                      f"{quality_str}")

        mi_epochs_all.extend(sub_mi_epochs)
        mi_labels_all.extend(sub_mi_labels)
        mi_subjects_all.extend([sub_id] * len(sub_mi_epochs))

        blink_pos_all.extend(sub_blink_pos)
        blink_pos_subjects.extend([sub_id] * len(sub_blink_pos))
        blink_neg_all.extend(sub_blink_neg)
        blink_neg_subjects.extend([sub_id] * len(sub_blink_neg))

        subjects_info[sub_id] = {
            "n_sessions": len(sessions),
            "n_mi_epochs": len(sub_mi_epochs),
            "n_blink_pos": len(sub_blink_pos),
            "n_blink_neg": len(sub_blink_neg),
        }

    # Combine blink subjects in same order as pos+neg epoch concatenation
    blink_subjects_all = blink_pos_subjects + blink_neg_subjects

    return {
        "mi_epochs": mi_epochs_all,
        "mi_labels": mi_labels_all,
        "mi_subjects": mi_subjects_all,
        "blink_pos_epochs": blink_pos_all,
        "blink_neg_epochs": blink_neg_all,
        "blink_subjects": blink_subjects_all,
        "subjects_info": subjects_info,
    }
