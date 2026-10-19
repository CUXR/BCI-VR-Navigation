"""Headless headset-fit check or deterministic CSV replay."""

import argparse
import csv
import json
from pathlib import Path
import threading
import time

import numpy as np

from .quality import CHANNELS, QualityConfig, QualityMonitor, format_quality


def replay(path, *, config=None, output=None):
    monitor = QualityMonitor(config, source="recorded_csv", muse_packets=False)
    step = max(1, round(monitor.config.sfreq * monitor.config.update_s))
    summaries = {ch: {s: 0 for s in ("good", "fair", "poor", "unknown")} for ch in CHANNELS}
    count = 0
    samples = 0
    sink = Path(output).open("w") if output else None
    try:
        with Path(path).open(newline="") as handle:
            reader = csv.DictReader(handle)
            names = list(CHANNELS) if set(CHANNELS) <= set(reader.fieldnames or []) else [f"EEG_{i}" for i in range(4)]
            if not set(names + ["timestamp"]) <= set(reader.fieldnames or []):
                raise ValueError("CSV needs timestamp and TP9/AF7/AF8/TP10 or EEG_0..EEG_3")
            batch = []
            for row in reader:
                if not any(row[name] for name in names):
                    continue  # marker-only rows in legacy recordings
                batch.append([float(row["timestamp"])] + [float(row[name]) if row[name] else np.nan for name in names])
                if len(batch) < step:
                    continue
                values = np.asarray(batch)
                samples += len(batch)
                now = samples / monitor.config.sfreq
                monitor.feed(values[:, 1:].T, values[:, 0], now=now)
                report = monitor.evaluate(now=now, wall_time=float(values[-1, 0]))
                for ch in CHANNELS:
                    summaries[ch][report["channels"][ch]["status"]] += 1
                if sink:
                    sink.write(json.dumps(report, allow_nan=False) + "\n")
                count += 1
                batch.clear()
            if batch:
                values = np.asarray(batch)
                samples += len(batch)
                now = samples / monitor.config.sfreq
                monitor.feed(values[:, 1:].T, values[:, 0], now=now)
                report = monitor.evaluate(now=now, wall_time=float(values[-1, 0]))
                for ch in CHANNELS:
                    summaries[ch][report["channels"][ch]["status"]] += 1
                if sink:
                    sink.write(json.dumps(report, allow_nan=False) + "\n")
                count += 1
    finally:
        if sink:
            sink.close()
    return {"file": str(path), "samples": samples, "reports": count, "channels": summaries}


def live(args):
    from brainflow.board_shim import BoardIds, BoardShim, BrainFlowInputParams

    board_id = BoardIds.SYNTHETIC_BOARD if args.simulate else BoardIds.MUSE_2_BOARD
    params = BrainFlowInputParams()
    if args.serial:
        params.serial_number = args.serial if args.serial.startswith("Muse-") else "Muse-" + args.serial
    board = BoardShim(board_id, params)
    monitor = QualityMonitor(QualityConfig(sfreq=BoardShim.get_sampling_rate(board_id), mains_hz=args.mains),
                             output=args.output, source="synthetic" if args.simulate else "muse",
                             muse_packets=not args.simulate)
    stop = threading.Event()

    def read():
        channels = BoardShim.get_eeg_channels(board_id)[:4]
        ts = BoardShim.get_timestamp_channel(board_id)
        packet = BoardShim.get_package_num_channel(board_id)
        while not stop.is_set():
            try:
                data = board.get_board_data()
                if data.shape[1]:
                    monitor.feed(data[channels], data[ts], data[packet] if not args.simulate else None)
                monitor.set_health(board_backlog_samples=board.get_board_data_count())
            except Exception as exc:
                monitor.set_health(reader_error=str(exc))
                stop.set()
            stop.wait(0.02)

    reader = None
    try:
        board.prepare_session()
        monitor.start()
        board.start_stream()
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        deadline = time.monotonic() + args.seconds if args.seconds else float("inf")
        while time.monotonic() < deadline and not stop.wait(0.5):
            print(format_quality(monitor.latest), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if reader:
            reader.join(timeout=3)
        try:
            if board.is_prepared():
                try:
                    board.stop_stream()
                finally:
                    board.release_session()
        finally:
            monitor.stop()
    health = monitor.latest
    if health:
        error = (health["health"]["reader_error"] or health["health"]["quality_worker_error"]
                 or monitor.output_error)
        if error:
            raise RuntimeError(error)


def main():
    parser = argparse.ArgumentParser(description="Per-electrode Muse signal quality (provisional thresholds)")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--replay", type=Path, help="Replay a recorded CSV without Bluetooth")
    source.add_argument("--simulate", action="store_true", help="Use BrainFlow synthetic data")
    parser.add_argument("--serial", help="Muse BLE name or short serial")
    parser.add_argument("--mains", type=int, choices=(50, 60), default=60)
    parser.add_argument("--sfreq", type=float, default=256, help="CSV replay sample rate only")
    parser.add_argument("--seconds", type=float, help="Stop live checking after this many seconds")
    parser.add_argument("--output", type=Path, help="Write timestamped quality JSONL")
    args = parser.parse_args()
    if args.seconds is not None and args.seconds <= 0:
        parser.error("--seconds must be positive")
    if args.replay:
        print(json.dumps(replay(args.replay, config=QualityConfig(sfreq=args.sfreq, mains_hz=args.mains), output=args.output)))
    else:
        live(args)


if __name__ == "__main__":
    main()
