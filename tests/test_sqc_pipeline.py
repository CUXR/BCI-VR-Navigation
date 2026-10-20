"""Signal-quality pipeline contracts and integration checks."""

import json
from pathlib import Path
import tempfile
import time
import unittest

import numpy as np
from scipy.special import expit

from src.signal_quality import (
    EpochClassifier, QualityConfig, QualityMonitor, SignalQualityEngine,
    calc_isgood_feats, calc_psd, filter_data_dfiit, is_epoch_good,
    drlref_classification,
)
from macos_app.backend.muse_signal_check import ReadyConveyor, sensor_payload


def eeg(n=2048):
    t = np.arange(n) / 256.0
    return np.tile(20 * np.sin(2 * np.pi * 10 * t), (4, 1))


def classifier():
    # Test fixture only: these are not recovered Muse model parameters.
    return EpochClassifier(np.ones(544), np.zeros(544), np.full(4, 2.0),
                           np.zeros(544), lambda p: p, "unit-test identity remap")


class NoisePipelineTests(unittest.TestCase):
    def test_live_epoch_computes_psd_and_notched_features(self):
        engine = SignalQualityEngine()
        engine.process_eeg(eeg(518))  # Ends exactly on a 26-sample epoch boundary.
        np.testing.assert_allclose(engine.psd, calc_psd(engine.eeg_buffer.latest(256)))
        np.testing.assert_allclose(engine.isgood_feats,
                                   calc_isgood_feats(engine.eeg_notch_buffer.latest(256)))
        self.assertTrue(np.all(np.argmax(engine.psd, axis=0) == 10))
        self.assertGreater(np.linalg.norm(engine.isgood_feats), 0)

    def test_classifier_preserves_channel_and_feature_layout(self):
        psd = np.ones((129, 4))
        psd[10] = 10.0 ** np.arange(1, 5)
        coeff = np.zeros((136, 4))
        coeff[10] = 1
        probs, _ = is_epoch_good(psd, np.zeros(28), np.ones(544), coeff.ravel(),
                                 np.zeros(4), np.zeros(544))
        np.testing.assert_allclose(probs, expit(np.arange(1, 5)))

    def test_classifier_branch_runs_and_accumulates_three_probabilities(self):
        model = classifier()
        engine = SignalQualityEngine(QualityConfig(use_epoch_classifier=True), epoch_classifier=model)
        engine.process_drl(np.zeros((2, 64)))
        engine.process_eeg(eeg())
        self.assertEqual(engine.classifier_status, "active")
        np.testing.assert_allclose(engine.epoch_probabilities, expit(2.0) ** 3)
        self.assertTrue(np.all(engine.good_data == 1))

    def test_classifier_requested_without_assets_is_explicitly_unavailable(self):
        engine = SignalQualityEngine(QualityConfig(use_epoch_classifier=True))
        engine.process_eeg(eeg(512))
        self.assertEqual(engine.classifier_status, "unavailable")
        self.assertIsNone(engine.epoch_probabilities)
        np.testing.assert_array_equal(engine.good_data, 0)

    def test_noisy_raw_psd_reaches_classifier_even_when_horseshoe_is_good(self):
        coeff = np.zeros((136, 4))
        coeff[60] = -1
        model = EpochClassifier(np.ones(544), coeff.ravel(), np.zeros(4), np.zeros(544),
                                lambda p: p, "unit-test 60-Hz detector")
        engine = SignalQualityEngine(QualityConfig(use_epoch_classifier=True), epoch_classifier=model)
        engine.process_drl(np.zeros((2, 64)))
        engine.process_eeg(eeg())
        self.assertTrue(np.all(engine.good_data == 1))
        t = np.arange(2048) / 256
        noisy = eeg() + 200 * np.sin(2 * np.pi * 60 * t)
        engine.process_eeg(noisy)
        self.assertTrue(np.all(engine.h_coupled[1:3] == 1))
        self.assertTrue(np.all(engine.good_data == 0))
        self.assertTrue(np.all(engine.epoch_probabilities < 0.5))

    def test_classifier_parameters_and_remapping_are_validated(self):
        with self.assertRaises(ValueError):
            EpochClassifier(np.zeros(544), np.zeros(544), np.zeros(4), np.zeros(544),
                            lambda p: p, "invalid zero scales")
        model = classifier()
        with self.assertRaises(ValueError):
            model.coeff[0] = 10
        with self.assertRaises(ValueError):
            is_epoch_good(np.ones((129, 4)), np.zeros(28), np.ones(544), np.zeros(544),
                          np.zeros(4), np.zeros(544), remap=lambda p: np.full(4, np.nan))

    def test_conditional_ear_coupling_does_not_hide_a_poor_ear(self):
        engine = SignalQualityEngine()
        data = eeg()
        data[0] *= 15
        engine.process_eeg(data)
        self.assertEqual(engine.h_sensor[0], 4)
        self.assertEqual(engine.h_coupled[0], 4)
        self.assertEqual(engine.h_coupled[3], engine.h_sensor[3])

    def test_quiet_gate_requires_horseshoe_and_drl(self):
        engine = SignalQualityEngine()
        engine.eeg2_36_buffer.update_chunk(eeg(256) / 2)
        engine.h_coupled[:] = [1, 2, 4, 1]
        engine.is_drl_connect = True
        engine._calc_noise()
        np.testing.assert_array_equal(engine.good_data, [1, 0, 0, 1])
        engine.is_drl_connect = False
        engine._calc_noise()
        np.testing.assert_array_equal(engine.good_data, 0)
        engine.is_drl_connect = True
        engine.h_coupled[:] = 1
        engine.eeg2_36_buffer.update_chunk(eeg(256) * 5)
        engine._calc_noise()
        np.testing.assert_array_equal(engine.good_data, 0)

    def test_coupled_output_preserves_bad_independent_electrode(self):
        data = eeg(512)
        data[2] *= 12.5
        monitor = QualityMonitor()
        monitor.feed(data, 1000 + np.arange(512) / 256, now=0)
        report = monitor.evaluate(now=0)
        self.assertEqual(report["channels"]["AF8"]["electrode_status"], "poor")
        self.assertEqual(report["channels"]["AF8"]["sqc_eeg_status"], "good")
        self.assertEqual(report["channels"]["AF8"]["sqc_status"], "unknown")
        self.assertEqual(report["channels"]["AF8"]["horseshoe_raw"], 4)

    def test_clipping_extension_cannot_be_hidden_by_coupling_or_filtering(self):
        data = eeg(512)
        data[1] = np.clip(995 + data[1], -1000, 1000)
        monitor = QualityMonitor()
        monitor.feed(data, 1000 + np.arange(512) / 256, now=0)
        item = monitor.evaluate(now=0)["channels"]["AF7"]
        self.assertEqual(item["status"], "poor")
        self.assertIn("saturation", item["safeguard_reasons"])
        self.assertEqual(item["sqc_eeg_status"], "good")

    def test_chunk_boundaries_do_not_change_filters_features_or_state(self):
        data = eeg()
        whole, chunks = SignalQualityEngine(), SignalQualityEngine()
        whole.process_eeg(data)
        for start in range(0, data.shape[1], 17):
            chunks.process_eeg(data[:, start:start + 17])
        for attr in ("std_uv", "psd", "isgood_feats", "h_sensor", "h_coupled", "hold_counter"):
            np.testing.assert_allclose(getattr(whole, attr), getattr(chunks, attr), atol=1e-10)
        whole.reset()
        whole.process_eeg(data)
        np.testing.assert_array_equal(whole.h_coupled, chunks.h_coupled)

    def test_dfiit_matches_explicit_recurrence_with_carried_state(self):
        b, a = [0.2, 0.4, 0.2], [1, -0.3, 0.1]
        data = eeg(512)
        actual, state = filter_data_dfiit(np.zeros((4, 2)), data[:, :111], b, a)
        tail, _ = filter_data_dfiit(state, data[:, 111:], b, a)
        expected = np.zeros_like(data)
        z = np.zeros((4, 2))
        for i in range(data.shape[1]):
            y = b[0] * data[:, i] + z[:, 0]
            z[:, 0] = b[1] * data[:, i] + z[:, 1] - a[1] * y
            z[:, 1] = b[2] * data[:, i] - a[2] * y
            expected[:, i] = y
        np.testing.assert_allclose(np.concatenate((actual, tail), axis=1), expected)


class ReferenceInputTests(unittest.TestCase):
    def feed(self, monitor, index, drl=None):
        t = (np.arange(128) + index * 128) / 256
        monitor.feed(eeg(128), 1000 + t, drl_ref=drl, now=index * 0.5)
        return monitor.evaluate(now=index * 0.5)

    def test_absent_reference_is_never_reported_connected(self):
        monitor = QualityMonitor()
        for index in range(40):
            report = self.feed(monitor, index)
        self.assertIsNone(report["is_drl_connected"])
        self.assertEqual(report["reference_status"], "unavailable")
        self.assertEqual(report["sqc"]["status"], "eeg_only")
        self.assertIsNone(report["sqc"]["good_data"])
        self.assertTrue(all(c["sqc_status"] == "unknown" for c in report["channels"].values()))
        self.assertTrue(all(c["status"] == "good" for c in report["channels"].values()))
        json.dumps(report, allow_nan=False)

    def test_reference_warmup_disconnection_invalid_and_recovery(self):
        engine = SignalQualityEngine()
        self.assertIsNone(engine.process_drl(np.zeros((2, 1))))
        self.assertEqual(engine.reference_status, "warming_up")
        self.assertTrue(engine.process_drl(np.zeros((2, 63))))
        self.assertEqual(engine.reference_status, "connected")
        self.assertFalse(engine.process_drl(np.vstack((np.full(64, 500000), np.zeros(64)))))
        self.assertEqual(engine.reference_status, "disconnected")
        self.assertIsNone(engine.process_drl(np.full((2, 1), np.nan)))
        self.assertEqual(engine.reference_status, "invalid")
        self.assertIsNone(engine.process_drl(np.empty((2, 0))))
        self.assertTrue(engine.process_drl(np.zeros((2, 64))))
        self.assertEqual(engine.reference_status, "connected")

    def test_empty_or_nonfinite_drl_is_not_connected(self):
        for invalid in (np.empty((0, 2)), np.full((64, 2), np.inf), np.ones((64, 1))):
            self.assertIsNone(drlref_classification(invalid))
        at_limit = np.column_stack((np.full(64, 200000), np.zeros(64)))
        self.assertTrue(drlref_classification(at_limit))
        at_limit[:, 0] += 1
        self.assertFalse(drlref_classification(at_limit))

    def test_reference_expires_while_eeg_continues_and_requires_fresh_window(self):
        monitor = QualityMonitor()
        for index in range(4):
            report = self.feed(monitor, index, np.zeros((2, 64)))
        self.assertTrue(report["is_drl_connected"])
        self.assertEqual(report["channels"]["AF7"]["sqc_status"], "good")
        self.feed(monitor, 4)
        report = self.feed(monitor, 5)
        self.assertEqual(report["stream_status"], "ok")
        self.assertEqual(report["reference_status"], "stale")
        self.assertIsNone(report["is_drl_connected"])
        self.assertEqual(report["channels"]["AF7"]["sqc_status"], "unknown")
        monitor.feed_drl(np.zeros((2, 1)), now=3.0)
        report = self.feed(monitor, 6)
        self.assertEqual(report["reference_status"], "warming_up")
        monitor.feed_drl(np.zeros((2, 63)), now=3.5)
        report = self.feed(monitor, 7)
        self.assertEqual(report["reference_status"], "connected")

    def test_ready_conveyor_keeps_three_seconds_and_exposes_unverified_sqc(self):
        monitor = QualityMonitor()
        for index in range(4):
            report = self.feed(monitor, index)
        conveyor = ReadyConveyor()
        first = sensor_payload(report, conveyor, 10)
        self.assertEqual(first["AF7"]["ready_progress"], 0)
        filled = sensor_payload(report, conveyor, 13)
        self.assertEqual(filled["AF7"]["ready_progress"], 1)
        self.assertEqual(filled["AF7"]["sqc_quality"], "unknown")
        report["channels"]["AF7"]["status"] = "poor"
        self.assertEqual(sensor_payload(report, conveyor, 14)["AF7"]["ready_progress"], 0)


class StreamingSQCIntegrationTests(unittest.TestCase):
    def test_worker_consumes_eeg_reference_and_classifier_and_flushes_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quality.jsonl"
            monitor = QualityMonitor(QualityConfig(use_epoch_classifier=True),
                                     epoch_classifier=classifier(), output=path)
            monitor.feed(eeg(512), 1000 + np.arange(512) / 256,
                         drl_ref=np.zeros((2, 64)))
            monitor.start()
            try:
                deadline = time.monotonic() + 2
                while monitor.latest is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                report = monitor.latest
                self.assertIsNotNone(report)
                self.assertEqual(report["stream_status"], "ok")
                self.assertEqual(report["reference_status"], "connected")
                self.assertEqual(report["sqc"]["classifier_status"], "active")
                self.assertTrue(all(p > 0.5 for p in report["sqc"]["epoch_probabilities"]))
                self.assertTrue(all(c["sqc_status"] == "good" for c in report["channels"].values()))
            finally:
                monitor.stop()
            reports = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertGreaterEqual(len(reports), 2)
            self.assertEqual(reports[-1]["sqc"]["classifier_provenance"], "unit-test identity remap")


if __name__ == "__main__":
    unittest.main()
