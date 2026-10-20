"""Connect to a Muse 2 and stream connection state plus per-electrode quality to the macOS app.

The quality rule is the team's `src.signal_quality.QualityMonitor` (see docs/signal-quality.md);
this script only owns the BrainFlow connection and the line protocol the app reads on stdout:

    {"event": "status", "state": "connecting", "serial": "Muse-15C3", "attempt": 1}
    {"event": "status", "state": "connected", "serial": "Muse-15C3"}
    {"event": "quality", "stream_status": "ok", "stream_reasons": [],
     "sensors": {"TP9": {"quality": "good", "reasons": [], "rms_uv": 14.2, "ready_progress": 0.67}, ...}}
    {"event": "status", "state": "signal_lost"}
    {"event": "error", "message": "..."}
    {"event": "status", "state": "disconnected"}

Sensor quality is the monitor's own vocabulary: good, fair, poor, unknown.
The headband is released on SIGTERM/SIGINT so the PsychoPy recorder can connect next
(the Muse 2 accepts one Bluetooth connection at a time).

Usage, from the repo root:
    python macos_app/backend/muse_signal_check.py --serial Muse-15C3
"""

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from brainflow.board_shim import BoardIds, BoardShim, BrainFlowError, BrainFlowInputParams  # noqa: E402

from src.signal_quality import CHANNELS, QualityConfig, QualityMonitor  # noqa: E402

BOARD_ID = BoardIds.MUSE_2_BOARD.value
UPDATE_S = 0.5
GIVE_UP_S = 12.0
# Fit check passes a sensor only after this many seconds of uninterrupted good readings.
READY_FILL_S = 3.0


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, allow_nan=False), flush=True)


class ReadyConveyor:
    """Per-sensor fill toward ready.

    Progress grows with time spent good and drops to zero on any reading that is not good,
    so a sensor is ready (progress 1.0) only after READY_FILL_S of steady contact.
    """

    def __init__(self, fill_s=READY_FILL_S):
        self.fill_s = fill_s
        self.good_since = {}

    def update(self, statuses, now):
        progress = {}
        for name, status in statuses.items():
            if status == "good":
                start = self.good_since.setdefault(name, now)
                progress[name] = min(1.0, (now - start) / self.fill_s)
            else:
                self.good_since.pop(name, None)
                progress[name] = 0.0
        return progress

    def reset(self):
        self.good_since.clear()


def connect(serial, attempts, timeout):
    for attempt in range(1, attempts + 1):
        emit("status", state="connecting", serial=serial, attempt=attempt)
        params = BrainFlowInputParams()
        params.serial_number = serial
        params.timeout = timeout
        board = BoardShim(BOARD_ID, params)
        try:
            board.prepare_session()
            return board
        except BrainFlowError as error:
            try:
                board.release_session()
            except BrainFlowError:
                pass
            if attempt == attempts:
                raise RuntimeError(
                    f"Could not connect to {serial}. Make sure it is turned on, nearby, "
                    f"and not connected to another app. ({error})"
                ) from error
            time.sleep(1)
    return None


def sensor_payload(report, conveyor, now):
    quality_rank = {"good": 0, "fair": 1, "poor": 2, "unknown": 3}
    statuses = {}
    for name in CHANNELS:
        item = report["channels"].get(name, {})
        # Coupling cannot make a poor electrode ready or bypass raw/transport safeguards.
        statuses[name] = max(
            item.get("status", "unknown"), item.get("electrode_status", "unknown"),
            key=quality_rank.__getitem__,
        )
    progress = conveyor.update(statuses, now)
    sensors = {}
    for name in CHANNELS:
        item = report["channels"].get(name, {})
        rms = item.get("rms_uv")
        sensors[name] = {
            "quality": statuses[name],
            "sqc_quality": item.get("sqc_status", "unknown"),
            "electrode_quality": item.get("electrode_status", "unknown"),
            "reasons": item.get("reasons", []),
            "rms_uv": None if rms is None else round(rms, 1),
            "ready_progress": round(progress[name], 2),
        }
    return sensors


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--serial", required=True, help="Muse Bluetooth name, e.g. Muse-15C3")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=15, help="BLE discovery timeout per attempt, seconds")
    parser.add_argument("--mains", type=int, choices=(50, 60), default=60)
    args = parser.parse_args()

    BoardShim.disable_board_logger()
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    parent = os.getppid()

    board = None
    monitor = None
    reader = None
    try:
        board = connect(args.serial, args.attempts, args.timeout)
        if stopping.is_set():
            return 0
        monitor = QualityMonitor(QualityConfig(sfreq=BoardShim.get_sampling_rate(BOARD_ID), mains_hz=args.mains),
                                 source="muse", muse_packets=True)
        monitor.start()
        board.start_stream()
        emit("status", state="connected", serial=args.serial)

        eeg_channels = BoardShim.get_eeg_channels(BOARD_ID)[:4]
        timestamp_channel = BoardShim.get_timestamp_channel(BOARD_ID)
        packet_channel = BoardShim.get_package_num_channel(BOARD_ID)
        last_data = [time.monotonic()]

        # Same reader shape as `python -m src.signal_quality`: drain the board and feed the monitor.
        def read():
            while not stopping.is_set():
                try:
                    data = board.get_board_data()
                    if data.shape[1]:
                        monitor.feed(data[eeg_channels], data[timestamp_channel], data[packet_channel])
                        last_data[0] = time.monotonic()
                    monitor.set_health(board_backlog_samples=board.get_board_data_count())
                except Exception as exc:  # reported through the monitor's health fields
                    monitor.set_health(reader_error=str(exc))
                    stopping.set()
                stopping.wait(0.02)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()

        lost = False
        conveyor = ReadyConveyor()
        # Stop if the app quits without signalling us (we get re-parented).
        while not stopping.wait(UPDATE_S) and os.getppid() == parent:
            report = monitor.latest
            if report is None:
                continue
            silent_for = time.monotonic() - last_data[0]
            if silent_for > GIVE_UP_S:
                raise RuntimeError(f"Lost the signal from {args.serial}. Check that it is still on and charged.")
            stale = "stale_stream" in report["stream_reasons"] or "no_data" in report["stream_reasons"]
            if stale and silent_for > 3.0:
                if not lost:
                    lost = True
                    conveyor.reset()
                    emit("status", state="signal_lost")
                continue
            if lost:
                lost = False
                emit("status", state="connected", serial=args.serial)
            emit("quality", stream_status=report["stream_status"], stream_reasons=report["stream_reasons"],
                 reference_status=report.get("reference_status", "unavailable"),
                 sqc=report.get("sqc", {}),
                 sensors=sensor_payload(report, conveyor, time.monotonic()))

        error = monitor.latest and monitor.latest["health"].get("reader_error")
        if error:
            raise RuntimeError(f"The headband stream stopped: {error}")
        return 0
    except RuntimeError as error:
        emit("error", message=str(error))
        return 2
    finally:
        stopping.set()
        if reader is not None:
            reader.join(timeout=3)
        if board is not None:
            try:
                if board.is_prepared():
                    try:
                        board.stop_stream()
                    except BrainFlowError:
                        pass
                    board.release_session()
            except BrainFlowError:
                pass
        if monitor is not None:
            try:
                monitor.stop()
            except RuntimeError:
                pass
        emit("status", state="disconnected")


if __name__ == "__main__":
    sys.exit(main())
