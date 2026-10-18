"""Per-participant EEG + marker collector for the AutoMove protocol.

This is the Python half of the data-collection mode. Unity drives the
trial timing (see AutoMoveDataCollectionController) and pushes markers
over the WebSocket exposed by `pipeline.marker_server`. This module
concurrently:

    1. Streams EEG samples from a Muse 2 (or a synthetic / mock board).
    2. Receives Unity markers, injects the matching integer code into
       the BrainFlow stream so the EEG and trial log share a clock,
       and persists per-trial rows to disk.
    3. Reacts to control frames from Unity (`run_start`, `run_end`,
       `session_end`) for run/session bookkeeping. Ctrl-C also flushes
       cleanly.

Output layout (mirrors src/pilot_data_collection/psychopy_recording/
muse_psychopy_recording_structured.py so existing analysis tooling
keeps working):

    data/sub<NN>/session_<YYYYMMDD_HHMMSS>/
        metadata.yaml      # session, participant, board, runs
        eeg_data.csv       # per-sample timestamp + 4 EEG channels (+marker rows)
        trial_log.csv      # one row per phase event with run_index / phase

Run via `pipeline.cli` (added later) or directly:

    python -m pipeline.collector --participant 5 --name "Asheeb" --runs 5
    python -m pipeline.collector --participant 5 --name "Asheeb" --mock --runs 1

`--mock` swaps the BrainFlow board for an in-process MockBoard so the
collector can be smoke-tested without a Muse or BrainFlow installed.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import signal
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

from . import labels as L
from .marker_server import (
    ControlEvent,
    Event,
    MarkerEvent,
    MarkerServer,
    DEFAULT_HOST as MARKER_DEFAULT_HOST,
    DEFAULT_PORT as MARKER_DEFAULT_PORT,
)

log = logging.getLogger(__name__)


# ── Constants — must mirror eeg_to_meta/config.py for downstream parity
SFREQ = 256
N_EEG_CHANNELS = 4
CH_NAMES = ["TP9", "AF7", "AF8", "TP10"]
DEFAULT_OUTPUT_ROOT = Path("data")
EEG_POLL_INTERVAL_S = 0.1


# ── Trial log row ───────────────────────────────────────────────────


@dataclass
class TrialRow:
    """One row written to trial_log.csv. Per-phase, not per-trial."""

    trial_id: str
    trial_type: str
    label: str
    phase: str
    run_index: int
    trial_index: int
    ts_unity: float
    ts_recv: float
    marker_code: int
    session_id: str
    participant_id: str
    notes: str = ""

    @classmethod
    def from_event(
        cls,
        ev: MarkerEvent,
        *,
        trial_id: str,
        session_id: str,
        participant_id: str,
        notes: str = "",
    ) -> "TrialRow":
        return cls(
            trial_id=trial_id,
            trial_type=ev.trial_type,
            label=L.TRIAL_TYPE_TO_LABEL.get(ev.trial_type, "unknown"),
            phase=ev.phase,
            run_index=ev.run_index,
            trial_index=ev.trial_index,
            ts_unity=ev.ts_unity,
            ts_recv=ev.ts_recv,
            marker_code=ev.marker_code,
            session_id=session_id,
            participant_id=participant_id,
            notes=notes,
        )

    @staticmethod
    def fieldnames() -> list[str]:
        return [
            "trial_id",
            "trial_type",
            "label",
            "phase",
            "run_index",
            "trial_index",
            "ts_unity",
            "ts_recv",
            "marker_code",
            "session_id",
            "participant_id",
            "notes",
        ]


# ── Mock board (used when --mock is passed) ────────────────────────


class MockBoard:
    """In-process stand-in for BrainFlow BoardShim.

    Generates pink-noise-ish synthetic EEG at SFREQ Hz across 4 channels
    on a background thread. `insert_marker` and `get_board_data` mirror
    the BrainFlow surface used by the collector.
    """

    EEG_INDICES = [1, 2, 3, 4]   # match Muse 2 layout (channel 0 reserved)
    TIMESTAMP_INDEX = 5
    MARKER_INDEX = 6
    N_ROWS = 7

    def __init__(self, sfreq: int = SFREQ, seed: int = 0) -> None:
        self.sfreq = sfreq
        self._rng = np.random.default_rng(seed)
        self._stream_t0: float | None = None
        self._buffer: list[np.ndarray] = []  # list of (N_ROWS, n_samples) chunks
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pending_markers: list[tuple[float, float]] = []  # (ts, code)

    def start_stream(self) -> None:
        if self._thread is not None:
            return
        self._stream_t0 = time.time()
        self._stop.clear()
        self._thread = threading.Thread(target=self._produce, daemon=True)
        self._thread.start()
        log.debug("MockBoard streaming started")

    def stop_stream(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        log.debug("MockBoard streaming stopped")

    def release_session(self) -> None:
        self.stop_stream()

    def insert_marker(self, code: float) -> None:
        with self._lock:
            self._pending_markers.append((time.time(), float(code)))

    def get_board_data(self) -> np.ndarray:
        with self._lock:
            if not self._buffer:
                return np.zeros((self.N_ROWS, 0), dtype=np.float64)
            chunk = np.concatenate(self._buffer, axis=1)
            self._buffer.clear()
            return chunk

    @staticmethod
    def get_eeg_channels(_board_id: int = 0) -> list[int]:
        return MockBoard.EEG_INDICES

    @staticmethod
    def get_marker_channel(_board_id: int = 0) -> int:
        return MockBoard.MARKER_INDEX

    @staticmethod
    def get_timestamp_channel(_board_id: int = 0) -> int:
        return MockBoard.TIMESTAMP_INDEX

    @staticmethod
    def get_sampling_rate(_board_id: int = 0) -> int:
        return SFREQ

    # ── internals ───────────────────────────────────────────────────

    def _produce(self) -> None:
        period = 1.0 / self.sfreq
        next_t = time.time()
        sample_idx = 0
        chunk_size = max(1, self.sfreq // 10)  # 100 ms chunks
        while not self._stop.is_set():
            now = time.time()
            if now < next_t:
                time.sleep(min(0.01, next_t - now))
                continue
            n = chunk_size
            arr = np.zeros((self.N_ROWS, n), dtype=np.float64)
            for ch in self.EEG_INDICES:
                arr[ch] = self._rng.normal(0.0, 25.0, size=n)
            arr[self.TIMESTAMP_INDEX] = (
                now + np.arange(n) * period
            )
            with self._lock:
                if self._pending_markers:
                    pending = self._pending_markers
                    self._pending_markers = []
                else:
                    pending = []
                if pending:
                    for ts, code in pending:
                        offset = int(round((ts - now) * self.sfreq))
                        offset = max(0, min(n - 1, offset))
                        arr[self.MARKER_INDEX, offset] = code
                self._buffer.append(arr)
            sample_idx += n
            next_t = now + n * period


# ── Collector ──────────────────────────────────────────────────────


class Collector:
    """Drive one collection session for one participant."""

    def __init__(
        self,
        *,
        participant: int,
        name: str,
        runs: int = 1,
        mock: bool = False,
        serial: Optional[str] = None,
        marker_bind: str = MARKER_DEFAULT_HOST,
        marker_port: int = MARKER_DEFAULT_PORT,
        output_root: Path = DEFAULT_OUTPUT_ROOT,
        notes: str = "",
    ) -> None:
        self.participant = participant
        self.name = name
        self.runs = runs
        self.mock = mock
        self.serial = serial
        self.marker_bind = marker_bind
        self.marker_port = marker_port
        self.output_root = Path(output_root)
        self.notes = notes

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_id = f"session_{ts}"
        self.session_dir = self.output_root / f"sub{participant:02d}" / self.session_id
        self.eeg_csv_path = self.session_dir / "eeg_data.csv"
        self.trial_log_path = self.session_dir / "trial_log.csv"
        self.metadata_path = self.session_dir / "metadata.yaml"

        self._board = None  # BoardShim or MockBoard
        self._board_id_str = "MOCK" if mock else "MUSE_2_BOARD"
        self._marker_server: Optional[MarkerServer] = None
        self._stop = asyncio.Event()
        self._eeg_thread: Optional[threading.Thread] = None
        self._eeg_thread_stop = threading.Event()
        self._sample_buffer: list[dict] = []
        self._sample_lock = threading.Lock()
        self._trials: list[TrialRow] = []
        self._sampling_rate: int = SFREQ
        self._eeg_indices: list[int] = list(range(N_EEG_CHANNELS))
        self._marker_index: Optional[int] = None
        self._timestamp_index: Optional[int] = None
        self._session_t0: Optional[float] = None
        self._session_t1: Optional[float] = None
        self._runs_completed = 0
        self._cur_run_index: int = -1

    # ── public entrypoint ──────────────────────────────────────────

    async def run(self) -> int:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        log.info("Session directory: %s", self.session_dir)

        try:
            self._open_board()
        except Exception as exc:                                    # noqa: BLE001
            log.error("Failed to open board: %s", exc)
            return 2

        self._start_eeg_thread()

        self._marker_server = MarkerServer(
            host=self.marker_bind,
            port=self.marker_port,
            on_marker=self._safe_insert_marker,
        )
        await self._marker_server.start()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._request_stop)
            except (NotImplementedError, RuntimeError):
                # Windows / restricted environments — fall back to KeyboardInterrupt.
                pass

        log.info(
            "Collector ready. Marker WS ws://%s:%d. Waiting for Unity to start "
            "%d run(s) (Ctrl-C or session_end to stop).",
            self.marker_bind, self.marker_port, self.runs,
        )

        drain_task = asyncio.create_task(self._drain_events())
        try:
            await self._stop.wait()
        finally:
            drain_task.cancel()
            try:
                await drain_task
            except asyncio.CancelledError:
                pass
            await self._shutdown()

        return 0

    # ── lifecycle helpers ──────────────────────────────────────────

    def _request_stop(self) -> None:
        if not self._stop.is_set():
            log.info("Stop requested; flushing data.")
            self._stop.set()

    def _open_board(self) -> None:
        if self.mock:
            self._board = MockBoard()
            self._board.start_stream()
            self._sampling_rate = self._board.get_sampling_rate()
            self._eeg_indices = self._board.get_eeg_channels()
            self._marker_index = self._board.get_marker_channel()
            self._timestamp_index = self._board.get_timestamp_channel()
            self._board_id_str = "MOCK"
            log.info("MockBoard opened (synthetic EEG, %d Hz)", self._sampling_rate)
            return

        # Real Muse 2 via BrainFlow (deferred import so --mock works without it).
        from brainflow.board_shim import (                          # noqa: PLC0415
            BoardIds,
            BoardShim,
            BrainFlowInputParams,
        )
        BoardShim.enable_board_logger()
        params = BrainFlowInputParams()
        if self.serial:
            params.serial_number = self.serial
        board_id = BoardIds.MUSE_2_BOARD.value
        board = BoardShim(board_id, params)
        board.prepare_session()
        board.start_stream()
        self._board = board
        self._sampling_rate = BoardShim.get_sampling_rate(board_id)
        self._eeg_indices = BoardShim.get_eeg_channels(board_id)
        self._marker_index = BoardShim.get_marker_channel(board_id)
        self._timestamp_index = BoardShim.get_timestamp_channel(board_id)
        self._board_id_str = "MUSE_2_BOARD"
        log.info(
            "Muse 2 connected (%s); %d Hz, %d EEG channels.",
            self.serial or "auto",
            self._sampling_rate,
            len(self._eeg_indices),
        )

    def _safe_insert_marker(self, code: int) -> None:
        if self._board is None:
            return
        try:
            self._board.insert_marker(float(code))
        except Exception:                                           # noqa: BLE001
            log.exception("Failed to inject marker code=%d", code)

    def _start_eeg_thread(self) -> None:
        self._session_t0 = time.time()
        self._eeg_thread_stop.clear()
        self._eeg_thread = threading.Thread(target=self._eeg_loop, daemon=True)
        self._eeg_thread.start()

    def _eeg_loop(self) -> None:
        while not self._eeg_thread_stop.is_set():
            try:
                if self._board is None:
                    time.sleep(EEG_POLL_INTERVAL_S)
                    continue
                data = self._board.get_board_data()
                if data is None or data.shape[1] == 0:
                    time.sleep(EEG_POLL_INTERVAL_S)
                    continue
                self._ingest_eeg_chunk(data)
            except Exception:                                       # noqa: BLE001
                log.exception("EEG loop error; continuing")
                time.sleep(EEG_POLL_INTERVAL_S)
            else:
                time.sleep(EEG_POLL_INTERVAL_S)

    def _ingest_eeg_chunk(self, data: np.ndarray) -> None:
        n = data.shape[1]
        ts_row = (
            data[self._timestamp_index]
            if self._timestamp_index is not None and self._timestamp_index < data.shape[0]
            else None
        )
        marker_row = (
            data[self._marker_index]
            if self._marker_index is not None and self._marker_index < data.shape[0]
            else None
        )
        eeg_rows = [data[idx] for idx in self._eeg_indices if idx < data.shape[0]]
        # Pad missing channels with NaN so the CSV stays rectangular.
        while len(eeg_rows) < N_EEG_CHANNELS:
            eeg_rows.append(np.full(n, np.nan))

        now_fallback = time.time()
        with self._sample_lock:
            for i in range(n):
                ts = (
                    float(ts_row[i])
                    if ts_row is not None and ts_row[i] > 0
                    else now_fallback
                )
                marker_val = (
                    float(marker_row[i])
                    if marker_row is not None
                    else 0.0
                )
                row = {"timestamp": ts}
                for ch_idx, ch_name in enumerate(CH_NAMES):
                    row[ch_name] = float(eeg_rows[ch_idx][i])
                row["marker_code"] = int(round(marker_val)) if abs(marker_val) > 1e-9 else None
                row["marker_label"] = (
                    L.MARKER_CODE_TO_TRIAL_TYPE.get(row["marker_code"])
                    if row["marker_code"] is not None
                    else None
                )
                self._sample_buffer.append(row)

    # ── event drain ─────────────────────────────────────────────────

    async def _drain_events(self) -> None:
        assert self._marker_server is not None
        queue = self._marker_server.queue
        while True:
            ev: Event = await queue.get()
            if isinstance(ev, MarkerEvent):
                self._on_marker_event(ev)
            elif isinstance(ev, ControlEvent):
                self._on_control_event(ev)

    def _on_marker_event(self, ev: MarkerEvent) -> None:
        trial_id = (
            f"trial_r{ev.run_index:02d}_t{ev.trial_index:04d}_{ev.trial_type}"
        )
        row = TrialRow.from_event(
            ev,
            trial_id=trial_id,
            session_id=self.session_id,
            participant_id=str(self.participant),
        )
        self._trials.append(row)
        log.debug(
            "MARKER run=%d trial=%d %s phase=%s code=%d",
            ev.run_index, ev.trial_index, ev.trial_type, ev.phase, ev.marker_code,
        )

    def _on_control_event(self, ev: ControlEvent) -> None:
        if ev.event == "run_start":
            self._cur_run_index = ev.run_index
            log.info("Run %d started (Unity ts=%.2f)", ev.run_index, ev.ts_unity)
        elif ev.event == "run_end":
            self._runs_completed = max(self._runs_completed, ev.run_index + 1)
            log.info("Run %d ended.", ev.run_index)
            if self._runs_completed >= self.runs:
                log.info("All %d run(s) complete; stopping.", self.runs)
                self._request_stop()
        elif ev.event == "session_end":
            log.info("Session end requested by Unity.")
            self._request_stop()

    # ── shutdown / persistence ──────────────────────────────────────

    async def _shutdown(self) -> None:
        self._session_t1 = time.time()
        if self._marker_server is not None:
            await self._marker_server.stop()

        self._eeg_thread_stop.set()
        if self._eeg_thread is not None:
            self._eeg_thread.join(timeout=3.0)

        if self._board is not None:
            try:
                self._board.stop_stream()
            except Exception:                                       # noqa: BLE001
                log.exception("Error stopping stream")
            try:
                self._board.release_session()
            except Exception:                                       # noqa: BLE001
                log.exception("Error releasing session")

        # Drain any tail of EEG samples produced after the loop exit.
        if self._board is not None:
            try:
                tail = self._board.get_board_data()
                if tail is not None and tail.shape[1] > 0:
                    self._ingest_eeg_chunk(tail)
            except Exception:                                       # noqa: BLE001
                pass

        self._write_eeg_csv()
        self._write_trial_log_csv()
        self._write_metadata_yaml()
        log.info(
            "Wrote %d EEG samples and %d trial events to %s",
            len(self._sample_buffer), len(self._trials), self.session_dir,
        )

    def _write_eeg_csv(self) -> None:
        with self._sample_lock:
            samples = list(self._sample_buffer)
        write_eeg_csv(self.eeg_csv_path, samples)

    def _write_trial_log_csv(self) -> None:
        write_trial_log_csv(self.trial_log_path, self._trials)

    def _write_metadata_yaml(self) -> None:
        meta = {
            "session_id": self.session_id,
            "participant_id": self.participant,
            "participant_name": self.name,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "start_time": self._session_t0,
            "end_time": self._session_t1,
            "duration_s": (
                self._session_t1 - self._session_t0
                if self._session_t0 and self._session_t1
                else None
            ),
            "board_id": self._board_id_str,
            "sampling_rate_hz": self._sampling_rate,
            "n_eeg_channels": len(CH_NAMES),
            "ch_names": CH_NAMES,
            "runs_configured": self.runs,
            "runs_completed": self._runs_completed,
            "n_trial_events": len(self._trials),
            "mock": self.mock,
            "notes": self.notes,
        }
        write_metadata_yaml(self.metadata_path, meta)


# ── Session file writers (shared with backend.calibration) ─────────


def write_eeg_csv(path: Path, samples: list[dict]) -> None:
    """Write per-sample dicts (timestamp, marker_code, marker_label, CH_NAMES)."""
    fieldnames = ["timestamp", "marker_code", "marker_label"] + list(CH_NAMES)
    with Path(path).open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for s in samples:
            writer.writerow({
                "timestamp": f"{s['timestamp']:.6f}",
                "marker_code": s.get("marker_code") if s.get("marker_code") is not None else "",
                "marker_label": s.get("marker_label") or "",
                **{ch: f"{s[ch]:.6f}" for ch in CH_NAMES},
            })


def write_trial_log_csv(path: Path, rows: list[TrialRow]) -> None:
    with Path(path).open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TrialRow.fieldnames())
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def write_metadata_yaml(path: Path, meta: dict) -> None:
    # Hand-rolled YAML to avoid an extra runtime dep; values are
    # primitive and quoted carefully.
    def _q(v: object) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        return f"\"{str(v)}\""

    with Path(path).open("w") as f:
        for k, v in meta.items():
            if isinstance(v, list):
                items = ", ".join(_q(x) for x in v)
                f.write(f"{k}: [{items}]\n")
            else:
                f.write(f"{k}: {_q(v) if v is not None else 'null'}\n")


# ── CLI ─────────────────────────────────────────────────────────────


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="EEG + Unity marker collector")
    p.add_argument("--participant", type=int, required=True,
                   help="Participant number, e.g. 5 -> data/sub05/")
    p.add_argument("--name", type=str, required=True,
                   help="Participant name (stored in metadata.yaml)")
    p.add_argument("--runs", type=int, default=1,
                   help="Total number of AutoMove runs Unity will play (default: 1)")
    p.add_argument("--mock", action="store_true",
                   help="Use synthetic EEG instead of a real Muse 2 (no BrainFlow needed)")
    p.add_argument("--serial", type=str, default=None,
                   help="Muse serial number (e.g. Muse-15C3); ignored under --mock")
    p.add_argument("--marker-bind", default=MARKER_DEFAULT_HOST,
                   help=f"Marker WebSocket bind address (default {MARKER_DEFAULT_HOST})")
    p.add_argument("--marker-port", type=int, default=MARKER_DEFAULT_PORT,
                   help=f"Marker WebSocket port (default {MARKER_DEFAULT_PORT})")
    p.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT,
                   help="Root dir for participant data (default: data/)")
    p.add_argument("--notes", type=str, default="",
                   help="Free-form note to embed in metadata.yaml")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="DEBUG-level logging")
    return p


async def _amain(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    collector = Collector(
        participant=args.participant,
        name=args.name,
        runs=args.runs,
        mock=args.mock,
        serial=args.serial,
        marker_bind=args.marker_bind,
        marker_port=args.marker_port,
        output_root=args.output_root,
        notes=args.notes,
    )
    return await collector.run()


def main() -> None:
    try:
        raise SystemExit(asyncio.run(_amain()))
    except KeyboardInterrupt:
        raise SystemExit(0)


if __name__ == "__main__":
    main()
