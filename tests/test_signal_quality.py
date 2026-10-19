import csv
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from src.signal_quality import CHANNELS, CsvRecorder, QualityConfig, QualityMonitor
from src.signal_quality.__main__ import replay


def clean(n=512, start=0):
    t = (np.arange(n) + start) / 256
    return np.tile(20 * np.sin(2 * np.pi * 10 * t), (4, 1)), 1000 + t


class QualityTests(unittest.TestCase):
    def score(self, data, *, monitor=None, now=0, timestamps=None, packets=None):
        monitor = monitor or QualityMonitor()
        monitor.feed(data, clean(data.shape[1])[1] if timestamps is None else timestamps,
                     packets, now=now)
        return monitor.evaluate(now=now, wall_time=1000 + now)

    def test_good_channels_and_dc_offset(self):
        result = self.score(clean()[0] + 400)
        self.assertEqual(result["stream_status"], "ok")
        for info in result["channels"].values():
            self.assertEqual(info["status"], "good")
            self.assertAlmostEqual(info["rms_uv"], 20 / np.sqrt(2))

    def test_frontal_sensors_use_their_own_rms_limits(self):
        # 300 uV RMS is past the ear limit (100/200) but under the forehead limit (400/800).
        result = self.score(clean()[0] * 15)
        for ch in ("TP9", "TP10"):
            self.assertNotEqual(result["channels"][ch]["status"], "good")
            self.assertIn("high_amplitude", result["channels"][ch]["reasons"])
        for ch in ("AF7", "AF8"):
            self.assertEqual(result["channels"][ch]["status"], "good")

    def test_saturation_identifies_each_channel_without_cleaning_raw(self):
        for idx, ch in enumerate(CHANNELS):
            with self.subTest(channel=ch):
                data, _ = clean()
                data[idx, :10] = -1000
                original = data.copy()
                result = self.score(data)
                self.assertEqual(result["channels"][ch]["status"], "poor")
                self.assertIn("saturation", result["channels"][ch]["reasons"])
                self.assertTrue(all(result["channels"][other]["status"] == "good" for other in CHANNELS if other != ch))
                np.testing.assert_array_equal(data, original)

    def test_flat_line_and_invalid_values(self):
        for fault, expected in ((0, "flat_line"), (np.nan, "invalid_values"),
                                (np.inf, "invalid_values"), (1e300, "out_of_range")):
            with self.subTest(fault=fault):
                data, _ = clean()
                data[1] = fault
                with np.errstate(all="raise"):
                    result = self.score(data)
                self.assertIn(expected, result["channels"]["AF7"]["reasons"])
                self.assertEqual(result["channels"]["TP9"]["status"], "good")
                json.dumps(result, allow_nan=False)

    def test_line_noise_requires_persistence_for_both_mains_frequencies(self):
        for mains in (50, 60):
            monitor = QualityMonitor(QualityConfig(mains_hz=mains))
            data, ts = clean()
            data[2] += 80 * np.sin(2 * np.pi * mains * np.arange(512) / 256)
            for i in range(3):
                report = self.score(data, monitor=monitor, now=i * 0.5)
                self.assertEqual(report["channels"]["AF8"]["status"], "poor" if i == 2 else "fair")
            self.assertIn("line_noise", report["channels"]["AF8"]["reasons"])

    def test_brief_blink_does_not_declare_poor_contact(self):
        data, _ = clean()
        data[1:3, 100:132] += 300 * np.sin(np.linspace(0, np.pi, 32))
        report = self.score(data)
        self.assertNotEqual(report["channels"]["AF7"]["status"], "poor")
        self.assertNotEqual(report["channels"]["AF8"]["status"], "poor")

    def test_recovery_requires_new_clean_samples_for_three_seconds(self):
        monitor = QualityMonitor()
        bad, _ = clean()
        bad[0] = 1000
        self.score(bad, monitor=monitor)
        for i in range(1, 9):
            result = self.score(clean()[0], monitor=monitor, now=i * 0.5)
            self.assertEqual(result["channels"]["TP9"]["status"], "poor" if i < 7 else "good")

    def test_warmup_stale_and_reconnect(self):
        monitor = QualityMonitor()
        self.assertEqual(monitor.evaluate(now=0)["stream_reasons"], ["no_data"])
        self.score(clean(128)[0], monitor=monitor)
        self.assertEqual(monitor.latest["stream_reasons"], ["warming_up"])
        self.score(clean()[0], monitor=monitor, now=0.1)
        self.assertEqual(monitor.evaluate(now=0.7)["stream_reasons"], ["stale_stream"])
        self.score(clean(128)[0], monitor=monitor, now=0.8)
        self.assertEqual(monitor.latest["stream_reasons"], ["warming_up"])

    def test_packet_repeats_wrap_loss_and_backwards(self):
        packets = (65520 + np.arange(512) // 12) % 65536
        result = self.score(clean()[0], packets=packets)
        self.assertEqual(result["missing_fraction"], 0)
        self.assertEqual(result["stream_status"], "ok")
        lost = packets.copy()
        lost[252:] = (lost[252:] + 5) % 65536
        result = self.score(clean()[0], packets=lost)
        self.assertIn("sample_loss", result["stream_reasons"])
        self.assertTrue(all(q["status"] == "unknown" for q in result["channels"].values()))
        backwards = np.arange(512) // 12
        backwards[252:] -= 5
        self.assertIn("out_of_order_packets", self.score(clean()[0], packets=backwards)["stream_reasons"])
        self.assertIn("repeated_packets", self.score(clean()[0], packets=np.zeros(512))["stream_reasons"])

    def test_timestamp_loss_and_bad_clock(self):
        data, ts = clean()
        ts[256:] += 0.5
        self.assertEqual(self.score(data, timestamps=ts)["stream_status"], "unknown")
        ts[10] = ts[9]
        self.assertIn("invalid_timestamps", self.score(data, timestamps=ts)["stream_reasons"])

    def test_monitor_memory_is_bounded_and_slow_scoring_does_not_block_feed(self):
        monitor = QualityMonitor()
        self.score(clean(10000)[0], monitor=monitor)
        self.assertEqual(monitor.latest["samples_in_window"], 512)
        entered, release = threading.Event(), threading.Event()
        original = monitor._channel

        def slow(values, name=None):
            entered.set()
            release.wait(2)
            return original(values, name)

        with patch.object(monitor, "_channel", side_effect=slow):
            worker = threading.Thread(target=lambda: monitor.evaluate(now=0))
            worker.start()
            self.assertTrue(entered.wait(1))
            before = time.monotonic()
            monitor.feed(*clean(128), now=0.1)
            elapsed = time.monotonic() - before
            release.set()
            worker.join(2)
            self.assertLess(elapsed, 0.1)

    def test_worker_sidecar_includes_final_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quality.jsonl"
            monitor = QualityMonitor(output=path)
            monitor.start()
            monitor.feed(*clean())
            monitor.set_health(recording_error="disk full", board_backlog_samples=np.int32(42))
            monitor.stop()
            reports = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(reports[-1]["health"]["recording_error"], "disk full")
            self.assertEqual(reports[-1]["health"]["board_backlog_samples"], 42)

    def test_blocked_quality_sidecar_cannot_leave_a_green_live_snapshot(self):
        entered, release = threading.Event(), threading.Event()
        with patch("pathlib.Path.open") as open_file:
            def blocked_write(message):
                entered.set()
                release.wait(2)
            open_file.return_value.write.side_effect = blocked_write
            monitor = QualityMonitor(output="unused.jsonl")
            monitor.feed(*clean())
            monitor.start()
            try:
                self.assertTrue(entered.wait(1))
                time.sleep(1.05)
                monitor.feed(*clean())
                self.assertEqual(monitor.latest["stream_reasons"], ["quality_worker_delayed"])
            finally:
                release.set()
                monitor.stop()

    def test_replay_keeps_partial_tail_and_skips_marker_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "eeg.csv"
            with path.open("w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["timestamp"] + [f"EEG_{i}" for i in range(4)])
                data, ts = clean(513)
                writer.writerows(zip(ts, *data))
                writer.writerow([1003, "", "", "", ""])
            result = replay(path, output=Path(tmp) / "out.jsonl")
            self.assertEqual(result["samples"], 513)
            self.assertEqual(result["reports"], 5)


class RecordingTests(unittest.TestCase):
    def test_incremental_rows_and_shutdown_drain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw.csv"
            recorder = CsvRecorder(path, ["timestamp", "TP9"])
            recorder.start()
            for i in range(20):
                recorder.submit([{"timestamp": i, "TP9": -1000}])
            recorder.close()
            with path.open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 20)
            self.assertEqual([int(row["timestamp"]) for row in rows], list(range(20)))
            self.assertEqual(recorder.written_samples, 20)

    def test_slow_disk_queue_is_bounded_and_overflow_is_explicit(self):
        entered, release = threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("src.signal_quality.recording.csv.DictWriter") as factory:
                def slow(rows):
                    entered.set()
                    release.wait(2)
                factory.return_value.writerows.side_effect = slow
                recorder = CsvRecorder(Path(tmp) / "raw.csv", ["TP9"], max_batches=1)
                recorder.start()
                recorder.submit([{"TP9": 1}])
                self.assertTrue(entered.wait(1))
                recorder.submit([{"TP9": 2}])
                with self.assertRaisesRegex(RuntimeError, "queue full"):
                    recorder.submit([{"TP9": 3}])
                self.assertEqual(recorder.backlog, 1)
                self.assertEqual(recorder.rejected_samples, 1)
                release.set()
                with self.assertRaisesRegex(RuntimeError, "incomplete"):
                    recorder.close()
                self.assertEqual(recorder.written_samples, 2)

    def test_disk_error_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("src.signal_quality.recording.csv.DictWriter") as factory:
                factory.return_value.writerows.side_effect = OSError("disk full")
                recorder = CsvRecorder(Path(tmp) / "raw.csv", ["TP9"])
                recorder.start()
                recorder.submit([{"TP9": 1}])
                with self.assertRaisesRegex(RuntimeError, "disk full"):
                    recorder.close()
                self.assertEqual(recorder.written_samples, 0)


if __name__ == "__main__":
    unittest.main()
