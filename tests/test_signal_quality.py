import csv
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from src.signal_quality import (
    CHANNELS,
    CsvRecorder,
    QualityConfig,
    QualityMonitor,
    SignalQualityEngine,
    calc_isgood_feats,
    calc_psd,
    calc_var_feats,
    drlref_classification,
    dt_ear,
    dt_front,
    filter_2_hp_order2_fdesign,
    filter_36_lp_order6,
    good_data_check,
    is_epoch_good,
    is_nan_alert,
    is_noise_alert,
)
from src.signal_quality.__main__ import replay


def clean(n=512, start=0):
    t = (np.arange(n) + start) / 256
    return np.tile(20 * np.sin(2 * np.pi * 10 * t), (4, 1)), 1000 + t


class SignalQualityEngineUnitTests(unittest.TestCase):
    """Direct unit tests for the signal quality check pipeline components."""

    def test_dt_ear_exact_thresholds(self):
        # dt_ear: 2.0 = good, 1.0 = ok/fair, 0.0 = bad/poor
        self.assertEqual(dt_ear(123.0, 4.0), 0.0)
        self.assertEqual(dt_ear(122.9, 4.0), 1.0)
        self.assertEqual(dt_ear(46.0, 3.0), 1.0)
        self.assertEqual(dt_ear(45.9, 3.0), 2.0)
        self.assertEqual(dt_ear(11.2808, 2.0), 2.0)
        self.assertEqual(dt_ear(5.0421, 1.4), 2.0)
        self.assertEqual(dt_ear(3.0, 0.8221), 2.0)
        self.assertEqual(dt_ear(3.0, 0.8220), 0.0)
        self.assertEqual(dt_ear(0.0, -np.inf), 0.0)

    def test_dt_front_exact_thresholds(self):
        # dt_front: 2.0 = good, 1.0 = ok/fair, 0.0 = bad/poor
        self.assertEqual(dt_front(15.9951, 500.0, 5.0), 2.0)
        self.assertEqual(dt_front(118.9837, 100.0, 1.5), 0.0)
        self.assertEqual(dt_front(118.98, 100.0, 1.5), 1.0)
        self.assertEqual(dt_front(46.8163, 100.0, 1.5), 1.0)
        # Between 15.9952 and 46.8163: gated by prev_subwin_var >= 265.8586 and kurt >= 2.8146
        self.assertEqual(dt_front(25.0, 265.8585, 4.0), 2.0)
        self.assertEqual(dt_front(25.0, 265.8586, 2.8145), 2.0)
        self.assertEqual(dt_front(25.0, 265.8586, 2.8146), 0.0)

    def test_filter_coefficients_and_var_feats(self):
        b_hp_256, a_hp_256 = filter_2_hp_order2_fdesign(256.0)
        np.testing.assert_allclose(b_hp_256, [0.96588528974407, -1.93177057948814, 0.96588528974407])
        np.testing.assert_allclose(a_hp_256, [1.0, -1.930606427219668, 0.932934731756612])
        b_hp_220, a_hp_220 = filter_2_hp_order2_fdesign(220.0)
        np.testing.assert_allclose(b_hp_220, [0.960414643790783, -1.920829287581566, 0.960414643790783])
        np.testing.assert_allclose(a_hp_220, [1.0, -1.919261672801331, 0.922396902361801])
        b_lp36, a_lp36 = filter_36_lp_order6(256.0)
        self.assertEqual(len(b_lp36), 7)
        self.assertEqual(len(a_lp36), 7)

        t = np.arange(128) / 256.0
        sine = np.tile(20.0 * np.sin(2.0 * np.pi * 10.0 * t), (4, 1))
        std_p, log10_var, kurt = calc_var_feats(sine)
        np.testing.assert_allclose(std_p, 20.0 / np.sqrt(2.0), rtol=1e-6)
        np.testing.assert_allclose(log10_var, np.log10(200.0 * 128.0 / 127.0), rtol=1e-6)
        # Pearson kurtosis of a pure sinusoid is 1.5
        np.testing.assert_allclose(kurt, 1.5, rtol=1e-6)

    def test_good_data_check_and_alerts(self):
        t = np.arange(256) / 256.0
        quiet_sig = 10.0 * np.sin(2.0 * np.pi * 10.0 * t)  # var ~ 50 < 275
        is_quiet, sub_var = good_data_check(quiet_sig, threshold=275.0)
        self.assertEqual(is_quiet, 1.0)
        self.assertLess(sub_var, 100.0)

        loud_sig = 30.0 * np.sin(2.0 * np.pi * 10.0 * t)  # var ~ 450 >= 275
        is_quiet_loud, _ = good_data_check(loud_sig, threshold=275.0)
        self.assertEqual(is_quiet_loud, 0.0)

        nan_buf = np.zeros((768, 4))
        self.assertFalse(is_nan_alert(nan_buf))
        nan_buf[:50, :] = 1.0  # 50 / 768 = 6.51% >= 6%
        self.assertTrue(is_nan_alert(nan_buf))

        good_epochs = np.ones(100)
        self.assertEqual(is_noise_alert(good_epochs), (False, 0.0))
        good_epochs[:50] = 0.0
        self.assertEqual(is_noise_alert(good_epochs), (True, 0.5))

    def test_drlref_classification_across_models(self):
        drl_good = np.column_stack((np.full(64, 1000.0), np.full(64, 900.0)))
        self.assertTrue(drlref_classification(drl_good, muse_model=3))

        # Exceeds Muse S (model 4) diff limit 25000 but under Muse 2 (model 3) limit 200000
        drl_mid = np.column_stack((np.full(64, 100000.0), np.zeros(64)))
        self.assertTrue(drlref_classification(drl_mid, muse_model=3))
        self.assertFalse(drlref_classification(drl_mid, muse_model=4))

        # High variance on |DRL - REF| (> 87035000) disconnects even if last sample is small
        alt = np.tile([0.0, 25000.0], 32)
        alt[-1] = 0.0
        drl_noisy = np.column_stack((alt, np.zeros(64)))
        self.assertFalse(drlref_classification(drl_noisy, muse_model=3))

    def test_psd_isgood_feats_and_logistic_epoch_good(self):
        data, _ = clean(256)
        psd = calc_psd(data, srate=256.0, nfft=512)
        self.assertEqual(psd.shape, (129, 4))
        # Peak power at 10 Hz (bin index 10 at 1 Hz steps)
        for ch in range(4):
            self.assertEqual(int(np.argmax(psd[:, ch])), 10)
        feats = calc_isgood_feats(data)
        self.assertEqual(feats.shape, (28,))
        probs, flags = is_epoch_good(
            psd,
            feats,
            feat_std_544=np.ones(544),
            feat_coeff_544=np.zeros(544),
            intercept_4=np.ones(4),
            feat_mean_544=np.zeros(544),
            threshold=0.5,
        )
        self.assertTrue(np.all(flags))
        np.testing.assert_allclose(probs, 1.0 / (1.0 + np.exp(-1.0)))

    def test_coupled_vs_per_sensor_and_ear_quiet_gate(self):
        sqc = SignalQualityEngine(QualityConfig(couple_channels=True))
        data, _ = clean(512)
        # Make AF8 bad while AF7 is good: h_sensor[2] == 4.0, but h_coupled[1..2] == min(1.0, 4.0) == 1.0
        data[2] = 250.0 * np.sin(2.0 * np.pi * 10.0 * np.arange(512) / 256.0)
        sqc.process_eeg(data)
        self.assertEqual(sqc.h_sensor[1], 1.0)
        self.assertEqual(sqc.h_sensor[2], 4.0)
        self.assertEqual(sqc.h_coupled[1], 1.0)
        self.assertEqual(sqc.h_coupled[2], 1.0)


class QualityTests(unittest.TestCase):
    def score(self, data, *, monitor=None, now=0, timestamps=None, packets=None, drl_ref=None):
        # These legacy cases assert independent electrode behavior; coupling has its own tests.
        monitor = monitor or QualityMonitor(QualityConfig(couple_channels=False))
        monitor.feed(
            data,
            clean(data.shape[1])[1] if timestamps is None else timestamps,
            packets,
            drl_ref=drl_ref,
            now=now,
        )
        return monitor.evaluate(now=now, wall_time=1000 + now)

    def test_good_channels_and_dc_offset(self):
        result = self.score(clean()[0] + 400)
        self.assertEqual(result["stream_status"], "ok")
        for info in result["channels"].values():
            self.assertEqual(info["status"], "good")
            self.assertEqual(info["horseshoe"], 1.0)
            # 2-36 Hz Butterworth cascade passes 10 Hz with gain ~0.9992 (14.131 uV vs 14.142 uV raw)
            self.assertAlmostEqual(info["rms_uv"], 20 / np.sqrt(2), places=1)
            self.assertAlmostEqual(info["raw_std_uv"], 20 / np.sqrt(2), places=5)

    def test_ear_and_frontal_decision_trees_differ(self):
        # 1) Kurtosis + sub-window variance gate in dt_front (15.9952 <= std_p < 46.8163,
        # prev_sub_win_var >= 265.8586, kurt >= 2.8146) fails AF7/AF8 while TP9/TP10 pass (< 46.0 uV).
        t = np.arange(512) / 256.0
        spiky = 8.0 * np.sin(2.0 * np.pi * 10.0 * t)
        for pos in range(0, 512, 43):
            end = min(512, pos + 8)
            spiky[pos:end] += 95.0 * np.sin(2.0 * np.pi * 16.0 * np.arange(end - pos) / 256.0)
        data = np.tile(spiky, (4, 1))
        result = self.score(data)
        for ch in ("TP9", "TP10"):
            self.assertEqual(result["channels"][ch]["status"], "good")
        for ch in ("AF7", "AF8"):
            self.assertEqual(result["channels"][ch]["status"], "poor")
            self.assertIn("kurtosis_artifact", result["channels"][ch]["reasons"])

        # 2) In the 46.0..46.8163 uV band with low kurtosis (1.5 < 2.8146), dt_ear is fair (2.0)
        # while dt_front is good (1.0).
        sine_46 = np.tile(65.6 * np.sin(2.0 * np.pi * 10.0 * t), (4, 1))
        result_band = self.score(sine_46)
        for ch in ("TP9", "TP10"):
            self.assertEqual(result_band["channels"][ch]["status"], "fair")
        for ch in ("AF7", "AF8"):
            self.assertEqual(result_band["channels"][ch]["status"], "good")

    def test_saturation_identifies_each_channel_without_cleaning_raw(self):
        t = np.arange(512) / 256.0
        for idx, ch in enumerate(CHANNELS):
            with self.subTest(channel=ch):
                data, _ = clean()
                # Sustained railing in the 2-36 Hz band on a single channel
                data[idx] = np.where(np.sin(2 * np.pi * 10 * t) >= 0, 1000.0, -1000.0)
                original = data.copy()
                result = self.score(data)
                self.assertEqual(result["channels"][ch]["status"], "poor")
                self.assertIn("saturation", result["channels"][ch]["reasons"])
                self.assertTrue(
                    all(result["channels"][other]["status"] == "good" for other in CHANNELS if other != ch)
                )
                np.testing.assert_array_equal(data, original)

    def test_flat_line_and_invalid_values(self):
        for fault, expected in (
            (0, "flat_line"),
            (np.nan, "invalid_values"),
            (np.inf, "invalid_values"),
            (1e300, "out_of_range"),
        ):
            with self.subTest(fault=fault):
                data, _ = clean()
                data[1] = fault
                with np.errstate(all="raise"):
                    result = self.score(data)
                self.assertIn(expected, result["channels"]["AF7"]["reasons"])
                self.assertEqual(result["channels"]["TP9"]["status"], "good")
                json.dumps(result, allow_nan=False)

    def test_persistence_transitions_fair_then_poor(self):
        # Hysteresis state 4.0 caps initial bad epochs to fair (2.0) until
        # prop_bad_15 > 0.7 (11 of 15 epochs), then transitions to poor (4.0).
        monitor = QualityMonitor(QualityConfig(window_s=1.0, couple_channels=False))
        t = np.arange(256) / 256.0
        bad_chunk = np.tile(20.0 * np.sin(2 * np.pi * 10 * t), (4, 1))
        bad_chunk[2] = 250.0 * np.sin(2 * np.pi * 10 * t)  # std_p ~ 176 uV >= 118.9837

        report1 = self.score(bad_chunk, monitor=monitor, now=0.0, timestamps=1000.0 + t)
        self.assertEqual(report1["channels"]["AF8"]["status"], "fair")

        t2 = (np.arange(256) + 256) / 256.0
        bad_chunk2 = np.tile(20.0 * np.sin(2 * np.pi * 10 * t2), (4, 1))
        bad_chunk2[2] = 250.0 * np.sin(2 * np.pi * 10 * t2)
        report2 = self.score(bad_chunk2, monitor=monitor, now=0.4, timestamps=1000.0 + t2)
        self.assertEqual(report2["channels"]["AF8"]["status"], "poor")
        self.assertIn("high_amplitude", report2["channels"]["AF8"]["reasons"])

    def test_brief_blink_does_not_declare_poor_contact(self):
        data, _ = clean()
        data[1:3, 100:132] += 300 * np.sin(np.linspace(0, np.pi, 32))
        report = self.score(data)
        self.assertNotEqual(report["channels"]["AF7"]["status"], "poor")
        self.assertNotEqual(report["channels"]["AF8"]["status"], "poor")

    def test_recovery_follows_hold_and_streak(self):
        # When TP9 is poor (4.0), transitioning to clean data requires the 2-36 Hz filter + 128-sample
        # window to settle and the 3-epoch transition hold / good_streak state machine to complete.
        monitor = QualityMonitor()
        t = np.arange(512) / 256.0
        bad, ts = clean(512, start=0)
        bad[0] = 300.0 * np.sin(2 * np.pi * 10 * t)
        r0 = self.score(bad, monitor=monitor, now=0.0, timestamps=ts)
        self.assertEqual(r0["channels"]["TP9"]["status"], "poor")

        # One 26-sample clean step (~0.1 s) is still poor while the 128-sample window and hold clear
        clean_step, ts_step = clean(26, start=512)
        r1 = self.score(clean_step, monitor=monitor, now=0.1, timestamps=ts_step)
        self.assertEqual(r1["channels"]["TP9"]["status"], "poor")

        # After 1.0 s (256 samples) of clean data, TP9 recovers to good (1.0) and latches sensorState=1
        clean_1s, ts_1s = clean(256, start=538)
        r2 = self.score(clean_1s, monitor=monitor, now=0.5, timestamps=ts_1s)
        self.assertEqual(r2["channels"]["TP9"]["status"], "good")
        self.assertEqual(r2["channels"]["TP9"]["sensor_state"], 1)

    def test_drl_disconnection_forces_all_channels_poor(self):
        drl_bad = np.column_stack((np.full(64, 500000.0), np.zeros(64)))
        result = self.score(clean()[0], drl_ref=drl_bad)
        self.assertFalse(result["is_drl_connected"])
        for ch in CHANNELS:
            self.assertEqual(result["channels"][ch]["status"], "poor")
            self.assertIn("drl_disconnected", result["channels"][ch]["reasons"])

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
