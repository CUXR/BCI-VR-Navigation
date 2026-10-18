"""App-flow state machine: one user, one headset, one stage at a time.

    (no session) ──open──► connected ──► calibrating ──► connected
                               │  ▲                          │
                               │  └──────── training ◄───────┘
                               └──► realtime ──► connected

Calibration and realtime both need the live stream and are mutually
exclusive. Training only reads recorded files, so it runs in the
background while connected; it is blocked during calibration so it
can't train on a half-written session.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .board import EEGSource
from .calibration import CalibrationPlan, CalibrationRun
from .events import EventBus
from .realtime import RealtimeRunner
from .settings import Settings, user_key
from .training import (
    TrainingError,
    TrainingOptions,
    latest_model,
    sessions_dir,
    train_user_model,
)

log = logging.getLogger(__name__)

STAGE_CONNECTED = "connected"
STAGE_CALIBRATING = "calibrating"
STAGE_REALTIME = "realtime"


class StateError(RuntimeError):
    """Request is not valid in the current stage (→ HTTP 409)."""


class SessionManager:
    def __init__(self, settings: Settings, bus: EventBus) -> None:
        self.settings = settings
        self.bus = bus
        self._lock = asyncio.Lock()
        self.user_id: Optional[str] = None
        self.user_dir: Optional[Path] = None
        self.source: Optional[EEGSource] = None
        self.stage: Optional[str] = None
        self._calibration: Optional[CalibrationRun] = None
        self._calibration_task: Optional[asyncio.Task] = None
        self._last_calibration: Optional[dict] = None
        self._training_task: Optional[asyncio.Task] = None
        self._last_training: Optional[dict] = None
        self._realtime: Optional[RealtimeRunner] = None

    # ── status ─────────────────────────────────────────────────────

    def status(self) -> dict:
        model = latest_model(self.user_dir) if self.user_dir else None
        return {
            "session_open": self.source is not None,
            "user_key": user_key(self.user_id) if self.user_id else None,
            "stage": self.stage,
            "board": {
                "kind": self.source.kind,
                "streaming": self.source.is_streaming,
                "sampling_rate": self.source.sampling_rate,
                "samples_received": self.source.samples_received,
                "last_chunk_time": self.source.last_chunk_time,
            } if self.source else None,
            "calibration": {
                "running": self._calibration_task is not None and not self._calibration_task.done(),
                "progress": self._calibration.progress if self._calibration else None,
                "last_result": self._last_calibration,
            },
            "sessions_recorded": self._count_sessions(),
            "training": {
                "running": self.training_running,
                "last_result": self._last_training,
            },
            "model": {"path": str(model[0]), "report": model[1]} if model else None,
            "realtime": {
                "running": self._realtime is not None and self._realtime.running,
                "n_predictions": self._realtime.n_predictions if self._realtime else 0,
            },
        }

    @property
    def training_running(self) -> bool:
        return self._training_task is not None and not self._training_task.done()

    def _count_sessions(self) -> int:
        if not self.user_dir:
            return 0
        d = sessions_dir(self.user_dir)
        return len(list(d.glob("session_*"))) if d.is_dir() else 0

    # ── session ────────────────────────────────────────────────────

    async def open(self, user_id: str, serial: Optional[str] = None) -> dict:
        async with self._lock:
            if self.source is not None:
                if user_id == self.user_id:
                    return self.status()
                raise StateError("Another user's session is open; close it first")
            source = EEGSource(self.settings.board, serial or self.settings.serial)
            self.bus.publish("session_connecting", board=self.settings.board)
            try:
                await asyncio.to_thread(source.start)
            except Exception as exc:
                self.bus.publish("session_error", message=str(exc))
                raise ConnectionError(f"Could not connect to the headset: {exc}") from exc
            self.user_id = user_id
            self.user_dir = self.settings.user_dir(user_id)
            sessions_dir(self.user_dir).mkdir(parents=True, exist_ok=True)
            self.source = source
            self.stage = STAGE_CONNECTED
            self.bus.publish("session_opened", board=source.kind, sampling_rate=source.sampling_rate)
            return self.status()

    async def close(self) -> None:
        async with self._lock:
            await self._stop_calibration_locked()
            await self._stop_realtime_locked()
            if self._training_task is not None and not self._training_task.done():
                self._training_task.cancel()
            if self.source is not None:
                await asyncio.to_thread(self.source.stop)
            self.source = None
            self.user_id = None
            self.user_dir = None
            self.stage = None
            self._calibration = None
            self._last_calibration = None
            self._last_training = None
            self.bus.publish("session_closed")

    def _require_stage(self, *stages: str) -> None:
        if self.source is None:
            raise StateError("No session open; POST /session first")
        if self.stage not in stages:
            raise StateError(f"Not allowed while {self.stage}")

    # ── calibration ────────────────────────────────────────────────

    async def start_calibration(self, plan: CalibrationPlan) -> dict:
        async with self._lock:
            self._require_stage(STAGE_CONNECTED)
            if self.training_running:
                raise StateError("Training is running; wait for it to finish")
            run = CalibrationRun(self.source, plan, sessions_dir(self.user_dir), self.bus.publish)
            self._calibration = run
            self.stage = STAGE_CALIBRATING
            self._calibration_task = asyncio.create_task(self._calibration_main(run))
            return {
                "session_id": run.session_id,
                "plan": asdict(plan),
                "n_trials": run.progress["total"],
                "estimated_duration_s": plan.estimated_duration_s(),
            }

    async def _calibration_main(self, run: CalibrationRun) -> None:
        try:
            result = await run.run()
            self._last_calibration = result.to_dict()
        finally:
            if self.stage == STAGE_CALIBRATING:
                self.stage = STAGE_CONNECTED

    async def stop_calibration(self) -> Optional[dict]:
        async with self._lock:
            await self._stop_calibration_locked()
            return self._last_calibration

    async def _stop_calibration_locked(self) -> None:
        task = self._calibration_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task   # run() catches the cancel and saves what it has
            except asyncio.CancelledError:
                pass
        self._calibration_task = None

    # ── training ───────────────────────────────────────────────────

    async def start_training(self, options: TrainingOptions) -> dict:
        async with self._lock:
            self._require_stage(STAGE_CONNECTED, STAGE_REALTIME)
            if self.training_running:
                raise StateError("Training is already running")
            if self._count_sessions() == 0:
                raise StateError("No calibration data recorded yet")
            self._training_task = asyncio.create_task(self._training_main(options))
            self.bus.publish("training_started", classes=options.classes)
            return {"started": True, "classes": options.classes}

    async def _training_main(self, options: TrainingOptions) -> None:
        user_dir = self.user_dir
        try:
            model_path, report = await train_user_model(user_dir, options)
        except asyncio.CancelledError:
            self._last_training = {"status": "cancelled"}
            raise
        except TrainingError as exc:
            self._last_training = {"status": "failed", "error": str(exc)}
            self.bus.publish("training_finished", **self._last_training)
            return
        except Exception as exc:                                    # noqa: BLE001
            log.exception("Training crashed")
            self._last_training = {"status": "failed", "error": str(exc)}
            self.bus.publish("training_finished", **self._last_training)
            return
        self._last_training = {"status": "completed", "model_path": str(model_path), "report": report}
        self.bus.publish("training_finished", **self._last_training)

    # ── realtime ───────────────────────────────────────────────────

    async def start_realtime(
        self,
        *,
        mi_threshold: Optional[float] = None,
        blink_threshold: Optional[float] = None,
        ws_host: Optional[str] = None,
        ws_port: Optional[int] = None,
    ) -> dict:
        async with self._lock:
            self._require_stage(STAGE_CONNECTED)
            model = latest_model(self.user_dir)
            if model is None:
                raise StateError("No trained model for this user; POST /train first")
            runner = RealtimeRunner(
                self.source, model[0], self.bus.publish,
                ws_host=ws_host or self.settings.unity_ws_host,
                ws_port=ws_port or self.settings.unity_ws_port,
                mi_threshold=mi_threshold,
                blink_threshold=blink_threshold,
            )
            await runner.start()
            self._realtime = runner
            self.stage = STAGE_REALTIME
            return {"model_path": str(model[0]), "unity_ws": f"ws://{runner.ws_host}:{runner.ws_port}"}

    async def stop_realtime(self) -> None:
        async with self._lock:
            await self._stop_realtime_locked()

    async def _stop_realtime_locked(self) -> None:
        if self._realtime is not None:
            await self._realtime.stop()
            self._realtime = None
            if self.stage == STAGE_REALTIME:
                self.stage = STAGE_CONNECTED
