"""Refit MI + blink classifiers on a single user's calibration data.

Runs the "Train Personalization" step of the pipeline: take every session
recorded for one user (by `pipeline.collector` from Unity, or by the app
backend's calibration engine), clean it, slice MI and blink epochs out of
it, and fit fresh sklearn pipelines that only ever saw this user.

Two ways to point it at data:

    --participant NN     data/sub<NN>/session_*/   (Unity collector layout)
    --sessions-dir DIR   DIR/session_*/            (app backend layout)

The MI class set is either given explicitly (`--classes left,right,blink`)
or detected from the trial types present in the data, so the same code
handles 2-class left/right and 4-class navigation recordings.

Design notes:
    * Features: spectral + Hjorth + asymmetry (no CSP — CSP is a binary
      construct; RandomForest handles the multiclass case). These are the
      same 78 MI / 75 blink features eeg_to_meta/inference.py computes live.
    * MI epochs can be cut as several overlapping 2 s windows per trial
      (`--mi-stride`), which mirrors the realtime sliding window and
      multiplies the training data. CV then groups windows by trial so
      windows of one trial never land in both train and test folds.
    * Blink positives are centred on the frontal (AF7/AF8) peak inside the
      blink trial window when the trial has a usable start/end span, else
      on the event marker. Negatives come from MI and rest trials.
    * Every rejected epoch is counted by reason and returned in the report
      so a frontend can tell the user what to redo.
    * No PII is written to the bundle (no names, no user ids).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
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
from sklearn.model_selection import (                               # noqa: E402
    StratifiedGroupKFold,
    StratifiedKFold,
    cross_val_predict,
)
from sklearn.pipeline import Pipeline                               # noqa: E402
from sklearn.preprocessing import StandardScaler                    # noqa: E402


log = logging.getLogger(__name__)

DEFAULT_DATA_ROOT = _PROJECT_ROOT / "data"
DEFAULT_OUTPUT_DIR = _ML_DIR / "models" / "participants"
PERSONALIZED_MODEL_VERSION = "personalized_v2"

# These channel names are written by pipeline.collector.
COLLECTOR_CH_COLS = ["TP9", "AF7", "AF8", "TP10"]
FRONTAL_IDX = [1, 2]  # AF7, AF8 within COLLECTOR_CH_COLS

# Readiness gate reported back to the app. MI chance is 1 / n_classes, so
# the MI bar is expressed as a margin above chance.
READY_MIN_MI_TRIALS_PER_CLASS = 8
READY_MIN_BLINK_TRIALS = 8
READY_MI_MARGIN_OVER_CHANCE = 0.10
READY_MIN_BLINK_CV = 0.80


# ── Options / bookkeeping ──────────────────────────────────────────


@dataclass
class EpochOptions:
    """How epochs are cut from trials.

    The defaults reproduce the original single-window behaviour; the app
    backend passes a stride/offset to get several windows per trial.
    """

    mi_window_s: float = ml_config.MI_EPOCH_FIXED_S
    mi_stride_s: Optional[float] = None   # None → one window per trial
    mi_offset_s: float = 0.0              # skip reaction time after "start"
    blink_window_s: float = ml_config.BLINK_EPOCH_S
    blink_peak_search: bool = True


@dataclass
class RejectionStats:
    """Counts of MI windows dropped during cleaning, by reason."""

    too_short: int = 0
    bad_channels: int = 0
    amplitude: int = 0
    flat: int = 0

    def total(self) -> int:
        return self.too_short + self.bad_channels + self.amplitude + self.flat


@dataclass
class SessionPaths:
    session_dir: Path
    eeg_csv: Path
    trial_log_csv: Path
    metadata_yaml: Optional[Path]


# ── Session discovery / loading ────────────────────────────────────


def find_sessions_in(sessions_dir: Path) -> list[SessionPaths]:
    """Return every complete session_*/ directory directly under sessions_dir."""
    sessions_dir = Path(sessions_dir)
    if not sessions_dir.is_dir():
        return []
    sessions: list[SessionPaths] = []
    for session_dir in sorted(sessions_dir.glob("session_*")):
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


def find_sessions(participant: int, data_root: Path) -> list[SessionPaths]:
    """Return every session directory for one participant under data_root."""
    return find_sessions_in(Path(data_root) / f"sub{participant:02d}")


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
    event_marker, has_end. Times are taken from `ts_recv`. Missing phases get
    sensible fallbacks so partially-completed trials are still usable.
    """
    if trials_df.empty:
        return pd.DataFrame(columns=[
            "trial_type", "run_index", "trial_index",
            "start_time", "end_time", "event_marker", "has_end",
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
        has_end = end_t is not None
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
            "has_end": has_end,
        })
    return pd.DataFrame(rows)


def detect_mi_trial_types(trial_types: set[str]) -> list[str]:
    """MI trial types present in the data, in canonical class order."""
    return [t for t in L.ALL_MI_TRIAL_TYPES if t in trial_types]


def mi_trial_types_from_classes(classes: list[str]) -> tuple[list[str], bool]:
    """Map app class names (e.g. ["left", "right", "blink"]) to MI trial types.

    Returns (mi_trial_types, include_blink).
    """
    unknown = [c for c in classes if c not in L.APP_CLASS_TO_TRIAL_TYPE]
    if unknown:
        raise ValueError(
            f"Unknown class(es) {unknown}; expected any of "
            f"{sorted(L.APP_CLASS_TO_TRIAL_TYPE)}"
        )
    mi_types = [
        L.APP_CLASS_TO_TRIAL_TYPE[c] for c in L.APP_MI_CLASSES if c in classes
    ]
    return mi_types, L.CLASS_BLINK in classes


# ── Epoch extraction ───────────────────────────────────────────────


def _segment_with_ts(eeg_df: pd.DataFrame, t0: float, t1: float) -> tuple[np.ndarray, np.ndarray]:
    mask = (eeg_df["timestamp"] >= t0) & (eeg_df["timestamp"] <= t1)
    arr = eeg_df.loc[mask, COLLECTOR_CH_COLS].values.astype(np.float64)
    ts = eeg_df.loc[mask, "timestamp"].values.astype(np.float64)
    if arr.size == 0:
        return np.zeros((len(COLLECTOR_CH_COLS), 0)), ts
    for c in range(arr.shape[1]):
        arr[:, c] = interpolate_nans(arr[:, c])
    return arr.T, ts  # (n_channels, n_samples), (n_samples,)


def _segment(eeg_df: pd.DataFrame, t0: float, t1: float) -> np.ndarray:
    return _segment_with_ts(eeg_df, t0, t1)[0]


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


def _fit_length(seg: np.ndarray, n: int) -> np.ndarray:
    if seg.shape[1] > n:
        return seg[:, :n]
    if seg.shape[1] < n:
        return np.pad(seg, ((0, 0), (0, n - seg.shape[1])), mode="edge")
    return seg


def _clean_mi_window(raw: np.ndarray, sfreq: float, stats: RejectionStats) -> Optional[tuple[np.ndarray, float]]:
    """Preprocess one MI window; return (cleaned, ptp) or None if rejected."""
    cleaned = preprocess_mi_epoch(raw, sfreq)
    ch_quality = score_channel_quality_epoch(cleaned, sfreq)
    if int(np.sum(ch_quality)) < max(2, len(COLLECTOR_CH_COLS) // 2):
        stats.bad_channels += 1
        return None
    good = cleaned[ch_quality]
    ptp = float(np.ptp(good, axis=1).max())
    if ptp > ml_config.ARTIFACT_PTP_MI:
        stats.amplitude += 1
        return None
    if np.any(np.std(good, axis=1) < ml_config.FLAT_THRESHOLD_UV):
        stats.flat += 1
        return None
    return cleaned, ptp


def extract_mi_epochs(
    eeg_df: pd.DataFrame,
    trials_df: pd.DataFrame,
    sfreq: float,
    mi_trial_types: list[str],
    *,
    options: Optional[EpochOptions] = None,
    stats: Optional[RejectionStats] = None,
    group_prefix: str = "",
) -> tuple[list[np.ndarray], list[int], list[dict]]:
    """Cut, clean and label MI epochs for the given trial types.

    Labels are indices into `mi_trial_types`. Each meta dict carries a
    `group` id shared by all windows of one trial (used for grouped CV).
    """
    opts = options or EpochOptions()
    stats = stats if stats is not None else RejectionStats()
    aggregated = _aggregate_trials(trials_df)
    if aggregated.empty:
        return [], [], []
    mi = aggregated[aggregated["trial_type"].isin(mi_trial_types)]
    if mi.empty:
        return [], [], []

    target_sfreq = float(ml_config.TARGET_SFREQ)
    win_samples = int(opts.mi_window_s * target_sfreq)
    min_samples = int(ml_config.MI_EPOCH_MIN_S * sfreq)

    epochs: list[np.ndarray] = []
    labels: list[int] = []
    meta: list[dict] = []

    for _, trial in mi.iterrows():
        seg = _segment(eeg_df, trial["start_time"] + opts.mi_offset_s, trial["end_time"])
        if seg.shape[1] < min_samples:
            stats.too_short += 1
            continue
        seg, actual_sfreq = _resample_if_needed(seg, sfreq)

        if opts.mi_stride_s is None or seg.shape[1] <= win_samples:
            windows = [_fit_length(seg, win_samples)]
        else:
            stride = max(1, int(opts.mi_stride_s * target_sfreq))
            windows = [
                seg[:, s : s + win_samples]
                for s in range(0, seg.shape[1] - win_samples + 1, stride)
            ]

        group = f"{group_prefix}r{int(trial['run_index'])}_t{int(trial['trial_index'])}"
        for w_idx, window in enumerate(windows):
            result = _clean_mi_window(window, actual_sfreq, stats)
            if result is None:
                continue
            cleaned, ptp = result
            epochs.append(cleaned)
            labels.append(mi_trial_types.index(trial["trial_type"]))
            meta.append({
                "trial_type": trial["trial_type"],
                "run_index": int(trial["run_index"]),
                "trial_index": int(trial["trial_index"]),
                "window_index": w_idx,
                "group": group,
                "ptp_uv": ptp,
            })

    return epochs, labels, meta


def extract_mi_epochs_4class(
    eeg_df: pd.DataFrame,
    trials_df: pd.DataFrame,
    sfreq: float,
) -> tuple[list[np.ndarray], list[int], list[dict]]:
    """4-class MI epoch extraction (forward / backward / rotate-left / rotate-right)."""
    return extract_mi_epochs(eeg_df, trials_df, sfreq, list(ml_config.MI_TYPES_4CLASS))


def _blink_center(eeg_df: pd.DataFrame, trial: pd.Series, sfreq: float, window_s: float, peak_search: bool) -> float:
    """Timestamp to centre a blink epoch on.

    With a start/end span longer than the epoch, use the largest frontal
    deflection inside it — the user blinks some reaction time after the cue,
    not exactly at it. Otherwise fall back to the event marker.
    """
    marker = float(trial["event_marker"])
    if not peak_search or not bool(trial.get("has_end", False)):
        return marker
    t0, t1 = float(trial["start_time"]), float(trial["end_time"])
    if t1 - t0 < window_s:
        return marker
    seg, ts = _segment_with_ts(eeg_df, t0, t1)
    if seg.shape[1] < 10:
        return marker
    filtered = preprocess_blink_epoch(seg, sfreq)
    frontal = np.abs(filtered[FRONTAL_IDX]).sum(axis=0)
    peak = int(np.argmax(frontal))
    # Keep the whole epoch inside the trial span.
    half = window_s / 2
    return float(np.clip(ts[peak], t0 + half, t1 - half))


def extract_blink_pos_epochs(
    eeg_df: pd.DataFrame,
    trials_df: pd.DataFrame,
    sfreq: float,
    window_s: float = ml_config.BLINK_EPOCH_S,
    *,
    peak_search: bool = True,
) -> list[np.ndarray]:
    """Positive (blink) epochs, one per `blink_intentional` trial."""
    aggregated = _aggregate_trials(trials_df)
    if aggregated.empty:
        return []
    blinks = aggregated[aggregated["trial_type"] == L.TRIAL_BLINK]
    if blinks.empty:
        return []

    target_samples = int(window_s * ml_config.TARGET_SFREQ)
    epochs: list[np.ndarray] = []

    for _, trial in blinks.iterrows():
        center = _blink_center(eeg_df, trial, sfreq, window_s, peak_search)
        seg = _segment(eeg_df, center - window_s / 2, center + window_s / 2)
        if seg.shape[1] < 10:
            continue
        seg, actual_sfreq = _resample_if_needed(seg, sfreq)
        seg = _fit_length(seg, target_samples)
        epochs.append(preprocess_blink_epoch(seg, actual_sfreq))
    return epochs


def extract_blink_neg_epochs(
    eeg_df: pd.DataFrame,
    trials_df: pd.DataFrame,
    sfreq: float,
    window_s: float = ml_config.BLINK_EPOCH_S,
    rng: Optional[np.random.Generator] = None,
    *,
    source_trial_types: Optional[list[str]] = None,
) -> list[np.ndarray]:
    """Sample non-blink windows (no overlap) from inside MI and rest trials."""
    if rng is None:
        rng = np.random.default_rng(ml_config.RANDOM_STATE)
    if source_trial_types is None:
        source_trial_types = list(L.ALL_MI_TRIAL_TYPES) + [L.TRIAL_REST]
    aggregated = _aggregate_trials(trials_df)
    if aggregated.empty:
        return []
    sources = aggregated[aggregated["trial_type"].isin(source_trial_types)]
    target_samples = int(window_s * ml_config.TARGET_SFREQ)

    epochs: list[np.ndarray] = []
    for _, trial in sources.iterrows():
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
    mi_trial_types: list[str] = field(default_factory=list)
    mi_trials_recorded: dict[str, int] = field(default_factory=dict)
    blink_trials_recorded: int = 0
    rejections: RejectionStats = field(default_factory=RejectionStats)

    @property
    def mi_groups(self) -> list[str]:
        return [m["group"] for m in self.mi_meta]


def gather_session_data(
    sessions: list[SessionPaths],
    *,
    mi_trial_types: Optional[list[str]] = None,
    include_blink: bool = True,
    options: Optional[EpochOptions] = None,
) -> ParticipantData:
    """Load, clean and epoch every session. Detects MI classes if not given."""
    opts = options or EpochOptions()
    loaded = [(sp, *load_session(sp)) for sp in sessions]

    if mi_trial_types is None:
        present: set[str] = set()
        for _, _, trials_df, _ in loaded:
            present.update(str(t) for t in trials_df["trial_type"].dropna().unique())
        mi_trial_types = detect_mi_trial_types(present)
        log.info("Detected MI trial types: %s", mi_trial_types)

    data = ParticipantData(
        mi_epochs=[], mi_labels=[], mi_meta=[], blink_pos=[], blink_neg=[],
        n_sessions=len(sessions), n_runs_seen=0,
        mi_trial_types=list(mi_trial_types),
        mi_trials_recorded={t: 0 for t in mi_trial_types},
    )
    runs_seen: set[tuple[str, int]] = set()
    rng = np.random.default_rng(ml_config.RANDOM_STATE)

    for sp, eeg_df, trials_df, sfreq in loaded:
        name = sp.session_dir.name
        runs_seen.update(
            (name, int(r)) for r in trials_df["run_index"].unique() if not pd.isna(r)
        )
        aggregated = _aggregate_trials(trials_df)
        if not aggregated.empty:
            counts = aggregated["trial_type"].value_counts()
            for t in mi_trial_types:
                data.mi_trials_recorded[t] += int(counts.get(t, 0))
            data.blink_trials_recorded += int(counts.get(L.TRIAL_BLINK, 0))

        mi_e, mi_l, mi_m = extract_mi_epochs(
            eeg_df, trials_df, sfreq, mi_trial_types,
            options=opts, stats=data.rejections, group_prefix=f"{name}/",
        )
        data.mi_epochs.extend(mi_e)
        data.mi_labels.extend(mi_l)
        data.mi_meta.extend(mi_m)

        n_pos = n_neg = 0
        if include_blink:
            pos = extract_blink_pos_epochs(
                eeg_df, trials_df, sfreq, opts.blink_window_s,
                peak_search=opts.blink_peak_search,
            )
            neg = extract_blink_neg_epochs(eeg_df, trials_df, sfreq, opts.blink_window_s, rng)
            data.blink_pos.extend(pos)
            data.blink_neg.extend(neg)
            n_pos, n_neg = len(pos), len(neg)

        log.info(
            "  %s: MI windows=%d, blink+=%d, blink-=%d (sfreq≈%.1f Hz)",
            name, len(mi_e), n_pos, n_neg, sfreq,
        )

    data.n_runs_seen = len(runs_seen)
    return data


def gather_participant_data(participant: int, data_root: Path) -> ParticipantData:
    sessions = find_sessions(participant, data_root)
    if not sessions:
        raise FileNotFoundError(
            f"No sessions found for participant {participant} under {data_root}. "
            f"Run `python -m pipeline.collector --participant {participant} ...` first."
        )
    return gather_session_data(sessions)


# ── Model fitting ──────────────────────────────────────────────────


def _build_mi_pipeline() -> Pipeline:
    """RandomForest pipeline for motor imagery."""
    return Pipeline([
        ("scaler", StandardScaler()),
        ("clf", RandomForestClassifier(
            n_estimators=200, max_depth=6, min_samples_leaf=2,
            random_state=ml_config.RANDOM_STATE,
        )),
    ])


def _build_blink_pipeline() -> Pipeline:
    """LDA pipeline for blink detection."""
    return Pipeline([
        ("scaler", StandardScaler()),
        ("clf", LinearDiscriminantAnalysis(solver="lsqr", shrinkage="auto")),
    ])


@dataclass
class CVResult:
    accuracy: float
    per_class_accuracy: dict[int, float]
    n_folds: int


def _cross_validate(
    pipeline: Pipeline,
    X: np.ndarray,
    y: np.ndarray,
    groups: Optional[np.ndarray] = None,
    *,
    k: int = ml_config.CV_FOLDS,
) -> Optional[CVResult]:
    """Out-of-fold accuracy (overall + per class), or None if data is too small.

    With `groups`, folds are split by group (trial) so overlapping windows
    from one trial cannot leak between train and test.
    """
    if X.shape[0] < 4 or len(np.unique(y)) < 2:
        return None
    classes = np.unique(y)
    if groups is not None:
        units_per_class = [len(np.unique(groups[y == c])) for c in classes]
    else:
        units_per_class = [int(np.sum(y == c)) for c in classes]
    folds = min(k, min(units_per_class))
    if folds < 2:
        return None
    try:
        if groups is not None:
            cv = StratifiedGroupKFold(n_splits=folds, shuffle=True, random_state=ml_config.RANDOM_STATE)
            pred = cross_val_predict(pipeline, X, y, cv=cv, groups=groups)
        else:
            cv = StratifiedKFold(n_splits=folds, shuffle=True, random_state=ml_config.RANDOM_STATE)
            pred = cross_val_predict(pipeline, X, y, cv=cv)
    except Exception:                                               # noqa: BLE001
        log.exception("CV scoring failed")
        return None
    per_class = {int(c): float(np.mean(pred[y == c] == c)) for c in classes}
    return CVResult(accuracy=float(np.mean(pred == y)), per_class_accuracy=per_class, n_folds=folds)


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
    mi_class_names: list[str] = field(default_factory=list)
    mi_cv_per_class: dict[str, float] = field(default_factory=dict)


def fit_personalized_models(data: ParticipantData) -> FitResult:
    class_names = [L.TRIAL_TYPE_TO_LABEL[t] for t in data.mi_trial_types]

    mi_pipeline = None
    mi_cv: Optional[CVResult] = None
    mi_train_acc = None
    mi_class_counts: dict[str, int] = {}
    if data.mi_epochs:
        X_mi = extract_mi_features_batch(data.mi_epochs, ml_config.TARGET_SFREQ)
        y_mi = np.asarray(data.mi_labels)
        for cidx, cname in enumerate(class_names):
            mi_class_counts[cname] = int(np.sum(y_mi == cidx))
        if len(np.unique(y_mi)) >= 2 and X_mi.shape[0] >= 4:
            mi_pipeline = _build_mi_pipeline()
            mi_cv = _cross_validate(_build_mi_pipeline(), X_mi, y_mi, np.asarray(data.mi_groups))
            mi_pipeline.fit(X_mi, y_mi)
            mi_train_acc = float(np.mean(mi_pipeline.predict(X_mi) == y_mi))
        else:
            log.warning(
                "MI dataset too small for fitting: n=%d, classes=%s",
                X_mi.shape[0], np.unique(y_mi).tolist(),
            )

    blink_pipeline = None
    blink_cv: Optional[CVResult] = None
    blink_train_acc = None
    if data.blink_pos and data.blink_neg:
        all_eps = data.blink_pos + data.blink_neg
        all_labs = np.array([1] * len(data.blink_pos) + [0] * len(data.blink_neg))
        X_b = extract_blink_features_batch(all_eps, ml_config.TARGET_SFREQ)
        if len(np.unique(all_labs)) >= 2 and X_b.shape[0] >= 4:
            blink_pipeline = _build_blink_pipeline()
            blink_cv = _cross_validate(_build_blink_pipeline(), X_b, all_labs)
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
        mi_cv=mi_cv.accuracy if mi_cv else None,
        blink_cv=blink_cv.accuracy if blink_cv else None,
        mi_train_acc=mi_train_acc,
        blink_train_acc=blink_train_acc,
        n_mi=len(data.mi_epochs),
        n_blink_pos=len(data.blink_pos),
        n_blink_neg=len(data.blink_neg),
        mi_class_counts=mi_class_counts,
        mi_class_names=class_names,
        mi_cv_per_class=(
            {class_names[c]: acc for c, acc in mi_cv.per_class_accuracy.items()}
            if mi_cv else {}
        ),
    )


# ── Report ─────────────────────────────────────────────────────────


@dataclass
class ReadinessCheck:
    name: str
    passed: bool
    detail: str


@dataclass
class PersonalizeReport:
    """Everything the app needs to show after training. JSON-serialisable."""

    model_path: str
    created_at: str
    n_sessions: int
    mi_classes: list[str]
    mi_trials_recorded: dict[str, int]
    mi_trials_kept: dict[str, int]
    mi_windows_kept: dict[str, int]
    mi_windows_rejected: dict[str, int]
    blink_trials_recorded: int
    blink_epochs: int
    non_blink_epochs: int
    mi_cv_accuracy: Optional[float]
    mi_cv_per_class: dict[str, float]
    mi_chance: Optional[float]
    blink_cv_accuracy: Optional[float]
    mi_train_accuracy: Optional[float]
    blink_train_accuracy: Optional[float]
    checks: list[ReadinessCheck]
    ready: bool

    def to_dict(self) -> dict:
        return asdict(self)


def build_report(data: ParticipantData, fit: FitResult, model_path: Path) -> PersonalizeReport:
    class_names = fit.mi_class_names
    trials_kept: dict[str, int] = {c: 0 for c in class_names}
    seen_groups: set[str] = set()
    for m in data.mi_meta:
        if m["group"] not in seen_groups:
            seen_groups.add(m["group"])
            trials_kept[L.TRIAL_TYPE_TO_LABEL[m["trial_type"]]] += 1
    recorded = {
        L.TRIAL_TYPE_TO_LABEL[t]: n for t, n in data.mi_trials_recorded.items()
    }
    chance = 1.0 / len(class_names) if class_names else None

    checks: list[ReadinessCheck] = []
    for cname in class_names:
        n = trials_kept.get(cname, 0)
        checks.append(ReadinessCheck(
            name=f"enough_trials:{cname}",
            passed=n >= READY_MIN_MI_TRIALS_PER_CLASS,
            detail=f"{n} clean trials (need {READY_MIN_MI_TRIALS_PER_CLASS})",
        ))
    if fit.mi_pipeline is None or fit.mi_cv is None or chance is None:
        checks.append(ReadinessCheck("mi_accuracy", False, "MI model could not be validated"))
    else:
        bar = chance + READY_MI_MARGIN_OVER_CHANCE
        checks.append(ReadinessCheck(
            name="mi_accuracy",
            passed=fit.mi_cv >= bar,
            detail=f"cross-validated {fit.mi_cv:.0%} (need {bar:.0%}, chance {chance:.0%})",
        ))
    checks.append(ReadinessCheck(
        name="enough_trials:blink",
        passed=fit.n_blink_pos >= READY_MIN_BLINK_TRIALS,
        detail=f"{fit.n_blink_pos} blink epochs (need {READY_MIN_BLINK_TRIALS})",
    ))
    if fit.blink_pipeline is None or fit.blink_cv is None:
        checks.append(ReadinessCheck("blink_accuracy", False, "Blink model could not be validated"))
    else:
        checks.append(ReadinessCheck(
            name="blink_accuracy",
            passed=fit.blink_cv >= READY_MIN_BLINK_CV,
            detail=f"cross-validated {fit.blink_cv:.0%} (need {READY_MIN_BLINK_CV:.0%})",
        ))

    return PersonalizeReport(
        model_path=str(model_path),
        created_at=datetime.now().isoformat(timespec="seconds"),
        n_sessions=data.n_sessions,
        mi_classes=class_names,
        mi_trials_recorded=recorded,
        mi_trials_kept=trials_kept,
        mi_windows_kept=dict(fit.mi_class_counts),
        mi_windows_rejected=asdict(data.rejections),
        blink_trials_recorded=data.blink_trials_recorded,
        blink_epochs=fit.n_blink_pos,
        non_blink_epochs=fit.n_blink_neg,
        mi_cv_accuracy=fit.mi_cv,
        mi_cv_per_class=fit.mi_cv_per_class,
        mi_chance=chance,
        blink_cv_accuracy=fit.blink_cv,
        mi_train_accuracy=fit.mi_train_acc,
        blink_train_accuracy=fit.blink_train_acc,
        checks=checks,
        ready=all(c.passed for c in checks),
    )


# ── Bundle save ────────────────────────────────────────────────────


def save_personalized_bundle(
    fit: FitResult,
    *,
    participant: Optional[int] = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    output_path: Optional[Path] = None,
    mi_trial_types: Optional[list[str]] = None,
) -> Path:
    """Save the personalised bundle (default: models/participants/sub<NN>.pkl)."""
    if output_path is None:
        if participant is None:
            raise ValueError("Either participant or output_path is required")
        output_path = Path(output_dir) / f"sub{participant:02d}.pkl"
    mi_trial_types = mi_trial_types or list(ml_config.MI_TYPES_4CLASS)
    return save_model_bundle(
        mi_pipeline=fit.mi_pipeline,
        mi_csp_W=None,
        blink_pipeline=fit.blink_pipeline,
        subjects_list=[],  # intentionally empty — no subject IDs / names in bundle
        class_names=list(fit.mi_class_names),
        blink_class_names=["non_blink", L.LABEL_BLINK],
        model_version=PERSONALIZED_MODEL_VERSION,
        model_kind="personalized",
        mi_label_map={t: i for i, t in enumerate(mi_trial_types)},
        participant_id=participant,
        output_path=Path(output_path),
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


def run_personalization(
    sessions: list[SessionPaths],
    *,
    output_path: Path,
    classes: Optional[list[str]] = None,
    options: Optional[EpochOptions] = None,
    participant: Optional[int] = None,
) -> PersonalizeReport:
    """Load → clean → fit → save for an explicit list of sessions."""
    if not sessions:
        raise FileNotFoundError("No recorded sessions to train on")
    mi_types: Optional[list[str]] = None
    include_blink = True
    if classes:
        mi_types, include_blink = mi_trial_types_from_classes(classes)

    data = gather_session_data(
        sessions, mi_trial_types=mi_types, include_blink=include_blink, options=options,
    )
    log.info(
        "Loaded %d session(s); MI windows=%d (rejected %d), blink+=%d, blink-=%d",
        data.n_sessions, len(data.mi_epochs), data.rejections.total(),
        len(data.blink_pos), len(data.blink_neg),
    )
    if len(data.mi_trial_types) < 2:
        raise RuntimeError(
            f"Need at least two MI classes to train, found {data.mi_trial_types}"
        )

    fit = fit_personalized_models(data)
    # The realtime engine (eeg_to_meta.model_loader) refuses bundles that
    # lack either model, so a partial fit is a failure, not a result.
    problems = []
    if fit.mi_pipeline is None:
        problems.append(
            f"motor imagery: {len(data.mi_epochs)} clean windows across "
            f"{len(set(data.mi_labels))} class(es), rejected {asdict(data.rejections)}"
        )
    if include_blink and fit.blink_pipeline is None:
        problems.append(
            f"blink: {len(data.blink_pos)} blink / {len(data.blink_neg)} non-blink epochs"
        )
    if problems:
        raise RuntimeError("Not enough clean data to train — " + "; ".join(problems))

    out = save_personalized_bundle(
        fit, participant=participant, output_path=output_path,
        mi_trial_types=data.mi_trial_types,
    )
    report = build_report(data, fit, out)
    log.info(
        "MI: cv=%s per-class=%s | Blink: cv=%s | ready=%s",
        _pct(fit.mi_cv), {k: _pct(v) for k, v in fit.mi_cv_per_class.items()},
        _pct(fit.blink_cv), report.ready,
    )
    log.info("Saved personalized bundle → %s", out)
    return report


def _pct(v: Optional[float]) -> str:
    return f"{v * 100:.1f}%" if v is not None else "n/a"


def personalize(
    *,
    participant: int,
    data_root: Path = DEFAULT_DATA_ROOT,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    """End-to-end for a Unity-collector participant. Returns the bundle path."""
    log.info("Gathering data for participant sub%02d under %s", participant, data_root)
    sessions = find_sessions(participant, data_root)
    if not sessions:
        raise FileNotFoundError(
            f"No sessions found for participant {participant} under {data_root}. "
            f"Run `python -m pipeline.collector --participant {participant} ...` first."
        )
    report = run_personalization(
        sessions,
        output_path=Path(output_dir) / f"sub{participant:02d}.pkl",
        participant=participant,
    )
    return Path(report.model_path)


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Refit MI+blink models on one user's data.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--participant", type=int,
                     help="Participant number, e.g. 5 (loads <data-root>/sub05/session_*/)")
    src.add_argument("--sessions-dir", type=Path,
                     help="Directory containing session_*/ folders (app backend layout)")
    p.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT,
                   help=f"Root containing sub<NN>/ subdirs (default: {DEFAULT_DATA_ROOT})")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                   help=f"Where to write sub<NN>.pkl (default: {DEFAULT_OUTPUT_DIR})")
    p.add_argument("--output", type=Path, default=None,
                   help="Explicit bundle path (required with --sessions-dir)")
    p.add_argument("--classes", type=str, default=None,
                   help="Comma-separated classes, e.g. left,right,blink (default: detect from data)")
    p.add_argument("--mi-stride", type=float, default=None,
                   help="Cut overlapping MI windows every N seconds (default: one window per trial)")
    p.add_argument("--mi-offset", type=float, default=0.0,
                   help="Seconds to skip after each MI trial start (reaction time)")
    p.add_argument("--report-json", type=Path, default=None,
                   help="Write the training report as JSON to this path")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="DEBUG-level logging")
    return p


def _write_error(report_json: Optional[Path], message: str) -> None:
    """Leave a machine-readable error where the caller expects the report."""
    if report_json is None:
        return
    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps({"error": message}, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = _build_argparser()
    args = parser.parse_args(argv)
    if args.sessions_dir is not None and args.output is None:
        parser.error("--output is required with --sessions-dir")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    classes = [c.strip() for c in args.classes.split(",") if c.strip()] if args.classes else None
    options = EpochOptions(mi_stride_s=args.mi_stride, mi_offset_s=args.mi_offset)

    t0 = time.time()
    try:
        if args.sessions_dir is not None:
            sessions = find_sessions_in(args.sessions_dir)
            output = args.output
            participant = None
        else:
            sessions = find_sessions(args.participant, args.data_root)
            output = args.output or Path(args.output_dir) / f"sub{args.participant:02d}.pkl"
            participant = args.participant
        report = run_personalization(
            sessions, output_path=output, classes=classes,
            options=options, participant=participant,
        )
    except (FileNotFoundError, ValueError) as exc:
        log.error("%s", exc)
        _write_error(args.report_json, str(exc))
        return 2
    except RuntimeError as exc:
        log.error("Personalization failed: %s", exc)
        _write_error(args.report_json, str(exc))
        return 3

    if args.report_json is not None:
        args.report_json.parent.mkdir(parents=True, exist_ok=True)
        args.report_json.write_text(json.dumps(report.to_dict(), indent=2))
    log.info("Done in %.1fs → %s", time.time() - t0, report.model_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
