"""The one EEG connection shared by every stage of the app flow.

`EEGSource` opens the board once and fans each polled chunk out to
registered sinks, so calibration recording, realtime inference and the
connection-quality check can all read the same stream without
reconnecting the Muse between steps.

A sink is any callable `sink(chunk: EEGChunk) -> None`. Sinks run on the
poll thread and must be quick (copy / append, no heavy work).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from pipeline import labels as L

from .settings import BOARD_MUSE, BOARD_SIMULATED, BOARD_SYNTHETIC

log = logging.getLogger(__name__)

N_CHANNELS = 4
CH_NAMES = ["TP9", "AF7", "AF8", "TP10"]
MUSE_SFREQ = 256
POLL_INTERVAL_S = 0.02


@dataclass
class EEGChunk:
    timestamps: np.ndarray   # (n,) unix seconds, same clock as time.time()
    eeg: np.ndarray          # (4, n) µV in CH_NAMES order
    markers: np.ndarray      # (n,) 0 where no marker


Sink = Callable[[EEGChunk], None]


class EEGSource:
    def __init__(self, kind: str = BOARD_MUSE, serial: Optional[str] = None) -> None:
        self.kind = kind
        self.serial = serial
        self.sampling_rate: int = MUSE_SFREQ
        self._backend: Optional[_Backend] = None
        self._sinks: list[Sink] = []
        self._sinks_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self.last_chunk_time: Optional[float] = None
        self.samples_received = 0

    # ── lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        """Connect and start streaming. Blocking (BLE connect can take seconds)."""
        if self._running.is_set():
            return
        if self.kind == BOARD_SIMULATED:
            backend: _Backend = _SimulatedBackend()
        elif self.kind in (BOARD_MUSE, BOARD_SYNTHETIC):
            backend = _BrainFlowBackend(self.kind, self.serial)
        else:
            raise ValueError(f"Unknown board kind {self.kind!r}")
        backend.start()
        self._backend = backend
        self.sampling_rate = backend.sampling_rate
        self._running.set()
        self._thread = threading.Thread(target=self._poll_loop, name="eeg-poll", daemon=True)
        self._thread.start()
        log.info("EEG source %s streaming at %d Hz", self.kind, self.sampling_rate)

    def stop(self) -> None:
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._backend is not None:
            try:
                self._backend.stop()
            except Exception:                                       # noqa: BLE001
                log.exception("Error stopping EEG backend")
            self._backend = None
        log.info("EEG source stopped")

    @property
    def is_streaming(self) -> bool:
        return self._running.is_set()

    # ── sinks / markers ────────────────────────────────────────────

    def add_sink(self, sink: Sink) -> None:
        with self._sinks_lock:
            self._sinks.append(sink)

    def remove_sink(self, sink: Sink) -> None:
        with self._sinks_lock:
            if sink in self._sinks:
                self._sinks.remove(sink)

    def insert_marker(self, code: int) -> None:
        if self._backend is not None:
            try:
                self._backend.insert_marker(float(code))
            except Exception:                                       # noqa: BLE001
                log.exception("Failed to insert marker %d", code)

    def on_phase(self, trial_type: str, phase: str) -> None:
        """Calibration phase hook; only the simulated board reacts to it."""
        if self._backend is not None:
            self._backend.on_phase(trial_type, phase)

    def simulate_intent(self, intent: Optional[str]) -> bool:
        """Drive the simulated signal by hand (left / right / blink / None)."""
        if isinstance(self._backend, _SimulatedBackend):
            self._backend.set_intent(intent)
            return True
        return False

    # ── internals ──────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        while self._running.is_set():
            try:
                chunk = self._backend.read() if self._backend else None
            except Exception:                                       # noqa: BLE001
                log.exception("EEG read failed")
                chunk = None
            if chunk is not None and chunk.eeg.shape[1] > 0:
                self.last_chunk_time = time.time()
                self.samples_received += chunk.eeg.shape[1]
                with self._sinks_lock:
                    sinks = list(self._sinks)
                for sink in sinks:
                    try:
                        sink(chunk)
                    except Exception:                               # noqa: BLE001
                        log.exception("EEG sink failed")
            time.sleep(POLL_INTERVAL_S)


# ── Backends ──────────────────────────────────────────────────────


class _Backend:
    sampling_rate: int = MUSE_SFREQ

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def read(self) -> Optional[EEGChunk]: ...
    def insert_marker(self, code: float) -> None: ...
    def on_phase(self, trial_type: str, phase: str) -> None: ...


class _BrainFlowBackend(_Backend):
    def __init__(self, kind: str, serial: Optional[str]) -> None:
        self.kind = kind
        if serial and not serial.startswith("Muse-"):
            serial = f"Muse-{serial}"
        self.serial = serial
        self._board = None

    def start(self) -> None:
        from brainflow.board_shim import BoardIds, BoardShim, BrainFlowInputParams  # noqa: PLC0415

        board_id = (
            BoardIds.MUSE_2_BOARD.value if self.kind == BOARD_MUSE
            else BoardIds.SYNTHETIC_BOARD.value
        )
        params = BrainFlowInputParams()
        if self.serial and self.kind == BOARD_MUSE:
            params.serial_number = self.serial
        board = BoardShim(board_id, params)
        board.prepare_session()
        board.start_stream()
        self._board = board
        self.sampling_rate = BoardShim.get_sampling_rate(board_id)
        self._eeg_idx = BoardShim.get_eeg_channels(board_id)[:N_CHANNELS]
        self._ts_idx = BoardShim.get_timestamp_channel(board_id)
        self._marker_idx = BoardShim.get_marker_channel(board_id)
        if self.sampling_rate != MUSE_SFREQ:
            log.warning(
                "Board samples at %d Hz; models expect %d Hz (training resamples, "
                "realtime does not)", self.sampling_rate, MUSE_SFREQ,
            )

    def stop(self) -> None:
        if self._board is None:
            return
        try:
            self._board.stop_stream()
        finally:
            self._board.release_session()
            self._board = None

    def read(self) -> Optional[EEGChunk]:
        if self._board is None:
            return None
        data = self._board.get_board_data()
        if data.shape[1] == 0:
            return None
        return EEGChunk(
            timestamps=data[self._ts_idx].astype(np.float64),
            eeg=data[self._eeg_idx].astype(np.float64),
            markers=data[self._marker_idx].astype(np.float64),
        )

    def insert_marker(self, code: float) -> None:
        if self._board is not None:
            self._board.insert_marker(code)


class _SimulatedBackend(_Backend):
    """Muse-shaped signal that responds to calibration cues.

    Background: noise + a 10 Hz mu rhythm on the temporal channels.
    Left-hand imagery suppresses mu on the right side (TP10) and vice
    versa; a blink cue produces a frontal (AF7/AF8) blink ~0.4 s later.
    Lets the whole app flow — calibration, training, realtime — be
    exercised without a headset and produce a model that is better than
    chance, which random noise would not.
    """

    MU_AMPLITUDE_UV = 12.0
    MU_SUPPRESSION = 0.35          # fraction of mu left during imagery
    BLINK_AMPLITUDE_UV = 180.0
    BLINK_DELAY_S = 0.4
    BLINK_WIDTH_S = 0.08

    def __init__(self, seed: int = 0) -> None:
        self.sampling_rate = MUSE_SFREQ
        self._rng = np.random.default_rng(seed)
        self._lock = threading.Lock()
        self._next_sample_time: Optional[float] = None
        self._phase = 0.0
        self._intent: Optional[str] = None
        self._pending_markers: list[float] = []
        self._blink_times: list[float] = []

    def start(self) -> None:
        self._next_sample_time = time.time()

    def stop(self) -> None:
        self._next_sample_time = None

    def insert_marker(self, code: float) -> None:
        with self._lock:
            self._pending_markers.append(code)

    def on_phase(self, trial_type: str, phase: str) -> None:
        if trial_type in (L.TRIAL_MI_LEFT, L.TRIAL_MI_RIGHT):
            if phase == "start":
                self.set_intent("left" if trial_type == L.TRIAL_MI_LEFT else "right")
            elif phase == "end":
                self.set_intent(None)
        elif trial_type == L.TRIAL_BLINK and phase == "start":
            self.set_intent("blink")

    def set_intent(self, intent: Optional[str]) -> None:
        with self._lock:
            if intent == "blink":
                self._blink_times.append(time.time() + self.BLINK_DELAY_S)
            else:
                self._intent = intent

    def read(self) -> Optional[EEGChunk]:
        if self._next_sample_time is None:
            return None
        now = time.time()
        n = int((now - self._next_sample_time) * self.sampling_rate)
        if n <= 0:
            return None
        ts = self._next_sample_time + np.arange(n) / self.sampling_rate
        self._next_sample_time = ts[-1] + 1.0 / self.sampling_rate

        eeg = self._rng.normal(0.0, 6.0, size=(N_CHANNELS, n))
        phases = self._phase + 2 * np.pi * 10.0 * np.arange(n) / self.sampling_rate
        self._phase = float(phases[-1] + 2 * np.pi * 10.0 / self.sampling_rate)
        mu = self.MU_AMPLITUDE_UV * np.sin(phases)

        with self._lock:
            intent = self._intent
            markers_in = self._pending_markers
            self._pending_markers = []
            blink_times = [t for t in self._blink_times if t > ts[0] - 1.0]
            self._blink_times = blink_times

        left_gain = right_gain = 1.0
        if intent == "left":
            right_gain = self.MU_SUPPRESSION
        elif intent == "right":
            left_gain = self.MU_SUPPRESSION
        eeg[0] += left_gain * mu   # TP9
        eeg[3] += right_gain * mu  # TP10

        for bt in blink_times:
            bump = self.BLINK_AMPLITUDE_UV * np.exp(-0.5 * ((ts - bt) / self.BLINK_WIDTH_S) ** 2)
            eeg[1] += bump
            eeg[2] += bump

        markers = np.zeros(n)
        for i, code in enumerate(markers_in[:n]):
            markers[-(i + 1)] = code
        return EEGChunk(timestamps=ts, eeg=eeg, markers=markers)
