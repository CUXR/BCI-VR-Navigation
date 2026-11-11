"""Adapt macOS/PsychoPy recordings to the personalization trainer.

Left/right hand imagery is always trained. When a participant has recorded the
forward-walking block, the model also learns `mi_forward` (walking imagery) and
`idle` (the paired Relax trials). Forward trials only count when the session's
imagery check marked that block `use_for_training`.

The four-direction collector keeps its separate schema and bundle. App bundles
use a distinct filename so the two label sets cannot be accidentally exchanged.
"""

import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd

from .personalize import ParticipantData, fit_personalized_models, ml_config
from dataset import (  # personalize sets up the existing ML module imports
    load_session, extract_mi_epochs, extract_blink_epochs, extract_nonblink_epochs,
)
from models import save_model_bundle

log = logging.getLogger(__name__)

CLASS_NAMES = ["left_motor_imagery", "right_motor_imagery"]
FORWARD_TYPE, RELAX_TYPE = "motor_imagery_forward", "baseline_quiet"
# Optional classes, in label order after left/right. `mi_forward` and `idle` are
# the names the real-time engine, VR socket, and app already map to actions.
OPTIONAL_CLASSES = [(FORWARD_TYPE, "mi_forward"), (RELAX_TYPE, "idle")]
ALL_TRIAL_TYPES = {**dict.fromkeys(ml_config.MI_TYPES), FORWARD_TYPE: None, RELAX_TYPE: None}


def approved_forward_blocks(trial_path: Path) -> set[int]:
    """Forward blocks whose latest post-block check says `use_for_training`."""
    stamp = trial_path.stem.removeprefix("trial_log_")
    check = trial_path.with_name(f"imagery_check_{stamp}.json")
    try:
        reports = json.loads(check.read_text()).get("reports", [])
    except (OSError, ValueError, AttributeError):
        return set()
    verdicts = {}
    for report in reports:
        if isinstance(report, dict) and report.get("kind") == "block_check" and "block" in report:
            try:
                verdicts[int(report["block"])] = report.get("use_for_training") is True
            except (TypeError, ValueError):
                continue
    return {block for block, ok in verdicts.items() if ok}


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
    recorded = dict.fromkeys([*ALL_TRIAL_TYPES, ml_config.BLINK_TYPE], 0)
    short_mi = 0
    incomplete = 0
    unchecked_forward = 0
    # Extract every imagery type with a provisional label, then drop optional
    # classes with fewer than two usable examples and renumber contiguously.
    provisional = {t: i for i, t in enumerate(ALL_TRIAL_TYPES)}
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
        forward = trials["trial_type"] == FORWARD_TYPE
        if forward.any():
            approved = approved_forward_blocks(Path(session["trial_path"]))
            if "block_number" in trials.columns:
                blocks = pd.to_numeric(trials["block_number"], errors="coerce")
                rejected = forward & ~blocks.isin(approved)
            else:
                rejected = forward
            unchecked_forward += int(rejected.sum())
            trials = trials[~rejected]
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
        mi_trials = trials[trials["trial_type"].isin(provisional)]
        sample_counts = (
            np.searchsorted(times, mi_trials["end_time"], side="right")
            - np.searchsorted(times, mi_trials["start_time"], side="left")
        )
        too_short = int((sample_counts < int(ml_config.MI_EPOCH_MIN_S * sfreq)).sum())
        short_mi += too_short
        # The live engine does not run ASR, even when asrpy is installed.
        epochs, labels, meta = extract_mi_epochs(
            eeg, trials, sfreq, use_asr=False, reject_signal_quality=False, label_map=provisional,
        )
        data.mi_epochs.extend(epochs)
        data.mi_labels.extend(labels)
        data.mi_meta.extend(meta)
        positives, _ = extract_blink_epochs(eeg, trials, sfreq)
        data.blink_pos.extend(positives)
        data.blink_neg.extend(extract_nonblink_epochs(eeg, trials, sfreq, rng=rng))
    counts = [data.mi_labels.count(0), data.mi_labels.count(1), len(data.blink_pos), len(data.blink_neg)]
    usable_walking = data.mi_labels.count(provisional[FORWARD_TYPE])
    usable_relax = data.mi_labels.count(provisional[RELAX_TYPE])
    class_names = list(CLASS_NAMES)
    renumber = {0: 0, 1: 1}
    label_map = {t: provisional[t] for t in ml_config.MI_TYPES}
    for trial_type, name in OPTIONAL_CLASSES:
        if data.mi_labels.count(provisional[trial_type]) >= 2:
            renumber[provisional[trial_type]] = label_map[trial_type] = len(class_names)
            class_names.append(name)
    keep = [i for i, label in enumerate(data.mi_labels) if label in renumber]
    data.mi_epochs[:] = [data.mi_epochs[i] for i in keep]
    data.mi_meta[:] = [data.mi_meta[i] for i in keep]
    data.mi_labels[:] = [renumber[data.mi_labels[i]] for i in keep]
    data.class_names = class_names
    data.mi_label_map = label_map
    data.forward_report = {
        "forward_recorded": recorded[FORWARD_TYPE],
        "relax_recorded": recorded[RELAX_TYPE],
        "forward_excluded_by_check": unchecked_forward,
    }
    if recorded[FORWARD_TYPE] and "mi_forward" not in class_names:
        log.warning(
            "Forward walking not trained: %d recorded, %d excluded by the imagery check, "
            "fewer than two usable trials left.", recorded[FORWARD_TYPE], unchecked_forward,
        )
    if min(counts) < 2:
        raise ValueError(
            f"Saved recordings found: left: {recorded[ml_config.MI_TYPES[0]]}, "
            f"right: {recorded[ml_config.MI_TYPES[1]]}, blink: {recorded[ml_config.BLINK_TYPE]}, "
            f"walking: {recorded[FORWARD_TYPE]}, relax: {recorded[RELAX_TYPE]}. "
            "Usable examples "
            f"(left: {counts[0]}, right: {counts[1]}, blink: {counts[2]}, non-blink: {counts[3]}, "
            f"walking: {usable_walking}, relax: {usable_relax}). "
            f"Excluded: {short_mi} imagery trials shorter than two seconds of EEG, "
            f"{incomplete} trials without full EEG coverage, "
            f"{unchecked_forward} walking trials not approved by the imagery check. "
            "At least two usable examples of left, right, blink and non-blink are needed; "
            "walking is optional and trains once it has two. Collect again; "
            "keep imagining until the countdown finishes before pressing Space."
        )
    return data


def personalize_app(*, participant: int, data_root: Path, output_dir: Path) -> tuple[Path, dict]:
    if participant < 1:
        raise ValueError("Participant number must be positive.")
    data = gather_app_data(participant, Path(data_root))
    fit = fit_personalized_models(data, class_names=data.class_names)
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
        "class_names": data.class_names,
        **data.forward_report,
    }
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"sub{participant:02d}_app.pkl"
    # A failed or cancelled retrain must never truncate the last working model.
    with TemporaryDirectory(prefix=".training-", dir=output_dir) as temporary:
        saved = save_model_bundle(
            mi_pipeline=fit.mi_pipeline, mi_csp_W=None, blink_pipeline=fit.blink_pipeline,
            subjects_list=[], class_names=data.class_names, model_version="personalized_app_v1",
            model_kind="personalized", participant_id=participant,
            mi_label_map=data.mi_label_map,
            output_path=Path(temporary) / "model.pkl", extra_info=summary,
        )
        saved.replace(target)
    return target, summary
