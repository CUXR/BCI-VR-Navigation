"""Signal-quality pipeline contracts and integration checks."""

import unittest

import numpy as np
from scipy.special import expit

from src.signal_quality import (
    EpochClassifier, QualityConfig, QualityMonitor, SignalQualityEngine,
    calc_isgood_feats, calc_psd, filter_data_dfiit, is_epoch_good,
)


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
        self.assertEqual(report["channels"]["AF8"]["sqc_status"], "good")
        self.assertEqual(report["channels"]["AF8"]["horseshoe_raw"], 4)

    def test_clipping_extension_cannot_be_hidden_by_coupling_or_filtering(self):
        data = eeg(512)
        data[1] = np.clip(995 + data[1], -1000, 1000)
        monitor = QualityMonitor()
        monitor.feed(data, 1000 + np.arange(512) / 256, now=0)
        item = monitor.evaluate(now=0)["channels"]["AF7"]
        self.assertEqual(item["status"], "poor")
        self.assertIn("saturation", item["safeguard_reasons"])
        self.assertEqual(item["sqc_status"], "good")

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


if __name__ == "__main__":
    unittest.main()
