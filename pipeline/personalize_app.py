"""Adapt macOS/PsychoPy left/right recordings to the personalization trainer.

The four-direction collector keeps its separate schema and bundle. App bundles
use a distinct filename so the two label sets cannot be accidentally exchanged.
"""

from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd

from .personalize import ParticipantData, fit_personalized_models, ml_config
from dataset import (  # personalize sets up the existing ML module imports
    load_session, extract_mi_epochs, extract_blink_epochs, extract_nonblink_epochs,
)
from models import save_model_bundle

CLASS_NAMES = ["left_motor_imagery", "right_motor_imagery"]


def find_app_sessions(participant: int, data_root: Path) -> list[dict]:
    """Match EEG and trial files by timestamp; prefer new data over archived copies."""
    subject = f"sub{participant:02d}"
    folders = [data_root / subject, *sorted((data_root / "Muse_Data").glob(f"*/{subject}"))]
    sessions = {}
    for folder in folders:
        for eeg in sorted(folder.glob("eeg_data_*.csv")):
            stamp = eeg.stem.removeprefix("eeg_data_")
            trials = folder / f"trial_log_{stamp}.csv"
            if trials.is_file() and stamp not in sessions:
                sessions[stamp] = {"eeg_path": eeg, "trial_path": trials}
    return list(sessions.values())


def gather_app_data(participant: int, data_root: Path) -> ParticipantData:
    sessions = find_app_sessions(participant, data_root)
    if not sessions:
        raise ValueError("No paired EEG and trial recordings were found. Collect and save a session first.")
    data = ParticipantData([], [], [], [], [], len(sessions), 0)
    recorded = dict.fromkeys([*ml_config.MI_TYPES, ml_config.BLINK_TYPE], 0)
    short_mi = 0
    incomplete = 0
    rng = np.random.default_rng(ml_config.RANDOM_STATE)
    for session in sessions:
        eeg, trials, sfreq = load_session(session)
        # SessionRecorder interleaves marker-only rows with the buffered EEG.
        # They are annotations, not samples, and may arrive out of timestamp order.
        marker_only = eeg[ml_config.CH_COLS].isna().all(axis=1) & eeg["marker_code"].notna()
        eeg = eeg.loc[~marker_only].copy()
        required = {"trial_type", "start_time", "end_time", "event_marker"}
        if not required.issubset(trials.columns):
            raise ValueError("A trial log does not use the app's left/right recording format.")
        times = eeg["timestamp"].to_numpy(dtype=float)
        if len(times) < 2 or not np.isfinite(times).all() or np.any(np.diff(times) <= 0):
            raise ValueError("A recording has missing or unordered EEG timestamps.")
        sfreq = float(1.0 / np.median(np.diff(times[:2000])))
        trials = trials.copy()
        for column in ("start_time", "end_time", "event_marker"):
            trials[column] = pd.to_numeric(trials[column], errors="coerce")
        for kind in recorded:
            recorded[kind] += int((trials["trial_type"] == kind).sum())
        # Recorder blink rows span ±0.5 s, but the inference window is only 0.5 s.
        # Center that window on the keypress instead of truncating before the blink.
        blinks = trials["trial_type"] == ml_config.BLINK_TYPE
        half = ml_config.BLINK_EPOCH_S / 2
        trials.loc[blinks, "start_time"] = trials.loc[blinks, "event_marker"] - half
        trials.loc[blinks, "end_time"] = trials.loc[blinks, "event_marker"] + half
        covered = (
            (trials["start_time"] >= times[0]) & (trials["end_time"] <= times[-1])
            & (trials["end_time"] > trials["start_time"])
        )
        incomplete += int((~covered & trials["trial_type"].isin(recorded)).sum())
        trials = trials[covered]
        mi_trials = trials[trials["trial_type"].isin(ml_config.MI_TYPES)]
        sample_counts = (
            np.searchsorted(times, mi_trials["end_time"], side="right")
            - np.searchsorted(times, mi_trials["start_time"], side="left")
        )
        too_short = int((sample_counts < int(ml_config.MI_EPOCH_MIN_S * sfreq)).sum())
        short_mi += too_short
        # The live engine does not run ASR, even when asrpy is installed.
        epochs, labels, meta = extract_mi_epochs(
            eeg, trials, sfreq, use_asr=False, reject_signal_quality=False,
        )
        data.mi_epochs.extend(epochs)
        data.mi_labels.extend(labels)
        data.mi_meta.extend(meta)
        positives, _ = extract_blink_epochs(eeg, trials, sfreq)
        data.blink_pos.extend(positives)
        data.blink_neg.extend(extract_nonblink_epochs(eeg, trials, sfreq, rng=rng))
    counts = [data.mi_labels.count(0), data.mi_labels.count(1), len(data.blink_pos), len(data.blink_neg)]
    if min(counts) < 2:
        raise ValueError(
            f"Saved recordings found: left: {recorded[ml_config.MI_TYPES[0]]}, "
            f"right: {recorded[ml_config.MI_TYPES[1]]}, blink: {recorded[ml_config.BLINK_TYPE]}. "
            "Usable examples "
            f"(left: {counts[0]}, right: {counts[1]}, blink: {counts[2]}, non-blink: {counts[3]}). "
            f"Excluded: {short_mi} hand trials shorter than two seconds of EEG, "
            f"{incomplete} trials without full EEG coverage. "
            "At least two usable examples of each are needed. Collect again; "
            "keep imagining until the countdown finishes before pressing Space."
        )
    return data


def personalize_app(*, participant: int, data_root: Path, output_dir: Path) -> tuple[Path, dict]:
    if participant < 1:
        raise ValueError("Participant number must be positive.")
    data = gather_app_data(participant, Path(data_root))
    fit = fit_personalized_models(data, class_names=CLASS_NAMES)
    if fit.mi_pipeline is None or fit.blink_pipeline is None:
        raise ValueError("Both hand and blink models must train successfully. Collect another session.")
    summary = {
        "n_sessions": data.n_sessions,
        "n_mi_epochs": fit.n_mi,
        "n_blink_pos": fit.n_blink_pos,
        "n_blink_neg": fit.n_blink_neg,
        "mi_class_counts": fit.mi_class_counts,
        "cv_score_mi": fit.mi_cv,
        "cv_score_blink": fit.blink_cv,
    }
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"sub{participant:02d}_app.pkl"
    # A failed or cancelled retrain must never truncate the last working model.
    with TemporaryDirectory(prefix=".training-", dir=output_dir) as temporary:
        saved = save_model_bundle(
            mi_pipeline=fit.mi_pipeline, mi_csp_W=None, blink_pipeline=fit.blink_pipeline,
            subjects_list=[], class_names=CLASS_NAMES, model_version="personalized_app_v1",
            model_kind="personalized", participant_id=participant,
            mi_label_map={key: ml_config.LABEL_MAP[key] for key in ml_config.MI_TYPES},
            output_path=Path(temporary) / "model.pkl", extra_info=summary,
        )
        saved.replace(target)
    return target, summary
