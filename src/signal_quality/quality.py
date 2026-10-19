"""Provisional raw-signal checks; these do not measure electrode impedance."""

from collections import deque
from copy import deepcopy
from dataclasses import asdict, dataclass
import json
import logging
from pathlib import Path
import threading
import time

import numpy as np
from scipy.signal import welch

CHANNELS = ("TP9", "AF7", "AF8", "TP10")
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class QualityConfig:
    sfreq: float = 256.0
    window_s: float = 2.0
    update_s: float = 0.5
    stale_s: float = 0.5
    recovery_s: float = 3.0
    mains_hz: int = 60
    rail_uv: float = 999.0
    clip_fraction: float = 0.01
    flat_std_uv: float = 0.5
    rms_warn_uv: float = 100.0
    rms_bad_uv: float = 200.0
    line_warn_fraction: float = 0.25
    line_bad_fraction: float = 0.50
    loss_warn_fraction: float = 0.01
    loss_bad_fraction: float = 0.05

    def __post_init__(self):
        if self.sfreq <= 2 * (self.mains_hz + 2) or self.mains_hz not in (50, 60):
            raise ValueError("Use 50/60 Hz mains and a sampling rate above the mains band")
        if min(self.window_s, self.update_s, self.stale_s, self.recovery_s) <= 0:
            raise ValueError("Window, update, stale and recovery durations must be positive")
        if not (0 < self.flat_std_uv < self.rms_warn_uv < self.rms_bad_uv < self.rail_uv):
            raise ValueError("Amplitude thresholds must be ordered and positive")
        if not (0 <= self.line_warn_fraction < self.line_bad_fraction <= 1
                and 0 <= self.loss_warn_fraction < self.loss_bad_fraction <= 1
                and 0 <= self.clip_fraction <= 1):
            raise ValueError("Fraction thresholds must be ordered within [0, 1]")


class QualityMonitor:
    """One producer feeds batches; a worker scores bounded snapshots at 2 Hz.

    feed() never filters, computes spectra, writes files, or calls BrainFlow.
    evaluate() is also available without a worker for deterministic replay/tests.
    Packet counters are Muse's 16-bit counter repeated for 12 EEG samples.
    """

    def __init__(self, config=None, *, output=None, source="muse", muse_packets=True):
        self.config = config or QualityConfig()
        self.source = source
        self.muse_packets = muse_packets
        self.output = Path(output) if output is not None else None
        self._capacity = round(self.config.sfreq * self.config.window_s)
        self._eeg = np.empty((4, 0))
        self._timestamps = np.empty(0)
        self._packets = np.empty(0)
        self._arrival = None
        self._generation = 0
        self._scored_generation = -1
        self._history = {ch: deque(maxlen=4) for ch in CHANNELS}
        self._state = {ch: "unknown" for ch in CHANNELS}
        self._clean_since = {ch: None for ch in CHANNELS}
        self._state_reasons = {ch: [] for ch in CHANNELS}
        self._seen = {ch: False for ch in CHANNELS}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._health = {"reader_error": None, "recording_error": None,
                        "recording_backlog_batches": 0, "rejected_recording_samples": 0,
                        "board_backlog_samples": None, "quality_worker_error": None}
        self._report = None
        self._reported_at = None
        self.output_error = None

    def feed(self, eeg, timestamps, packets=None, *, now=None):
        eeg = np.asarray(eeg, dtype=np.float64)
        timestamps = np.asarray(timestamps, dtype=np.float64)
        if eeg.ndim != 2 or eeg.shape[0] != 4 or timestamps.shape != (eeg.shape[1],):
            raise ValueError("Expected EEG (4, n) and timestamps (n,)")
        if packets is not None and np.shape(packets) != timestamps.shape:
            raise ValueError("Packet counters must match timestamps")
        if not eeg.shape[1]:
            return
        packet_values = (np.asarray(packets, dtype=float) if packets is not None
                         else np.full(timestamps.size, np.nan))
        with self._lock:
            # Discard pre-disconnection history; recovery requires fresh data.
            arrival = time.monotonic() if now is None else now
            if self._arrival is not None and arrival - self._arrival > self.config.stale_s:
                self._eeg = self._eeg[:, :0]
                self._timestamps = self._timestamps[:0]
                self._packets = self._packets[:0]
            self._eeg = np.concatenate((self._eeg, eeg[:, -self._capacity:]), axis=1)[:, -self._capacity:]
            self._timestamps = np.concatenate((self._timestamps, timestamps[-self._capacity:]))[-self._capacity:]
            self._packets = np.concatenate((self._packets, packet_values[-self._capacity:]))[-self._capacity:]
            self._arrival = arrival
            self._generation += 1

    def set_health(self, **values):
        if values.keys() - self._health.keys():
            raise ValueError("Unknown acquisition health field")
        with self._lock:
            self._health.update({key: value.item() if isinstance(value, np.generic) else value
                                 for key, value in values.items()})

    @property
    def latest(self):
        with self._lock:
            report = deepcopy(self._report)
            arrival, reported_at = self._arrival, self._reported_at
        # A blocked sidecar must not leave a permanently green live indicator.
        if report is not None and self._thread is not None:
            now = time.monotonic()
            reason = None
            if arrival is not None and now - arrival > self.config.stale_s:
                reason = "stale_stream"
            elif reported_at is not None and now - reported_at > 2 * self.config.update_s:
                reason = "quality_worker_delayed"
            if reason:
                report["stream_status"], report["stream_reasons"] = "unknown", [reason]
                report["last_sample_age_s"] = now - arrival if arrival is not None else None
                for channel in report["channels"].values():
                    channel["status"], channel["reasons"] = "unknown", [reason]
        return report

    def _stream_health(self, ts, packets):
        c = self.config
        reasons = []
        if not np.isfinite(ts).all() or (ts <= 0).any() or (np.diff(ts) <= 0).any():
            return "unknown", ["invalid_timestamps"], None, "unavailable"
        if self.muse_packets and np.isfinite(packets).all():
            if (packets < 0).any() or (packets > 65535).any() or (packets != np.floor(packets)).any():
                return "unknown", ["invalid_packet_counter"], None, "packet_counter"
            # Repeated values inside a 12-sample packet are expected.
            boundaries = np.r_[0, np.flatnonzero(np.diff(packets)) + 1, len(packets)]
            if (np.diff(boundaries) > 12).any():
                return "unknown", ["repeated_packets"], None, "packet_counter"
            changes = np.diff(packets.astype(np.int64)) % 65536
            changes = changes[changes != 0]
            if (changes > 32768).any():
                return "unknown", ["out_of_order_packets"], None, "packet_counter"
            missing = int(np.maximum(changes - 1, 0).sum()) * 12
            method = "packet_counter"
        else:
            # Timestamps are only an estimate; Muse drivers may smooth BLE jitter.
            missing = int(np.maximum(np.rint(np.diff(ts) * c.sfreq) - 1, 0).sum())
            method = "timestamps"
        loss = missing / (len(ts) + missing)
        if loss > c.loss_bad_fraction:
            status, reasons = "unknown", ["sample_loss"]
        elif loss > c.loss_warn_fraction:
            status, reasons = "fair", ["sample_loss"]
        else:
            status = "ok"
        return status, reasons, loss, method

    def _channel(self, values):
        c = self.config
        if not np.isfinite(values).all():
            return "unknown", ["invalid_values"], True, {"invalid_fraction": float(np.mean(~np.isfinite(values)))}
        # Reject impossible decoder output before squaring (including finite 1e300).
        if np.max(np.abs(values)) > 1001:
            return "unknown", ["out_of_range"], True, {}
        std = float(np.std(values))
        clipped = float(np.mean(np.abs(values) >= c.rail_uv))
        freq, psd = welch(values, fs=c.sfreq, nperseg=min(len(values), round(c.sfreq)))
        total = psd[(freq >= 1) & (freq <= 90)].sum()
        line = float(psd[np.abs(freq - c.mains_hz) <= 2].sum() / total) if total > 0 else 0.0
        metrics = {"rms_uv": std, "std_uv": std, "peak_to_peak_uv": float(np.ptp(values)),
                   "clipped_fraction": clipped, "line_noise_fraction": line, "invalid_fraction": 0.0}
        if clipped > c.clip_fraction:
            return "poor", ["saturation"], True, metrics
        if std < c.flat_std_uv:
            return "poor", ["flat_line"], True, metrics
        reasons = []
        if std > c.rms_warn_uv:
            reasons.append("high_amplitude")
        if line > c.line_warn_fraction:
            reasons.append("line_noise")
        status = "poor" if std > c.rms_bad_uv or line > c.line_bad_fraction else "fair" if reasons else "good"
        return status, reasons, False, metrics

    def evaluate(self, *, now=None, wall_time=None):
        now = time.monotonic() if now is None else now
        c = self.config
        with self._lock:
            eeg, ts, packets = self._eeg.copy(), self._timestamps.copy(), self._packets.copy()
            arrival, generation, health = self._arrival, self._generation, self._health.copy()
        status, reasons, loss, method = "unknown", ["warming_up"], None, "unavailable"
        fresh = arrival is not None and now - arrival <= c.stale_s
        if not fresh:
            reasons = ["stale_stream" if arrival is not None else "no_data"]
        elif len(ts) >= self._capacity:
            status, reasons, loss, method = self._stream_health(ts, packets)
        if health["reader_error"]:
            status, reasons = "unknown", reasons + ["reader_error"]
        if health["quality_worker_error"]:
            status, reasons = "unknown", reasons + ["quality_worker_error"]
        if health["recording_error"]:
            reasons = reasons + ["recording_error"]
            if status == "ok":
                status = "fair"
        report = {"type": "signal_quality", "version": 1,
                  "timestamp": time.time() if wall_time is None else wall_time,
                  "source": self.source, "thresholds": asdict(c),
                  "window_start": float(ts[0]) if len(ts) and np.isfinite(ts[0]) else None,
                  "window_end": float(ts[-1]) if len(ts) and np.isfinite(ts[-1]) else None,
                  "stream_status": status, "stream_reasons": reasons,
                  "missing_fraction": loss, "loss_estimate_method": method,
                  "last_sample_age_s": max(0.0, now - arrival) if arrival is not None else None,
                  "samples_in_window": len(ts), "health": health, "channels": {}}
        report["quality_output_error"] = self.output_error
        for i, ch in enumerate(CHANNELS):
            metrics = {}
            candidate, why = "unknown", reasons[:]
            if status != "unknown":
                candidate, why, immediate, metrics = self._channel(eeg[i])
                if generation != self._scored_generation:
                    self._history[ch].append(candidate)
                    if immediate:
                        self._state[ch], self._state_reasons[ch] = candidate, why
                        self._clean_since[ch] = None
                    elif candidate != "good":
                        self._clean_since[ch] = None
                        persistent = sum(s == "poor" for s in self._history[ch]) >= 3
                        self._state[ch] = "poor" if persistent or self._state[ch] == "poor" else "fair"
                        self._state_reasons[ch] = why
                    else:
                        if self._clean_since[ch] is None:
                            self._clean_since[ch] = now
                        if not self._seen[ch] or now - self._clean_since[ch] >= c.recovery_s:
                            self._state[ch], self._state_reasons[ch] = "good", []
                    self._seen[ch] = True
                candidate, why = self._state[ch], self._state_reasons[ch]
            else:
                self._state[ch], self._state_reasons[ch] = "unknown", why
                self._history[ch].clear()
                self._clean_since[ch] = None
            report["channels"][ch] = {"status": candidate, "reasons": why, **metrics}
        self._scored_generation = generation
        with self._lock:
            self._report = report
            self._reported_at = time.monotonic()
        return deepcopy(report)

    def start(self):
        if self._thread is not None:
            raise RuntimeError("Quality monitor already started")
        # Fail before acquisition if the requested sidecar cannot be opened.
        output = self.output.open("w") if self.output is not None else None
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(output,), daemon=True)
        self._thread.start()

    def _run(self, output):
        try:
            while True:
                stopping = self._stop.is_set()
                report = self.evaluate()
                if output is not None:
                    try:
                        output.write(json.dumps(report, allow_nan=False) + "\n")
                        output.flush()
                    except OSError as exc:
                        self.output_error = str(exc)
                        log.exception("Quality sidecar failed; live monitoring continues")
                        output.close()
                        output = None
                if stopping:
                    break
                self._stop.wait(self.config.update_s)
        except Exception as exc:
            self.set_health(quality_worker_error=str(exc))
            log.exception("Quality worker failed")
            with self._lock:
                self._report = {"type": "signal_quality", "version": 1, "timestamp": time.time(),
                                "stream_status": "unknown", "stream_reasons": ["quality_worker_error"],
                                "health": self._health.copy(), "channels": {
                                    ch: {"status": "unknown", "reasons": ["quality_worker_error"]}
                                    for ch in CHANNELS}}
        finally:
            if output is not None:
                output.close()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise RuntimeError("Quality worker did not stop")


def format_quality(report):
    if report is None:
        return "Signal quality: warming up"
    channels = " | ".join(
        f"{ch}: {item['status']}" + (f" ({', '.join(item['reasons'])})" if item['reasons'] else "")
        for ch, item in report["channels"].items())
    reasons = f" ({', '.join(report['stream_reasons'])})" if report["stream_reasons"] else ""
    return f"Stream: {report['stream_status']}{reasons} | {channels}"
