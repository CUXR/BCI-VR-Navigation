"""Recording must include the EEG after the last intentional blink."""

import tempfile
import time
import unittest

import pandas as pd

from src.pilot_data_collection.session_recorder import SessionRecorder


class RecordingTailTests(unittest.TestCase):
    def test_immediate_stop_keeps_full_blink_window(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = SessionRecorder(output_dir=folder, simulate=True)
            self.assertTrue(recorder.connect())
            try:
                self.assertTrue(recorder.start_streaming())
                self.assertTrue(recorder.start_recording())
                time.sleep(0.6)
                marker = time.time()
                self.assertTrue(recorder.add_blink_marker(at=marker))
                recorder.stop_recording()
                self.assertIsNone(recorder.recording_error)
                eeg = pd.read_csv(recorder.eeg_output_file).dropna(subset=["EEG_0"])
                trials = pd.read_csv(recorder.trial_log_file)
                self.assertLessEqual(eeg.timestamp.min(), marker - 0.5)
                self.assertGreaterEqual(eeg.timestamp.max(), marker + 0.5)
                self.assertAlmostEqual(trials.iloc[0].end_time, marker + 0.5, places=5)
                self.assertAlmostEqual(trials.iloc[0].duration, 1.0, places=5)
            finally:
                recorder.disconnect()


if __name__ == "__main__":
    unittest.main()
