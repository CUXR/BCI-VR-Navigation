"""Realtime stage: user model → predictions to Unity and the app.

Reuses eeg_to_meta's inference engine, smoother and Unity WebSocket server
(same JSON protocol on :8765 that PredictionWebSocketClient expects), but
feeds them from the shared `EEGSource` instead of opening its own Muse
connection.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Callable, Optional

from .board import EEGChunk, EEGSource, MUSE_SFREQ
from .settings import PROJECT_ROOT

log = logging.getLogger(__name__)

_EEG_DIR = PROJECT_ROOT / "eeg_to_meta"
APP_PREDICTION_INTERVAL_S = 0.2   # throttle predictions forwarded to the app

Publish = Callable[..., None]


def _load_eeg_to_meta() -> dict[str, ModuleType]:
    """Import eeg_to_meta's flat modules (they use bare `from config import`)."""
    if str(_EEG_DIR) not in sys.path:
        sys.path.insert(0, str(_EEG_DIR))
    cfg = importlib.import_module("config")
    if Path(cfg.__file__).resolve().parent != _EEG_DIR:
        raise RuntimeError(
            f"A different 'config' module is already imported ({cfg.__file__}); "
            "ml_pipeline must not be imported into the backend process"
        )
    return {
        name: importlib.import_module(name)
        for name in ("config", "buffer", "model_loader", "inference", "smoothing", "websocket_server")
    }


class RealtimeRunner:
    def __init__(
        self,
        source: EEGSource,
        model_path: Path,
        publish: Publish,
        *,
        ws_host: str,
        ws_port: int,
        mi_threshold: Optional[float] = None,
        blink_threshold: Optional[float] = None,
    ) -> None:
        self.source = source
        self.model_path = Path(model_path)
        self.publish = publish
        self.ws_host = ws_host
        self.ws_port = ws_port
        self.mi_threshold = mi_threshold
        self.blink_threshold = blink_threshold
        self._task: Optional[asyncio.Task] = None
        self._ws = None
        self._buf = None
        self._sink = None
        self.n_predictions = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        m = _load_eeg_to_meta()
        cfg = m["config"]
        if self.source.sampling_rate != MUSE_SFREQ:
            log.warning("Realtime expects %d Hz, source is %d Hz", MUSE_SFREQ, self.source.sampling_rate)

        model = m["model_loader"].ModelBundle(self.model_path)
        if not await asyncio.to_thread(model.load):
            raise RuntimeError(f"Model failed to load or validate: {self.model_path}")
        self._apply_thresholds(cfg, model.mi_label_names())

        self._buf = m["buffer"].RingBuffer(max_seconds=10.0)
        buf = self._buf

        def sink(chunk: EEGChunk) -> None:
            buf.push(chunk.eeg)

        self._sink = sink
        self.source.add_sink(sink)

        self._ws = m["websocket_server"].JsonWSServer(host=self.ws_host, port=self.ws_port)
        self._ws.set_loop(asyncio.get_running_loop())
        try:
            await self._ws.start()
        except OSError:
            self.source.remove_sink(sink)
            raise

        engine = m["inference"].InferenceEngine(model)
        smoother = m["smoothing"].PredictionSmoother()
        self._task = asyncio.create_task(self._loop(cfg, engine, smoother))
        await self._ws.broadcast_state("realtime_ready", message="Realtime pipeline ready")
        self.publish(
            "realtime_started",
            classes=model.mi_label_names(),
            unity_ws=f"ws://{self.ws_host}:{self.ws_port}",
            thresholds=dict(cfg.CLASS_THRESHOLDS),
        )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._sink is not None:
            self.source.remove_sink(self._sink)
            self._sink = None
        if self._ws is not None:
            await self._ws.stop()
            self._ws = None
        self.publish("realtime_stopped", n_predictions=self.n_predictions)

    def _apply_thresholds(self, cfg: ModuleType, mi_labels: list[str]) -> None:
        mi = self.mi_threshold if self.mi_threshold is not None else cfg.MI_CONFIDENCE_THRESHOLD
        blink = self.blink_threshold if self.blink_threshold is not None else cfg.BLINK_CONFIDENCE_THRESHOLD
        for label in list(cfg.CLASS_THRESHOLDS) + mi_labels:
            if label != cfg.CLASS_NAMES["blink"]:
                cfg.CLASS_THRESHOLDS[label] = float(mi)
        cfg.CLASS_THRESHOLDS[cfg.CLASS_NAMES["blink"]] = float(blink)

    async def _loop(self, cfg: ModuleType, engine, smoother) -> None:
        last_app_publish = 0.0
        while True:
            t0 = time.perf_counter()
            blink_win = self._buf.get_blink_window()
            mi_win = self._buf.get_mi_window()
            if blink_win is not None or mi_win is not None:
                raw = await asyncio.to_thread(engine.classify, mi_win, blink_win)
                smoothed = smoother.update(raw)
                self.n_predictions += 1
                await self._ws.broadcast(smoothed)

                now = time.monotonic()
                is_blink = smoothed["label"] == cfg.CLASS_NAMES["blink"]
                if is_blink or now - last_app_publish >= APP_PREDICTION_INTERVAL_S:
                    last_app_publish = now
                    self.publish(
                        "prediction",
                        label=smoothed["label"],
                        confidence=smoothed["confidence"],
                        stable=smoothed["stable"],
                        raw_probs=smoothed["raw_probs"],
                        unity_clients=self._ws.n_clients,
                    )
            await asyncio.sleep(max(0.0, cfg.STRIDE_S - (time.perf_counter() - t0)))
