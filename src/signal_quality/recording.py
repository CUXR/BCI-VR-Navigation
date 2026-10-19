"""Bounded, nonblocking acquisition-to-disk queue with explicit failures."""

import csv
from pathlib import Path
import queue
import threading


class CsvRecorder:
    def __init__(self, path, fieldnames, *, max_batches=64):
        if max_batches < 1:
            raise ValueError("max_batches must be positive")
        self.path = Path(path)
        self.fieldnames = list(fieldnames)
        self._queue = queue.Queue(maxsize=max_batches)
        self._stop = threading.Event()
        self._thread = None
        self.error = None
        self.rejected_samples = 0
        self.written_samples = 0

    @property
    def backlog(self):
        return self._queue.qsize()

    def start(self):
        if self._thread is not None:
            raise RuntimeError("Recorder already started")
        handle = self.path.open("w", newline="")
        try:
            writer = csv.DictWriter(handle, fieldnames=self.fieldnames)
            writer.writeheader()
            handle.flush()
        except Exception:
            handle.close()
            raise
        self._thread = threading.Thread(target=self._run, args=(handle, writer), daemon=True)
        self._thread.start()

    def submit(self, rows):
        if self._thread is None or self._stop.is_set():
            raise RuntimeError("Recorder is not running")
        if self.error:
            self.rejected_samples += len(rows)
            raise RuntimeError(self.error)
        try:
            self._queue.put_nowait(rows)
        except queue.Full:
            self.rejected_samples += len(rows)
            self.error = "Recording queue full; session is incomplete"
            raise RuntimeError(self.error) from None

    def _run(self, handle, writer):
        try:
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    rows = self._queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                writer.writerows(rows)
                handle.flush()
                self.written_samples += len(rows)
        except Exception as exc:
            self.error = f"Recording write failed: {exc}"
        finally:
            try:
                handle.close()
            except OSError as exc:
                self.error = f"Recording close failed: {exc}"

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                self.error = "Recording writer did not stop; session may be incomplete"
        if self.error:
            raise RuntimeError(self.error)
