"""Abstraction layer for live Muse EEG input.

Provides two interchangeable backends:
    MuseBrainFlowStream  — real Muse 2 via BrainFlow BLE
    MockMuseStream       — synthetic sinusoidal EEG for offline testing

Both expose the same interface so the rest of the pipeline does not
need to know which one is active.
"""

import time
import logging
import threading
import sys

import numpy as np

from config import (
    N_CHANNELS, SFREQ, PROJECT_ROOT,
    MUSE_2_BOARD_ID, SYNTHETIC_BOARD_ID,
)
from buffer import RingBuffer

sys.path.insert(0, str(PROJECT_ROOT))
from src.signal_quality import QualityConfig, QualityMonitor

log = logging.getLogger(__name__)


class BaseStream:
    """Common interface for EEG stream sources."""

    def start(self, buf: RingBuffer):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    @property
    def is_running(self) -> bool:
        raise NotImplementedError


# ═══════════════════════════════════════════════════════════════════
# Real Muse 2 via BrainFlow
# ═══════════════════════════════════════════════════════════════════

class MuseBrainFlowStream(BaseStream):
    """Connects to a Muse 2 headband using BrainFlow's BLE backend.

    Pushes incoming samples into the shared RingBuffer on a background
    polling thread.
    """

    def __init__(self, serial: str | None = None, simulate: bool = False):
        self._serial = f"Muse-{serial}" if serial and not serial.startswith("Muse-") else serial
        self._simulate = simulate
        self._board = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._eeg_channels: list[int] = []
        self._sr: int = SFREQ
        self.quality = None

    def start(self, buf: RingBuffer):
        from brainflow.board_shim import BoardShim, BrainFlowInputParams, BrainFlowPresets

        params = BrainFlowInputParams()
        if self._serial:
            params.serial_number = self._serial

        board_id = SYNTHETIC_BOARD_ID if self._simulate else MUSE_2_BOARD_ID
        label = "SYNTHETIC" if self._simulate else "Muse 2"

        self._board = BoardShim(board_id, params)
        self._board.prepare_session()
        self._board.start_stream()

        self._sr = BoardShim.get_sampling_rate(board_id)
        self._eeg_channels = BoardShim.get_eeg_channels(board_id)
        log.info("%s stream started: %d EEG channels @ %d Hz",
                 label, len(self._eeg_channels), self._sr)

        self.quality = QualityMonitor(
            QualityConfig(sfreq=self._sr), source="synthetic" if self._simulate else "muse",
            muse_packets=not self._simulate)
        self.quality.start()
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop, args=(buf,), daemon=True)
        self._thread.start()

    def _poll_loop(self, buf: RingBuffer):
        """Pull new samples from BrainFlow and push into the ring buffer."""
        from brainflow.board_shim import BrainFlowPresets

        from brainflow.board_shim import BoardShim

        board_id = SYNTHETIC_BOARD_ID if self._simulate else MUSE_2_BOARD_ID
        timestamp = BoardShim.get_timestamp_channel(board_id)
        packet = BoardShim.get_package_num_channel(board_id)
        try:
            while self._running:
                data = self._board.get_board_data(preset=BrainFlowPresets.DEFAULT_PRESET)
                if data.shape[1] > 0:
                    eeg = data[self._eeg_channels[:N_CHANNELS]].copy()
                    buf.push(eeg)
                    self.quality.feed(eeg, data[timestamp], data[packet] if not self._simulate else None)
                self.quality.set_health(board_backlog_samples=self._board.get_board_data_count())
                time.sleep(0.02)
        except Exception as exc:
            self.quality.set_health(reader_error=str(exc))
            log.exception("EEG reader stopped")
            self._running = False

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                raise RuntimeError("EEG reader did not stop; board remains owned by reader")
        if self._board:
            try:
                self._board.stop_stream()
                self._board.release_session()
            except Exception:
                pass
        if self.quality is not None:
            self.quality.stop()
        log.info("BrainFlow stream stopped")

    @property
    def is_running(self) -> bool:
        return self._running


# ═══════════════════════════════════════════════════════════════════
# Mock stream for offline testing
# ═══════════════════════════════════════════════════════════════════

class MockMuseStream(BaseStream):
    """Generates synthetic EEG on a background thread.

    Produces alpha-band (10 Hz) + noise on all 4 channels at 256 Hz.
    Periodically injects a large amplitude "blink" artefact on the
    frontal channels to test the blink detector.
    """

    def __init__(self, sfreq: int = SFREQ, inject_blinks: bool = True):
        self._sfreq = sfreq
        self._inject_blinks = inject_blinks
        self.quality = None
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self, buf: RingBuffer):
        self.quality = QualityMonitor(QualityConfig(sfreq=self._sfreq), source="mock", muse_packets=False)
        self.quality.start()
        self._running = True
        self._thread = threading.Thread(
            target=self._generate_loop, args=(buf,), daemon=True)
        self._thread.start()
        log.info("Mock EEG stream started (%d Hz)", self._sfreq)

    def _generate_loop(self, buf: RingBuffer):
        rng = np.random.default_rng(42)
        t = 0.0
        timestamp_start = time.time()
        sample_count = 0
        dt = 1.0 / self._sfreq
        batch = 8                  # push 8 samples at a time
        blink_interval = 5.0       # inject blink every 5 s
        next_blink = blink_interval

        while self._running:
            samples = np.zeros((N_CHANNELS, batch), dtype=np.float64)
            for i in range(batch):
                alpha = 20.0 * np.sin(2 * np.pi * 10.0 * t)  # 10 Hz alpha
                noise = rng.normal(0, 5, size=N_CHANNELS)
                samples[:, i] = alpha + noise

                # Inject blink artefact on frontal channels
                if self._inject_blinks and t >= next_blink and t < next_blink + 0.15:
                    blink_amp = 300.0 * np.sin(2 * np.pi * 3.0 * (t - next_blink) / 0.15)
                    samples[1, i] += blink_amp   # AF7
                    samples[2, i] += blink_amp   # AF8

                if t >= next_blink + 0.15:
                    next_blink += blink_interval

                t += dt

            buf.push(samples)
            timestamps = timestamp_start + (sample_count + np.arange(batch)) / self._sfreq
            self.quality.feed(samples, timestamps)
            sample_count += batch
            time.sleep(batch * dt)  # real-time pacing

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        if self.quality is not None:
            self.quality.stop()
        log.info("Mock stream stopped")

    @property
    def is_running(self) -> bool:
        return self._running
