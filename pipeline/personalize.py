"""Refit MI + blink classifiers on a single participant's collector data.

Runs the second mode of the final-realtime pipeline ("Train Personalization"):
take everything the collector wrote under data/sub<NN>/session_*/, slice MI
and blink epochs out of it, and fit a fresh sklearn pipeline that only ever
saw this participant. The resulting bundle is dropped into

    ml_pipeline/models/participants/sub<NN>.pkl

so eeg_to_meta/main.py --participant NN can pick it up at realtime.

Design notes:
    * Only the new collector schema (per-phase trial_log rows + named EEG
      columns TP9/AF7/AF8/TP10) is supported here. The legacy 2-class
      psychopy schema stays with ml_pipeline/dataset.py.
    * 4-class MI: spectral + Hjorth + asymmetry features (no CSP — CSP is
      a binary construct and would need OvR/OvO wrappers; we lean on
      RandomForest's nonlinearity instead, matching the user spec
      "RandomForest for MI, LDA for blink").
    * Blink: same feature set + LDA pipeline as the legacy trainer.
      Negative blink windows are sampled randomly inside MI trials so the
      classifier sees realistic non-blink EEG.
    * No PII fields are written to the bundle (`participant_id` is the
      integer NN; no name).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from . import labels as L


# ── Reuse ml_pipeline preprocessing/features without rewriting them.
# Those modules use bare `from config import ...` because they're meant to
# run with cwd=ml_pipeline. Inject the directory so they import cleanly.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ML_DIR = _PROJECT_ROOT / "ml_pipeline"
if str(_ML_DIR) not in sys.path:
    sys.path.insert(0, str(_ML_DIR))

import config as ml_config                                          # noqa: E402
from features import (                                              # noqa: E402
    extract_mi_features_batch,
    extract_blink_features_batch,
)
from preprocessing import (                                         # noqa: E402
    preprocess_blink_epoch,
    preprocess_mi_epoch,
    interpolate_nans,
    score_channel_quality_epoch,
)
from models import save_model_bundle                                # noqa: E402
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis  # noqa: E402
from sklearn.ensemble import RandomForestClassifier                 # noqa: E402
from sklearn.model_selection import StratifiedKFold, cross_val_score  # noqa: E402
from sklearn.pipeline import Pipeline                               # noqa: E402
from sklearn.preprocessing import StandardScaler                    # noqa: E402


log = logging.getLogger(__name__)

DEFAULT_DATA_ROOT = _PROJECT_ROOT / "data"
DEFAULT_OUTPUT_DIR = _ML_DIR / "models" / "participants"
PERSONALIZED_MODEL_VERSION = "personalized_v1"

# These channel names are written by pipeline.collector.
COLLECTOR_CH_COLS = ["TP9", "AF7", "AF8", "TP10"]


# ── Session discovery / loading ────────────────────────────────────


@dataclass
class SessionPaths:
    session_dir: Path
    eeg_csv: Path
    trial_log_csv: Path
    metadata_yaml: Optional[Path]


def find_sessions(participant: int, data_root: Path) -> list[SessionPaths]:
    """Return every session directory for one participant under data_root."""
    sub_dir = Path(data_root) / f"sub{participant:02d}"
    if not sub_dir.is_dir():
        return []
    sessions: list[SessionPaths] = []
    for session_dir in sorted(sub_dir.glob("session_*")):
        eeg = session_dir / "eeg_data.csv"
        log_csv = session_dir / "trial_log.csv"
        if not eeg.is_file() or not log_csv.is_file():
            continue
        meta = session_dir / "metadata.yaml"
        sessions.append(SessionPaths(
            session_dir=session_dir,
            eeg_csv=eeg,
            trial_log_csv=log_csv,
            metadata_yaml=meta if meta.is_file() else None,
        ))
    return sessions


def load_session(paths: SessionPaths) -> tuple[pd.DataFrame, pd.DataFrame, float]:
    """Read one collector session into (eeg_df, trials_df, estimated_sfreq).

    `trials_df` here keeps the original per-phase rows; epoch builders below
    aggregate phases on the fly.
    """
    eeg_df = pd.read_csv(paths.eeg_csv)
    trials_df = pd.read_csv(paths.trial_log_csv)
    for col in COLLECTOR_CH_COLS:
        if col in eeg_df.columns:
            eeg_df[col] = pd.to_numeric(eeg_df[col], errors="coerce")

    ts = eeg_df["timestamp"].values[:2000]
    dt = np.diff(ts)
    dt = dt[dt > 0]
    sfreq = float(1.0 / np.median(dt)) if len(dt) > 0 else float(ml_config.TARGET_SFREQ)
    return eeg_df, trials_df, sfreq


def _aggregate_trials(trials_df: pd.DataFrame) -> pd.DataFrame:
    """Collapse per-phase rows into one row per (run_index, trial_index, trial_type).

    Output columns: trial_type, run_index, trial_index, start_time, end_time,
    event_marker. Times are taken from `ts_recv`. Missing phases get sensible
    fallbacks so partially-completed trials are still usable.
    """
    if trials_df.empty:
        return pd.DataFrame(columns=[
            "trial_type", "run_index", "trial_index",
            "start_time", "end_time", "event_marker",
        ])

    rows = []
    grouped = trials_df.groupby(["run_index", "trial_index", "trial_type"], sort=True)
    for (run_idx, trial_idx, trial_type), group in grouped:
        phases = {
            row["phase"]: float(row["ts_recv"])
            for _, row in group.iterrows()
            if pd.notna(row.get("ts_recv"))
        }
        cue_t = phases.get("cue")
        start_t = phases.get("start", cue_t)
        end_t = phases.get("end")
        if start_t is None:
            continue  # no usable timing
        if end_t is None:
            # Use a default trial duration so MI epochs still extract.
            end_t = start_t + ml_config.MI_EPOCH_FIXED_S

        rows.append({
            "trial_type": trial_type,
            "run_index": int(run_idx),
            "trial_index": int(trial_idx),
            "start_time": start_t,
            "end_time": end_t,
            "event_marker": phases.get("start", cue_t or start_t),
        })
    return pd.DataFrame(rows)


# ── Epoch extraction ───────────────────────────────────────────────


def _segment(eeg_df: pd.DataFrame, t0: float, t1: float) -> np.ndarray:
    mask = (eeg_df["timestamp"] >= t0) & (eeg_df["timestamp"] <= t1)
    arr = eeg_df.loc[mask, COLLECTOR_CH_COLS].values.astype(np.float64)
    if arr.size == 0:
        return np.zeros((len(COLLECTOR_CH_COLS), 0))
    for c in range(arr.shape[1]):
        arr[:, c] = interpolate_nans(arr[:, c])
    return arr.T  # (n_channels, n_samples)


def _resample_if_needed(data_2d: np.ndarray, sfreq_orig: float) -> tuple[np.ndarray, float]:
    target = float(ml_config.TARGET_SFREQ)
    if data_2d.shape[1] < 4:
        return data_2d, target
    if abs(sfreq_orig - target) < 2:
        return data_2d, target
    import mne                                                      # noqa: PLC0415
    mne.set_log_level("ERROR")
    info = mne.create_info(
        ch_names=[f"ch{i}" for i in range(data_2d.shape[0])],
        sfreq=sfreq_orig, ch_types="eeg",
    )
    raw = mne.io.RawArray(data_2d, info, verbose=False)
    raw.resample(target, verbose=False)
    return raw.get_data(), target


def extract_mi_epochs_4class(
    eeg_df: pd.DataFrame,
    trials_df: pd.DataFrame,
    sfreq: float,
) -> tuple[list[np.ndarray], list[int], list[dict]]:
    """4-class MI epoch extraction (forward / backward / rotate-left / rotate-right)."""
    aggregated = _aggregate_trials(trials_df)
    mi = aggregated[aggregated["trial_type"].isin(ml_config.MI_TYPES_4CLASS)]
    if mi.empty:
        return [], [], []

    fixed_samples = int(ml_config.MI_EPOCH_FIXED_S * ml_config.TARGET_SFREQ)
    min_samples = int(ml_config.MI_EPOCH_MIN_S * sfreq)

    epochs: list[np.ndarray] = []
    labels: list[int] = []
    meta: list[dict] = []

    for _, trial in mi.iterrows():
        seg = _segment(eeg_df, trial["start_time"], trial["end_time"])
        if seg.shape[1] < min_samples:
            continue
        seg, actual_sfreq = _resample_if_needed(seg, sfreq)
        if seg.shape[1] > fixed_samples:
            seg = seg[:, :fixed_samples]
        elif seg.shape[1] < fixed_samples:
            pad = fixed_samples - seg.shape[1]
            seg = np.pad(seg, ((0, 0), (0, pad)), mode="edge")

        cleaned = preprocess_mi_epoch(seg, actual_sfreq)
        ch_quality = score_channel_quality_epoch(cleaned, actual_sfreq)
        if int(np.sum(ch_quality)) < max(2, len(COLLECTOR_CH_COLS) // 2):
            continue
        good = cleaned[ch_quality]
        ptp = float(np.ptp(good, axis=1).max())
        if ptp > ml_config.ARTIFACT_PTP_MI:
            continue
        if np.any(np.std(cleaned[ch_quality], axis=1) < ml_config.FLAT_THRESHOLD_UV):
            continue

        epochs.append(cleaned)
        labels.append(ml_config.LABEL_MAP_4CLASS[trial["trial_type"]])
        meta.append({
            "trial_type": trial["trial_type"],
            "run_index": int(trial["run_index"]),
            "trial_index": int(trial["trial_index"]),
            "ptp_uv": ptp,
        })

    return epochs, labels, meta


def extract_blink_pos_epochs(
    eeg_df: pd.DataFrame,
    trials_df: pd.DataFrame,
    sfreq: float,
    window_s: float = ml_config.BLINK_EPOCH_S,
) -> list[np.ndarray]:
    """Positive (blink) epochs centred on each `intentional_blink` event marker."""
    aggregated = _aggregate_trials(trials_df)
    blinks = aggregated[aggregated["trial_type"] == L.TRIAL_BLINK]
    if blinks.empty:
        return []

    target_samples = int(window_s * ml_config.TARGET_SFREQ)
    epochs: list[np.ndarray] = []

    for _, trial in blinks.iterrows():
        center = float(trial["event_marker"])
        seg = _segment(eeg_df, center - window_s / 2, center + window_s / 2)
        if seg.shape[1] < 10:
            continue
        seg, actual_sfreq = _resample_if_needed(seg, sfreq)
        if seg.shape[1] > target_samples:
            seg = seg[:, :target_samples]
        elif seg.shape[1] < target_samples:
            pad = target_samples - seg.shape[1]
            seg = np.pad(seg, ((0, 0), (0, pad)), mode="edge")
        epochs.append(preprocess_blink_epoch(seg, actual_sfreq))
    return epochs


def extract_blink_neg_epochs(
    eeg_df: pd.DataFrame,
    trials_df: pd.DataFrame,
    sfreq: float,
    window_s: float = ml_config.BLINK_EPOCH_S,
    rng: Optional[np.random.Generator] = None,
) -> list[np.ndarray]:
    """Sample non-blink windows from inside MI trials (no overlap)."""
    if rng is None:
        rng = np.random.default_rng(ml_config.RANDOM_STATE)
    aggregated = _aggregate_trials(trials_df)
    mi = aggregated[aggregated["trial_type"].isin(ml_config.MI_TYPES_4CLASS)]
    target_samples = int(window_s * ml_config.TARGET_SFREQ)

    epochs: list[np.ndarray] = []
    for _, trial in mi.iterrows():
        seg = _segment(eeg_df, trial["start_time"], trial["end_time"])
        if seg.shape[1] < target_samples + 10:
            continue
        seg, actual_sfreq = _resample_if_needed(seg, sfreq)
        if seg.shape[1] < target_samples + 10:
            continue
        max_start = seg.shape[1] - target_samples
        starts: list[int] = []
        attempts = 0
        while len(starts) < ml_config.BLINK_NEG_PER_MI and attempts < 50:
            s = int(rng.integers(0, max_start))
            if all(abs(s - ex) >= target_samples for ex in starts):
                starts.append(s)
            attempts += 1
        for s in starts:
            window = seg[:, s : s + target_samples]
            epochs.append(preprocess_blink_epoch(window, actual_sfreq))
    return epochs


# ── Cross-session aggregation ──────────────────────────────────────


@dataclass
class ParticipantData:
    mi_epochs: list[np.ndarray]
    mi_labels: list[int]
    mi_meta: list[dict]
    blink_pos: list[np.ndarray]
    blink_neg: list[np.ndarray]
    n_sessions: int
    n_runs_seen: int


def gather_participant_data(participant: int, data_root: Path) -> ParticipantData:
    sessions = find_sessions(participant, data_root)
    if not sessions:
        raise FileNotFoundError(
            f"No sessions found for participant {participant} under {data_root}. "
            f"Run `python -m pipeline.collector --participant {participant} ...` first."
        )

    mi_epochs: list[np.ndarray] = []
    mi_labels: list[int] = []
    mi_meta: list[dict] = []
    blink_pos: list[np.ndarray] = []
    blink_neg: list[np.ndarray] = []
    runs_seen: set[int] = set()

    for sp in sessions:
        eeg_df, trials_df, sfreq = load_session(sp)
        runs_seen.update(int(r) for r in trials_df["run_index"].unique() if not pd.isna(r))

        mi_e, mi_l, mi_m = extract_mi_epochs_4class(eeg_df, trials_df, sfreq)
        mi_epochs.extend(mi_e)
        mi_labels.extend(mi_l)
        mi_meta.extend(mi_m)

        blink_pos.extend(extract_blink_pos_epochs(eeg_df, trials_df, sfreq))
        blink_neg.extend(extract_blink_neg_epochs(eeg_df, trials_df, sfreq))

        log.info(
            "  %s: MI=%d, blink+=%d, blink-=%d (sfreq≈%.1f Hz)",
            sp.session_dir.name, len(mi_e),
            len(blink_pos), len(blink_neg), sfreq,
        )

    return ParticipantData(
        mi_epochs=mi_epochs,
        mi_labels=mi_labels,
        mi_meta=mi_meta,
        blink_pos=blink_pos,
        blink_neg=blink_neg,
        n_sessions=len(sessions),
        n_runs_seen=len(runs_seen),
    )


# ── Model fitting ──────────────────────────────────────────────────


def _build_mi_pipeline() -> Pipeline:
    """RandomForest pipeline for 4-class motor imagery (per user spec)."""
    return Pipeline([
        ("scaler", StandardScaler()),
        ("clf", RandomForestClassifier(
            n_estimators=200, max_depth=6, min_samples_leaf=2,
            random_state=ml_config.RANDOM_STATE,
        )),
    ])


def _build_blink_pipeline() -> Pipeline:
    """LDA pipeline for blink detection (per user spec)."""
    return Pipeline([
        ("scaler", StandardScaler()),
        ("clf", LinearDiscriminantAnalysis(solver="lsqr", shrinkage="auto")),
    ])


def _safe_cv_score(pipeline: Pipeline, X: np.ndarray, y: np.ndarray, *, k: int = 5) -> Optional[float]:
    """Stratified k-fold CV mean accuracy, or None if data is too small."""
    if X.shape[0] < k * 2 or len(np.unique(y)) < 2:
        return None
    counts = np.bincount(y)
    counts = counts[counts > 0]
    folds = max(2, min(k, int(counts.min())))
    cv = StratifiedKFold(n_splits=folds, shuffle=True, random_state=ml_config.RANDOM_STATE)
    try:
        scores = cross_val_score(pipeline, X, y, cv=cv, scoring="accuracy")
        return float(np.mean(scores))
    except Exception:                                               # noqa: BLE001
        log.exception("CV scoring failed")
        return None


@dataclass
class FitResult:
    mi_pipeline: Optional[Pipeline]
    blink_pipeline: Optional[Pipeline]
    mi_cv: Optional[float]
    blink_cv: Optional[float]
    mi_train_acc: Optional[float]
    blink_train_acc: Optional[float]
    n_mi: int
    n_blink_pos: int
    n_blink_neg: int
    mi_class_counts: dict[str, int]


def fit_personalized_models(
    data: ParticipantData, *, class_names=ml_config.MI_CLASS_NAMES_4CLASS,
) -> FitResult:
    mi_pipeline = None
    mi_cv = None
    mi_train_acc = None
    mi_class_counts: dict[str, int] = {}
    if data.mi_epochs:
        X_mi = extract_mi_features_batch(data.mi_epochs, ml_config.TARGET_SFREQ)
        y_mi = np.asarray(data.mi_labels)
        for cidx, cname in enumerate(class_names):
            mi_class_counts[cname] = int(np.sum(y_mi == cidx))
        if len(np.unique(y_mi)) >= 2 and X_mi.shape[0] >= 4:
            mi_pipeline = _build_mi_pipeline()
            mi_cv = _safe_cv_score(_build_mi_pipeline(), X_mi, y_mi)
            mi_pipeline.fit(X_mi, y_mi)
            mi_train_acc = float(np.mean(mi_pipeline.predict(X_mi) == y_mi))
        else:
            log.warning(
                "MI dataset too small for fitting: n=%d, classes=%s",
                X_mi.shape[0], np.unique(y_mi).tolist(),
            )

    blink_pipeline = None
    blink_cv = None
    blink_train_acc = None
    if data.blink_pos and data.blink_neg:
        all_eps = data.blink_pos + data.blink_neg
        all_labs = np.array([1] * len(data.blink_pos) + [0] * len(data.blink_neg))
        X_b = extract_blink_features_batch(all_eps, ml_config.TARGET_SFREQ)
        if len(np.unique(all_labs)) >= 2 and X_b.shape[0] >= 4:
            blink_pipeline = _build_blink_pipeline()
            blink_cv = _safe_cv_score(_build_blink_pipeline(), X_b, all_labs)
            blink_pipeline.fit(X_b, all_labs)
            blink_train_acc = float(np.mean(blink_pipeline.predict(X_b) == all_labs))
        else:
            log.warning(
                "Blink dataset too small for fitting: n=%d, classes=%s",
                X_b.shape[0], np.unique(all_labs).tolist(),
            )

    return FitResult(
        mi_pipeline=mi_pipeline,
        blink_pipeline=blink_pipeline,
        mi_cv=mi_cv,
        blink_cv=blink_cv,
        mi_train_acc=mi_train_acc,
        blink_train_acc=blink_train_acc,
        n_mi=len(data.mi_epochs),
        n_blink_pos=len(data.blink_pos),
        n_blink_neg=len(data.blink_neg),
        mi_class_counts=mi_class_counts,
    )


# ── Bundle save ────────────────────────────────────────────────────


def save_personalized_bundle(
    fit: FitResult,
    *,
    participant: int,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    """Save the personalised bundle under ml_pipeline/models/participants/."""
    output_dir = Path(output_dir)
    out_path = output_dir / f"sub{participant:02d}.pkl"
    return save_model_bundle(
        mi_pipeline=fit.mi_pipeline,
        mi_csp_W=None,
        blink_pipeline=fit.blink_pipeline,
        subjects_list=[],  # intentionally empty — no subject IDs / names in bundle
        class_names=list(ml_config.MI_CLASS_NAMES_4CLASS),
        blink_class_names=["non_blink", L.LABEL_BLINK],
        model_version=PERSONALIZED_MODEL_VERSION,
        model_kind="personalized",
        mi_label_map=dict(ml_config.LABEL_MAP_4CLASS),
        participant_id=int(participant),
        output_path=out_path,
        extra_info={
            "n_mi_epochs": fit.n_mi,
            "n_blink_pos": fit.n_blink_pos,
            "n_blink_neg": fit.n_blink_neg,
            "mi_class_counts": dict(fit.mi_class_counts),
            "cv_score_mi": fit.mi_cv,
            "cv_score_blink": fit.blink_cv,
            "train_acc_mi": fit.mi_train_acc,
            "train_acc_blink": fit.blink_train_acc,
        },
    )


# ── Public API + CLI ───────────────────────────────────────────────


def personalize(
    *,
    participant: int,
    data_root: Path = DEFAULT_DATA_ROOT,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    """End-to-end: load → fit → save. Returns the saved bundle path."""
    log.info("Gathering data for participant sub%02d under %s", participant, data_root)
    data = gather_participant_data(participant, data_root)
    log.info(
        "Loaded %d session(s); MI=%d, blink+=%d, blink-=%d, runs=%d",
        data.n_sessions, len(data.mi_epochs), len(data.blink_pos),
        len(data.blink_neg), data.n_runs_seen,
    )

    log.info("Fitting personalized models...")
    fit = fit_personalized_models(data)
    if fit.mi_pipeline is None and fit.blink_pipeline is None:
        raise RuntimeError(
            "Both MI and blink models failed to train — check session "
            "data integrity in {0}/sub{1:02d}/".format(data_root, participant)
        )

    out = save_personalized_bundle(fit, participant=participant, output_dir=output_dir)

    if fit.mi_pipeline is not None:
        log.info(
            "MI: %d epochs, classes=%s, train_acc=%.1f%%, cv_acc=%s",
            fit.n_mi, fit.mi_class_counts,
            (fit.mi_train_acc or 0) * 100,
            f"{(fit.mi_cv or 0) * 100:.1f}%" if fit.mi_cv is not None else "n/a",
        )
    if fit.blink_pipeline is not None:
        log.info(
            "Blink: %d+ / %d-, train_acc=%.1f%%, cv_acc=%s",
            fit.n_blink_pos, fit.n_blink_neg,
            (fit.blink_train_acc or 0) * 100,
            f"{(fit.blink_cv or 0) * 100:.1f}%" if fit.blink_cv is not None else "n/a",
        )
    log.info("Saved personalized bundle → %s", out)
    return out


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Refit MI+blink models on one participant.")
    p.add_argument("--participant", type=int, required=True,
                   help="Participant number, e.g. 5 (loads data/sub05/session_*/)")
    p.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT,
                   help=f"Root containing sub<NN>/ subdirs (default: {DEFAULT_DATA_ROOT})")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                   help=f"Where to write sub<NN>.pkl (default: {DEFAULT_OUTPUT_DIR})")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="DEBUG-level logging")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    t0 = time.time()
    try:
        out = personalize(
            participant=args.participant,
            data_root=args.data_root,
            output_dir=args.output_dir,
        )
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 2
    except RuntimeError as exc:
        log.error("Personalization failed: %s", exc)
        return 3
    log.info("Done in %.1fs → %s", time.time() - t0, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
