import asyncio
import csv
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from pipeline.collector import Collector
from src.signal_quality import QualityMonitor
from src.pilot_data_collection.psychopy_recording.muse_psychopy_recording_structured import MusePsychopyRecorder


class CollectorQualityTests(unittest.IsolatedAsyncioTestCase):
    async def test_mock_collection_streams_raw_and_quality_before_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = Collector(participant=99, name="test", mock=True, output_root=Path(tmp))
            collector.session_dir.mkdir(parents=True)
            collector._open_board()
            collector._start_eeg_thread()
            try:
                await asyncio.sleep(2.6)
                report = collector.quality.latest
                self.assertEqual(report["stream_status"], "ok")
                self.assertIsNone(report["is_drl_connected"])
                self.assertEqual(report["reference_status"], "unavailable")
                self.assertEqual(report["sqc"]["classifier_status"], "disabled")
                self.assertTrue(report["sqc"]["coupled"])
                self.assertTrue(all(c["status"] == "good" for c in report["channels"].values()))
                self.assertGreater(collector.eeg_csv_path.stat().st_size, 1000)
                self.assertTrue(collector._eeg_thread.is_alive())
            finally:
                await collector._shutdown()
            with collector.eeg_csv_path.open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), collector._recorder.written_samples)
            self.assertGreater(len(rows), 512)
            self.assertEqual(set(rows[0]), {"timestamp", "marker_code", "marker_label", "TP9", "AF7", "AF8", "TP10"})
            self.assertIn("recording_complete: true", collector.metadata_path.read_text())
            reports = [json.loads(s) for s in (collector.session_dir / "signal_quality.jsonl").read_text().splitlines()]
            self.assertGreater(len(reports), 4)

    async def test_startup_failure_releases_board_and_marks_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = Collector(participant=99, name="test", mock=True, output_root=Path(tmp))
            with patch.object(collector, "_start_eeg_thread", side_effect=OSError("disk full")):
                result = await collector.run()
            self.assertEqual(result, 3)
            self.assertFalse(collector._board._thread is not None and collector._board._thread.is_alive())
            self.assertIn("recording_complete: false", collector.metadata_path.read_text())


class PsychoPyQualityTests(unittest.TestCase):
    def test_recording_open_failure_is_reported_without_starting_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = MusePsychopyRecorder(output_dir=tmp, simulate=True)
            recorder.is_streaming = True
            recorder.recording_thread = Mock()
            recorder.quality = QualityMonitor()
            with patch("src.signal_quality.recording.CsvRecorder.start", side_effect=OSError("disk full")):
                self.assertFalse(recorder.start_recording())
            self.assertFalse(recorder.is_recording)
            self.assertEqual(recorder.quality.evaluate()["health"]["recording_error"], "disk full")

    def test_synthetic_fit_then_record_uses_one_continuous_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = MusePsychopyRecorder(output_dir=tmp, simulate=True)
            self.assertTrue(recorder.connect())
            try:
                self.assertTrue(recorder.start_streaming())
                reader = recorder.recording_thread
                time.sleep(2.6)
                self.assertEqual(recorder.signal_quality["stream_status"], "ok")
                self.assertEqual(recorder.signal_quality["reference_status"], "unavailable")
                self.assertTrue(reader.is_alive())
                self.assertTrue(recorder.start_recording())
                self.assertIs(recorder.recording_thread, reader)
                recorder.add_marker("left_mi")
                time.sleep(0.3)
                self.assertGreater(Path(recorder.eeg_output_file).stat().st_size, 100)
                recorder.stop_recording()
                self.assertTrue(reader.is_alive())
                with open(recorder.eeg_output_file) as f:
                    rows = list(csv.DictReader(f))
                self.assertTrue(any(row["EEG_0"] for row in rows))
                self.assertTrue(any(row["marker_code"] == "1" for row in rows))
                self.assertEqual(set(rows[0]), {"timestamp", "marker_code", "marker_label", "EEG_0", "EEG_1", "EEG_2", "EEG_3"})
            finally:
                recorder.disconnect()
            self.assertFalse(reader.is_alive())
            self.assertGreater(Path(recorder.quality_output_file).stat().st_size, 100)


if __name__ == "__main__":
    unittest.main()
