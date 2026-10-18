"""Backend-driven calibration: cue schedule, timing and recording.

The backend decides what the user should do and when, and pushes each cue
to the frontend over the event bus; the frontend only renders cues. One
calibration run records one session directory in the same layout as
`pipeline.collector`, so `pipeline.personalize` trains on it unchanged:

    <user_dir>/sessions/session_<YYYYMMDD_HHMMSS>/
        eeg_data.csv  trial_log.csv  metadata.yaml

Trial timeline (each phase is one trial_log row and one board marker):

    MI:     cue (get ready) → start (imagine) → end → rest
    blink:  cue (get ready) → start (blink once) → end → rest
    rest:   start → end         (logged; used as non-blink negatives)
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from pipeline import labels as L
from pipeline.collector import (
    TrialRow,
    write_eeg_csv,
    write_metadata_yaml,
    write_trial_log_csv,
)

from .board import CH_NAMES, EEGChunk, EEGSource
from .training import DEFAULT_MI_OFFSET_S

log = logging.getLogger(__name__)

# Training skips DEFAULT_MI_OFFSET_S of reaction time and needs one full
# 2 s model window after it.
MI_WINDOW_S = 2.0
MIN_IMAGINE_S = MI_WINDOW_S + DEFAULT_MI_OFFSET_S

Publish = Callable[..., None]


@dataclass
class CalibrationPlan:
    classes: list[str] = field(default_factory=lambda: list(L.DEFAULT_APP_CLASSES))
    trials_per_class: int = 15
    cue_s: float = 1.5        # "get ready: LEFT"
    imagine_s: float = 4.0    # MI period; ≥ MIN_IMAGINE_S so a model window fits
    blink_s: float = 2.0      # window in which the user blinks once
    rest_s: float = 3.0
    rest_jitter_s: float = 1.0
    seed: Optional[int] = None

    def validate(self) -> None:
        if not self.classes:
            raise ValueError("classes must not be empty")
        unknown = [c for c in self.classes if c not in L.APP_CLASS_TO_TRIAL_TYPE]
        if unknown:
            raise ValueError(f"Unknown class(es): {unknown}")
        if len(set(self.classes)) != len(self.classes):
            raise ValueError("classes must be unique")
        if self.trials_per_class < 1:
            raise ValueError("trials_per_class must be ≥ 1")
        if self.imagine_s < MIN_IMAGINE_S:
            raise ValueError(
                f"imagine_s must be ≥ {MIN_IMAGINE_S} (reaction offset + 2 s model window)"
            )
        if min(self.cue_s, self.blink_s, self.rest_s) <= 0 or self.rest_jitter_s < 0:
            raise ValueError("timings must be positive")

    def trial_seconds(self, cls: str) -> float:
        active = self.blink_s if cls == L.CLASS_BLINK else self.imagine_s
        return self.cue_s + active + self.rest_s + self.rest_jitter_s / 2

    def estimated_duration_s(self) -> float:
        return self.trials_per_class * sum(self.trial_seconds(c) for c in self.classes)

    @classmethod
    def for_duration(cls, duration_s: float, **kwargs) -> "CalibrationPlan":
        """Fit as many balanced trials as possible into `duration_s`."""
        plan = cls(**kwargs)
        per_round = sum(plan.trial_seconds(c) for c in plan.classes)
        plan.trials_per_class = max(1, math.floor(duration_s / per_round))
        return plan

    def schedule(self) -> list[str]:
        """Balanced, shuffled order: each round contains every class once."""
        rng = random.Random(self.seed)
        order: list[str] = []
        for _ in range(self.trials_per_class):
            block = list(self.classes)
            rng.shuffle(block)
            # Avoid the same class twice in a row across block boundaries.
            if order and len(block) > 1 and block[0] == order[-1]:
                block[0], block[1] = block[1], block[0]
            order.extend(block)
        return order


@dataclass
class CalibrationResult:
    session_id: str
    session_dir: str
    status: str                       # "completed" | "cancelled" | "failed"
    trials_completed: dict[str, int]
    trials_planned: int
    duration_s: float
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


class _Recorder:
    """EEG sink that keeps every chunk while a calibration is running."""

    def __init__(self) -> None:
        self._chunks: list[EEGChunk] = []
        self._lock = threading.Lock()

    def __call__(self, chunk: EEGChunk) -> None:
        with self._lock:
            self._chunks.append(chunk)

    def n_samples(self) -> int:
        with self._lock:
            return sum(c.eeg.shape[1] for c in self._chunks)

    def samples(self):
        """Per-sample dicts in the collector CSV schema."""
        with self._lock:
            chunks = list(self._chunks)
        for c in chunks:
            for i in range(c.eeg.shape[1]):
                code = int(round(c.markers[i])) if abs(c.markers[i]) > 1e-9 else None
                yield {
                    "timestamp": float(c.timestamps[i]),
                    "marker_code": code,
                    "marker_label": L.MARKER_CODE_TO_TRIAL_TYPE.get(code) if code else None,
                    **{ch: float(c.eeg[j, i]) for j, ch in enumerate(CH_NAMES)},
                }


class CalibrationRun:
    def __init__(
        self,
        source: EEGSource,
        plan: CalibrationPlan,
        sessions_dir: Path,
        publish: Publish,
    ) -> None:
        plan.validate()
        self.source = source
        self.plan = plan
        self.publish = publish
        self.session_id = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.session_dir = Path(sessions_dir) / self.session_id
        self._recorder = _Recorder()
        self._rows: list[TrialRow] = []
        self._t0 = 0.0
        self._order = plan.schedule()
        self._completed: dict[str, int] = {c: 0 for c in plan.classes}
        self._rng = random.Random(plan.seed)

    @property
    def progress(self) -> dict:
        done = sum(self._completed.values())
        return {"completed": done, "total": len(self._order), "per_class": dict(self._completed)}

    async def run(self) -> CalibrationResult:
        self.session_dir.mkdir(parents=True, exist_ok=False)
        self.source.add_sink(self._recorder)
        self._t0 = time.time()
        status, error = "completed", None
        self.publish(
            "calibration_started",
            session_id=self.session_id,
            plan=asdict(self.plan),
            n_trials=len(self._order),
            estimated_duration_s=self.plan.estimated_duration_s(),
        )
        try:
            for idx, cls in enumerate(self._order):
                await self._run_trial(idx, cls)
        except asyncio.CancelledError:
            status = "cancelled"
        except Exception as exc:                                    # noqa: BLE001
            log.exception("Calibration failed")
            status, error = "failed", str(exc)
        finally:
            self.source.remove_sink(self._recorder)
            self.source.simulate_intent(None)

        result = await asyncio.to_thread(self._save, status, error)
        self.publish("calibration_finished", **result.to_dict())
        return result

    # ── trial sequencing ───────────────────────────────────────────

    async def _run_trial(self, idx: int, cls: str) -> None:
        trial_type = L.APP_CLASS_TO_TRIAL_TYPE[cls]
        active_s = self.plan.blink_s if cls == L.CLASS_BLINK else self.plan.imagine_s
        rows: list[TrialRow] = []

        await self._phase(rows, idx, cls, trial_type, "cue", self.plan.cue_s)
        await self._phase(rows, idx, cls, trial_type, "start", active_s)
        self._phase_now(rows, idx, cls, trial_type, "end", 0.0)
        # Commit only completed trials; a cancelled half-trial is noise.
        self._rows.extend(rows)
        self._completed[cls] += 1

        rest_s = self.plan.rest_s + self._rng.uniform(0, self.plan.rest_jitter_s)
        rest_rows: list[TrialRow] = []
        await self._phase(rest_rows, idx, "rest", L.TRIAL_REST, "start", rest_s)
        self._phase_now(rest_rows, idx, "rest", L.TRIAL_REST, "end", 0.0)
        self._rows.extend(rest_rows)

    async def _phase(self, rows, idx, cls, trial_type, phase, duration_s) -> None:
        self._phase_now(rows, idx, cls, trial_type, phase, duration_s)
        await asyncio.sleep(duration_s)

    def _phase_now(self, rows, idx, cls, trial_type, phase, duration_s) -> None:
        now = time.time()
        code = L.TRIAL_TYPE_TO_MARKER_CODE[trial_type]
        self.source.insert_marker(code)
        self.source.on_phase(trial_type, phase)
        rows.append(TrialRow(
            trial_id=f"trial_r00_t{idx:04d}_{trial_type}",
            trial_type=trial_type,
            label=L.TRIAL_TYPE_TO_LABEL.get(trial_type, "unknown"),
            phase=phase,
            run_index=0,
            trial_index=idx,
            ts_unity=now - self._t0,   # seconds since calibration start
            ts_recv=now,
            marker_code=code,
            session_id=self.session_id,
            participant_id="",         # no user identifiers on disk
        ))
        self.publish(
            "calibration_cue",
            trial_index=idx,
            n_trials=len(self._order),
            cls=cls,
            phase=phase,
            duration_s=duration_s,
            progress=self.progress,
        )

    # ── persistence ────────────────────────────────────────────────

    def _save(self, status: str, error: Optional[str]) -> CalibrationResult:
        duration = time.time() - self._t0
        result = CalibrationResult(
            session_id=self.session_id,
            session_dir=str(self.session_dir),
            status=status,
            trials_completed=dict(self._completed),
            trials_planned=len(self._order),
            duration_s=round(duration, 2),
            error=error,
        )
        if sum(self._completed.values()) == 0:
            # Nothing usable — don't leave an empty session for training to trip on.
            shutil.rmtree(self.session_dir, ignore_errors=True)
            return result

        write_eeg_csv(self.session_dir / "eeg_data.csv", self._recorder.samples())
        write_trial_log_csv(self.session_dir / "trial_log.csv", self._rows)
        write_metadata_yaml(self.session_dir / "metadata.yaml", {
            "session_id": self.session_id,
            "source": "app_calibration",
            "status": status,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "start_time": self._t0,
            "duration_s": round(duration, 2),
            "board_id": self.source.kind,
            "sampling_rate_hz": self.source.sampling_rate,
            "ch_names": CH_NAMES,
            "classes": self.plan.classes,
            "trials_planned": len(self._order),
            "trials_completed": sum(self._completed.values()),
            "n_eeg_samples": self._recorder.n_samples(),
        })
        log.info("Saved calibration session %s (%s)", self.session_dir, status)
        return result
