"""App recording → personal bundle → real-time inference, without a headband."""

import json
from pathlib import Path
import pickle
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from pipeline.personalize_app import (
    CLASS_NAMES, find_app_sessions, gather_app_data, personalize_app,
)

ROOT = Path(__file__).resolve().parents[1]


def recording(folder):
    folder.mkdir(parents=True)
    rng = np.random.default_rng(37)
    times = 1000 + np.arange(80 * 256) / 256
    eeg = rng.normal(0, 3, (len(times), 4))
    trials = []
    for i in range(12):
        start = 1002 + i * 4
        trial_type = "motor_imagery_left" if i % 2 == 0 else "motor_imagery_right"
        mask = (times >= start) & (times <= start + 2.2)
        eeg[mask, i % 2 + 1] += 10 * np.sin(2 * np.pi * (10 + i % 2 * 10) * times[mask])
        trials.append(dict(trial_type=trial_type, start_time=start, end_time=start + 2.2,
                           event_marker=np.nan))
    for i in range(6):
        marker = 1053 + i * 3
        eeg[:, 1:3] += (80 * np.exp(-((times - marker) / .06) ** 2))[:, None]
        trials.append(dict(trial_type="blink_intentional", start_time=marker - .5,
                           end_time=marker + .5, event_marker=marker))
    frame = pd.DataFrame(eeg, columns=[f"EEG_{i}" for i in range(4)])
    frame.insert(0, "timestamp", times)
    frame["marker_code"] = np.nan
    # Marker-only annotations are emitted separately, out of sample order.
    frame = pd.concat([frame, pd.DataFrame([dict(timestamp=1053, marker_code=10)])])
    frame.to_csv(folder / "eeg_data_20260101_120000.csv", index=False)
    pd.DataFrame(trials).to_csv(folder / "trial_log_20260101_120000.csv", index=False)


class AppPersonalizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.subject = self.root / "data/sub01"
        recording(self.subject)

    def test_pairs_archives_and_participant_isolation(self):
        archive = self.root / "data/Muse_Data/Phase1/sub01"
        recording(archive)
        (self.subject / "eeg_data_orphan.csv").write_text("not a paired session")
        sessions = find_app_sessions(1, self.root / "data")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["eeg_path"].parent, self.subject)
        self.assertEqual(find_app_sessions(2, self.root / "data"), [])

    def test_insufficient_data_preserves_previous_model(self):
        output = self.root / "models"
        output.mkdir()
        old = output / "sub01_app.pkl"
        old.write_bytes(b"previous model")
        trial_path = self.subject / "trial_log_20260101_120000.csv"
        trials = pd.read_csv(trial_path)
        trials[trials.trial_type != "motor_imagery_right"].to_csv(trial_path, index=False)
        with self.assertRaisesRegex(ValueError, "right: 0"):
            personalize_app(participant=1, data_root=self.root / "data", output_dir=output)
        self.assertEqual(old.read_bytes(), b"previous model")

    def test_save_failure_preserves_previous_model(self):
        output = self.root / "models"
        output.mkdir()
        old = output / "sub01_app.pkl"
        old.write_bytes(b"previous model")
        def fail_save(**kwargs):
            kwargs["output_path"].write_bytes(b"partial write")
            raise OSError("disk full")
        with patch("pipeline.personalize_app.save_model_bundle", side_effect=fail_save):
            with self.assertRaisesRegex(OSError, "disk full"):
                personalize_app(participant=1, data_root=self.root / "data", output_dir=output)
        self.assertEqual(old.read_bytes(), b"previous model")
        self.assertEqual(list(output.iterdir()), [old])

    def test_train_load_predict_and_reject_wrong_participant(self):
        data = gather_app_data(1, self.root / "data")
        self.assertEqual(data.mi_labels.count(0), 6)
        self.assertEqual(data.mi_labels.count(1), 6)
        self.assertEqual(len(data.blink_pos), 6)
        # Blink peak must be inside the centered half-second epoch, not at its edge.
        peak = np.argmax(np.abs(data.blink_pos[0][1]))
        self.assertTrue(32 < peak < 96, peak)
        output = self.root / "models"
        command = [sys.executable, str(ROOT / "macos_app/backend/train_personal_model.py"),
                   "--participant", "1", "--data-root", str(self.root / "data"),
                   "--output-dir", str(output)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        message = json.loads(result.stdout)
        path = Path(message["path"])
        self.assertEqual(message["event"], "trained")
        with path.open("rb") as f:
            bundle = pickle.load(f)
        self.assertEqual(bundle["class_names"], CLASS_NAMES)
        self.assertEqual(bundle["mi_class_counts"], {name: 6 for name in CLASS_NAMES})
        self.assertEqual(bundle["mi_pipeline"].n_features_in_, 78)
        self.assertEqual(bundle["blink_pipeline"].n_features_in_, 75)
        self.assertIsNone(bundle["mi_csp_W"])
        # Existing training and runtime modules use different bare `config` imports;
        # just as in the app, exercise inference in a separate Python process.
        inference = '''
import sys, numpy as np
sys.path.insert(0, sys.argv[1])
from model_loader import ModelBundle
from inference import InferenceEngine, _extract_mi_spectral_features, _extract_blink_features
from preprocess import preprocess_mi, preprocess_blink
model = ModelBundle(sys.argv[2])
assert model.load()
rng = np.random.default_rng(2)
mi = rng.normal(0, 5, (4, 512))
blink = rng.normal(0, 5, (4, 128))
mi_features = _extract_mi_spectral_features(preprocess_mi(mi, 256), 256, list(range(4)))
blink_features = _extract_blink_features(preprocess_blink(blink, 256), 256, list(range(4)))
assert np.isfinite(model.predict_mi(mi_features)[1]).all()
assert np.isfinite(model.predict_blink(blink_features)[1]).all()
result = InferenceEngine(model).classify(mi, None)
assert set(model.mi_label_names()).issubset(result['raw_probs']), result
assert np.isfinite(result['confidence'])
'''
        checked = subprocess.run([sys.executable, "-c", inference, str(ROOT / "eeg_to_meta"), str(path)],
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        rejected = subprocess.run(
            [sys.executable, str(ROOT / "macos_app/backend/realtime_control.py"),
             "--model", str(path), "--participant", "2", "--mock", "--no-ws"],
            input="", capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(rejected.returncode, 2, rejected.stderr)
        self.assertIn("does not match your profile", json.loads(rejected.stdout)["message"])


if __name__ == "__main__":
    unittest.main()
