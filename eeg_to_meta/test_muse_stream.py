"""Regression check for native BLE Muse 2 setup."""

import unittest
import time
from unittest.mock import patch

from brainflow.board_shim import BoardIds

from muse_stream import MuseBrainFlowStream
from buffer import RingBuffer


class MuseBrainFlowStreamTests(unittest.TestCase):
    @patch("muse_stream.QualityMonitor")
    @patch("muse_stream.threading.Thread")
    @patch("brainflow.board_shim.BoardShim")
    def test_short_serial_uses_native_ble_board(self, board_shim, thread, quality):
        board_shim.get_sampling_rate.return_value = 256
        board_shim.get_eeg_channels.return_value = [1, 2, 3, 4]
        stream = MuseBrainFlowStream(serial="15C3")
        stream.start(None)

        board_id, params = board_shim.call_args.args
        self.assertEqual(board_id, BoardIds.MUSE_2_BOARD.value)
        self.assertEqual(params.serial_number, "Muse-15C3")
        board_shim.return_value.prepare_session.assert_called_once_with()
        board_shim.return_value.start_stream.assert_called_once_with()
        thread.return_value.start.assert_called_once_with()

    def test_synthetic_reader_feeds_quality_and_inference_buffer(self):
        stream = MuseBrainFlowStream(simulate=True)
        buf = RingBuffer()
        try:
            stream.start(buf)
            time.sleep(2.6)
            self.assertTrue(stream.is_running)
            self.assertGreater(buf.total_written, 500)
            self.assertEqual(stream.quality.latest["stream_status"], "ok")
            self.assertEqual(stream.quality.latest["source"], "synthetic")
        finally:
            stream.stop()
        self.assertFalse(stream._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
