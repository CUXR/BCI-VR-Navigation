"""EEG recording session: BrainFlow streaming, raw EEG CSV, and the unified trial log.

Shared by the macOS app's headless helper (`macos_app/backend/muse_session_recorder.py`)
and the legacy PsychoPy script. Has no UI and no PsychoPy dependency.
The trial log schema follows PROGRAMMING_REQUIREMENTS.md.
"""

import csv
import os
import sys
import threading
import time
from datetime import datetime
from enum import Enum
from pathlib import Path

from brainflow.board_shim import BoardIds, BoardShim, BrainFlowError, BrainFlowInputParams

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.signal_quality import CsvRecorder, QualityConfig, QualityMonitor  # noqa: E402
from src.signal_quality.quality import format_quality  # noqa: E402


class TrialType(Enum):
    """Trial types matching PROGRAMMING_REQUIREMENTS.md"""
    MOTOR_IMAGERY_LEFT = "motor_imagery_left"
    MOTOR_IMAGERY_RIGHT = "motor_imagery_right"
    BLINK_INTENTIONAL = "blink_intentional"
    BASELINE_QUIET = "baseline_quiet"
    BASELINE_ACTIVE = "baseline_active"


class ExperimentState(Enum):
    """Experiment state machine"""
    IDLE = "idle"
    MOTOR_IMAGERY_BLOCK = "motor_imagery_block"
    BASELINE_BLOCK = "baseline_block"
    BLINK_BLOCK = "blink_block"
    COMPLETE = "complete"


class Trial:
    """Represents a single trial with unified structure"""
    def __init__(self, trial_id, trial_type, session_id, participant_id=None, 
                 block_number=None, trial_number=None):
        self.trial_id = trial_id
        self.trial_type = trial_type
        self.session_id = session_id
        self.participant_id = participant_id
        self.block_number = block_number
        self.trial_number = trial_number
        
        # Timestamps (will be set during trial)
        self.start_time = None
        self.end_time = None
        self.event_marker = None  # For event-centered trials (blinks)
        self.duration = None
        
        # Label for ML training
        self.label = self._get_label()
        self.notes = ""
    
    def _get_label(self):
        """Get label for ML training based on trial type"""
        label_map = {
            TrialType.MOTOR_IMAGERY_LEFT: "left",
            TrialType.MOTOR_IMAGERY_RIGHT: "right",
            TrialType.BLINK_INTENTIONAL: "blink",
            TrialType.BASELINE_QUIET: "baseline",
            TrialType.BASELINE_ACTIVE: "baseline",
        }
        return label_map.get(self.trial_type, "unknown")
    
    def finalize(self):
        """Calculate duration after trial ends"""
        if self.start_time is not None and self.end_time is not None:
            self.duration = self.end_time - self.start_time
        elif self.event_marker is not None:
            # For event-centered trials, duration is window size
            # Will be set based on extraction window (e.g., 400ms for ±200ms)
            pass
    
    def to_dict(self):
        """Convert trial to dictionary for CSV export"""
        return {
            'trial_id': self.trial_id,
            'trial_type': self.trial_type.value if isinstance(self.trial_type, TrialType) else self.trial_type,
            'label': self.label,
            'start_time': self.start_time,
            'end_time': self.end_time,
            'event_marker': self.event_marker,
            'duration': self.duration,
            'session_id': self.session_id,
            'participant_id': self.participant_id or '',
            'block_number': self.block_number or '',
            'trial_number': self.trial_number or '',
            'notes': self.notes,
        }


class SessionRecorder:
    """Records MUSE EEG data with markers and the unified trial log."""
    
    def __init__(self, output_dir=None, participant_id=None, subject_name=None,
                 subject_number=None, blink_window_ms=500, serial_number=None, simulate=False):
        """Initialize the recorder.

        Args:
            output_dir: Directory to save output files. Auto-derived from subject_number if not given.
            participant_id: Optional participant identifier
            subject_name: Participant name (for metadata.yaml)
            subject_number: Subject number, e.g. 6 -> data/sub06/
            blink_window_ms: Window size in ms for blink extraction (±window_ms around event)
            simulate: Use synthetic EEG without a headband; save under simulated_data/.
            serial_number: Muse serial number to connect to a specific device
        """
        if output_dir is None:
            sub_label = f"sub{subject_number:02d}" if subject_number is not None else "sub00"
            output_dir = os.path.join("simulated_data" if simulate else "data", sub_label)
        os.makedirs(output_dir, exist_ok=True)

        # Write metadata.yaml
        metadata_path = os.path.join(output_dir, "metadata.yaml")
        with open(metadata_path, "w") as f:
            f.write(f"date: {datetime.now().strftime('%Y-%m-%d')}\n")
            f.write(f"participant: {subject_name or participant_id or 'unknown'}\n")
            if simulate:
                f.write("simulated: true\n")
        print(f"Metadata saved to {metadata_path}")
        
        # Create output filenames with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_id = f"session_{timestamp}"
        self.session_id = session_id
        self.participant_id = participant_id
        
        self.eeg_output_file = os.path.join(output_dir, f"eeg_data_{timestamp}.csv")
        self.trial_log_file = os.path.join(output_dir, f"trial_log_{timestamp}.csv")
        self.quality_output_file = os.path.join(output_dir, f"signal_quality_{timestamp}.jsonl")
        
        self.board = None
        self.is_streaming = False
        self.is_recording = False
        
        # Data collection
        self._csv_recorder = None
        self.quality = None
        self.recording_error = None
        self.trials = []  # List of Trial objects
        self.current_trial = None
        self.recording_thread = None
        self.stop_recording_flag = threading.Event()
        self._flush_recording = threading.Event()
        self._recording_flushed = threading.Event()
        self._recording_end_time = None
        self._last_recorded_timestamp = None
        self.buffer_lock = threading.Lock()
        self.trial_lock = threading.Lock()  # Lock for trial access
        
        # Session start time for relative timestamps
        self.session_start_time = None
        
        # Board info (will be set after connection)
        self.simulate = simulate
        self.board_id = BoardIds.SYNTHETIC_BOARD.value if simulate else BoardIds.MUSE_2_BOARD.value
        self.serial_number = None if simulate else serial_number
        self.sampling_rate = None
        self.eeg_channels = None
        self.marker_channel = None
        self.timestamp_channel = None
        self.packet_channel = None
        
        # Blink extraction window
        self.blink_window_ms = blink_window_ms
        
        # Trial counter
        self.trial_counter = 0
        self.block_counter = 0
        
        # Marker definitions
        self.markers = {
            'blink': (10, 'blink_intentional'),
            'left_mi': (1, 'left_motor_imagery'),
            'right_mi': (2, 'right_motor_imagery'),
            'baseline_quiet': (3, 'baseline_quiet'),
            'baseline_active': (4, 'baseline_active'),
        }
        
    def connect(self, max_retries=3):
        """Connect to MUSE board with automatic BLE retries."""
        if self.board is not None:
            print("Already connected.")
            return True

        device_label = "Synthetic EEG" if self.simulate else (
            f"Muse2 (serial={self.serial_number})" if self.serial_number else "Muse2"
        )
        BoardShim.enable_board_logger()

        for attempt in range(1, max_retries + 1):
            print(f"Connecting to {device_label}... (attempt {attempt}/{max_retries})")
            try:
                params = BrainFlowInputParams()
                if self.serial_number:
                    params.serial_number = self.serial_number
                board = BoardShim(self.board_id, params)
                board.prepare_session()

                # Get board channel info after connection
                self.board = board
                self.sampling_rate = BoardShim.get_sampling_rate(self.board_id)
                self.eeg_channels = BoardShim.get_eeg_channels(self.board_id)
                if self.simulate:
                    self.eeg_channels = self.eeg_channels[:4]
                self.marker_channel = BoardShim.get_marker_channel(self.board_id)
                self.timestamp_channel = BoardShim.get_timestamp_channel(self.board_id)
                self.packet_channel = BoardShim.get_package_num_channel(self.board_id)

                print(f"{device_label} connected successfully.")
                return True

            except BrainFlowError as e:
                print(f"BrainFlow error on attempt {attempt}: {e}")
                try:
                    board.release_session()
                except Exception:
                    pass
                if attempt < max_retries:
                    print("Retrying...")
                    time.sleep(1)
            except Exception as e:
                print(f"Unexpected error on attempt {attempt}: {e}")
                if attempt < max_retries:
                    time.sleep(1)

        print(f"Failed to connect to {device_label} after {max_retries} attempts.")
        self.board = None
        return False
    
    def disconnect(self):
        """Disconnect from MUSE board."""
        if self.board is None:
            return
            
        if self.is_recording:
            self.stop_recording()
        if self.is_streaming:
            self.stop_streaming()
            
        try:
            self.board.release_session()
            print("BrainFlow session released.")
        except Exception as e:
            print(f"Error while releasing session: {e}")
            
        self.board = None
        self.is_streaming = False
    
    def start_streaming(self):
        """Start streaming data from MUSE."""
        if self.board is None:
            print("Cannot start: Muse2 is not connected.")
            return False
        if self.is_streaming:
            print("Streaming already in progress.")
            return True
            
        try:
            self.quality = QualityMonitor(QualityConfig(sfreq=self.sampling_rate),
                                          output=self.quality_output_file,
                                          source="synthetic" if self.simulate else "muse",
                                          muse_packets=not self.simulate)
            self.quality.start()
            self.board.start_stream()
            self.is_streaming = True
            self.session_start_time = time.time()
            self.stop_recording_flag.clear()
            self.recording_thread = threading.Thread(target=self._collect_data, daemon=True)
            self.recording_thread.start()
            print("Streaming started; checking electrode quality.")
            return True
        except Exception as exc:
            if self.is_streaming:
                self.board.stop_stream()
            self.is_streaming = False
            if self.quality is not None:
                self.quality.stop()
            print(f"Error while starting stream: {exc}")
            return False

    def stop_streaming(self):
        """Stop the sole reader before stopping/releasing BrainFlow."""
        if self.board is None or not self.is_streaming:
            return
        if self.is_recording:
            self.stop_recording()
        self.stop_recording_flag.set()
        if self.recording_thread is not None:
            self.recording_thread.join(timeout=3)
            if self.recording_thread.is_alive():
                raise RuntimeError("EEG reader did not stop")
        try:
            self.board.stop_stream()
        finally:
            self.is_streaming = False
            if self.quality is not None:
                self.quality.stop()
        print("Streaming stopped.")

    @property
    def signal_quality(self):
        return self.quality.latest if self.quality is not None else None

    def _record_rows(self, samples):
        rows = []
        for sample in samples:
            row = {"timestamp": sample["timestamp"],
                   "marker_code": sample["marker_code"] if sample["marker_code"] is not None else "",
                   "marker_label": sample["marker_label"] or ""}
            for i in range(4):
                row[f"EEG_{i}"] = sample["eeg_data"][i] if i < len(sample["eeg_data"]) else ""
            rows.append(row)
        try:
            self._csv_recorder.submit(rows)
        except RuntimeError as exc:
            if self.recording_error is None:
                print(f"RECORDING INCOMPLETE: {exc}")
            self.recording_error = str(exc)
        self.quality.set_health(recording_error=self._csv_recorder.error,
                                recording_backlog_batches=self._csv_recorder.backlog,
                                rejected_recording_samples=self._csv_recorder.rejected_samples)

    def _collect_data(self):
        """Continuously drain BrainFlow, including while fitting the headband."""
        last_report = 0.0
        while not self.stop_recording_flag.is_set():
            try:
                flush_requested = self._flush_recording.is_set()
                data = self.board.get_board_data()
                n = data.shape[1]
                if n:
                    timestamps = data[self.timestamp_channel]
                    eeg = data[self.eeg_channels[:4]]
                    self.quality.feed(eeg, timestamps,
                                      data[self.packet_channel] if not self.simulate else None)
                    with self.buffer_lock:
                        if self.is_recording:
                            samples = []
                            for i in range(n):
                                ts = float(timestamps[i])
                                marker = data[self.marker_channel, i]
                                for code, label in self.markers.values():
                                    if marker > 0 and abs(marker - code) < 0.1:
                                        samples.append({"timestamp": ts, "eeg_data": [],
                                                        "marker_code": code, "marker_label": label})
                                        break
                                samples.append({"timestamp": ts, "eeg_data": eeg[:, i].tolist(),
                                                "marker_code": None, "marker_label": None})
                            self._record_rows(samples)
                            self._last_recorded_timestamp = float(timestamps[-1])
                if flush_requested and (
                    self._recording_end_time is None
                    or (self._last_recorded_timestamp is not None
                        and self._last_recorded_timestamp >= self._recording_end_time)
                ):
                    with self.buffer_lock:
                        self.is_recording = False
                    self._recording_flushed.set()
                    self._flush_recording.clear()
                self.quality.set_health(board_backlog_samples=self.board.get_board_data_count())
            except Exception as exc:
                self.quality.set_health(reader_error=str(exc))
                self.recording_error = f"EEG reader failed: {exc}"
                print(self.recording_error)
                return
            if time.monotonic() - last_report >= 0.5:
                print(format_quality(self.signal_quality))
                last_report = time.monotonic()
            self.stop_recording_flag.wait(0.02)

    def start_recording(self):
        if not self.is_streaming or not self.recording_thread.is_alive():
            print("Cannot record: EEG reader is not running.")
            return False
        if self.is_recording:
            return True
        with self.buffer_lock:
            self._csv_recorder = CsvRecorder(self.eeg_output_file,
                                            ['timestamp', 'marker_code', 'marker_label'] +
                                            [f'EEG_{i}' for i in range(4)])
            try:
                self._csv_recorder.start()
            except OSError as exc:
                self.recording_error = str(exc)
                self.quality.set_health(recording_error=self.recording_error)
                print(f"Cannot start recording: {exc}")
                return False
            self.trials = []
            self._flush_recording.clear()
            self._recording_flushed.clear()
            self._recording_end_time = None
            self._last_recorded_timestamp = None
            self.recording_error = None
            self.is_recording = True
        print("Recording started.")
        return True

    def stop_recording(self):
        if not self.is_recording:
            return
        if self.current_trial is not None:
            self.end_trial()
        # A blink is logged immediately, but its post-event samples arrive later.
        # Wait for EEG coverage, not just one drain of the Bluetooth buffer.
        self._recording_end_time = max(
            (trial.event_marker + self.blink_window_ms / 1000.0
             for trial in self.trials if trial.event_marker is not None),
            default=None,
        )
        self._flush_recording.set()
        if self.recording_thread.is_alive() and not self._recording_flushed.wait(timeout=3):
            self.recording_error = "EEG reader did not flush the recording tail"
        with self.buffer_lock:
            self.is_recording = False
        self._save_eeg_data()
        self._save_trial_log()
        if self.recording_error:
            print(f"RECORDING INCOMPLETE: {self.recording_error}")
        else:
            print(f"Recording saved to {self.eeg_output_file}")
        print(f"Trial log saved to {self.trial_log_file}")

    def _save_eeg_data(self):
        try:
            self._csv_recorder.close()
        except RuntimeError as exc:
            self.recording_error = str(exc)
        self.quality.set_health(recording_error=self.recording_error,
                                recording_backlog_batches=self._csv_recorder.backlog,
                                rejected_recording_samples=self._csv_recorder.rejected_samples)

    def _save_trial_log(self):
        """Save trial log to CSV file per PROGRAMMING_REQUIREMENTS.md"""
        if len(self.trials) == 0:
            print("No trials to save.")
            return
        
        print(f"Saving {len(self.trials)} trials to {self.trial_log_file}")
        
        try:
            with open(self.trial_log_file, 'w', newline='') as f:
                fieldnames = [
                    'trial_id', 'trial_type', 'label', 'start_time', 'end_time',
                    'event_marker', 'duration', 'session_id', 'participant_id',
                    'block_number', 'trial_number', 'notes'
                ]
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                
                for trial in self.trials:
                    writer.writerow(trial.to_dict())
            
            print(f"Successfully saved {len(self.trials)} trials")
        except Exception as e:
            print(f"Error saving trial log: {e}")
            import traceback
            traceback.print_exc()
    
    def start_trial(self, trial_type, block_number=None, notes="", at=None):
        """Start a new trial.
        
        Args:
            trial_type: TrialType enum value
            block_number: Optional block number
            notes: Optional notes for the trial
            at: Unix time of the cue onset; defaults to now. The macOS app passes the
                moment it showed the cue, which shares the system clock with BrainFlow.
        """
        if self.current_trial is not None:
            print("Warning: Ending previous trial before starting new one")
            self.end_trial()
        
        self.trial_counter += 1
        if block_number is not None:
            self.block_counter = block_number
        
        trial = Trial(
            trial_id=f"trial_{self.trial_counter:04d}",
            trial_type=trial_type,
            session_id=self.session_id,
            participant_id=self.participant_id,
            block_number=self.block_counter if block_number is None else block_number,
            trial_number=self.trial_counter,
        )
        trial.start_time = time.time() if at is None else float(at)
        trial.notes = notes
        
        with self.trial_lock:
            self.current_trial = trial
        
        print(f"Started trial: {trial.trial_id} ({trial_type.value})")
        return trial
    
    def end_trial(self, at=None):
        """End the current trial at Unix time `at` (default now)."""
        if self.current_trial is None:
            return None
        
        with self.trial_lock:
            trial = self.current_trial
            trial.end_time = time.time() if at is None else float(at)
            trial.finalize()
            self.trials.append(trial)
            self.current_trial = None
        
        print(f"Ended trial: {trial.trial_id} (duration: {trial.duration:.3f}s)")
        return trial
    
    def add_blink_marker(self, at=None):
        """Add blink marker and create blink trial (event-centered).
        
        Per PROGRAMMING_REQUIREMENTS.md: blink trials are event-centered with
        ±blink_window_ms around the button press.
        """
        if self.board is None or not self.is_streaming:
            print("Cannot add blink marker: not streaming.")
            return False
        
        if not self.is_recording:
            print("Cannot add blink marker: not recording.")
            return False
        
        # Create blink trial
        trial = self.start_trial(TrialType.BLINK_INTENTIONAL, notes="Intentional blink")
        marker_time = time.time() if at is None else float(at)
        trial.event_marker = marker_time
        trial.start_time = marker_time - (self.blink_window_ms / 1000.0)
        trial.end_time = marker_time + (self.blink_window_ms / 1000.0)
        trial.duration = (2 * self.blink_window_ms) / 1000.0
        
        # Insert marker into BrainFlow stream
        code, label = self.markers['blink']
        try:
            self.board.insert_marker(float(code))
            
            # Add marker to buffer
            with self.buffer_lock:
                marker_sample = {
                    'timestamp': marker_time,
                    'eeg_data': [],
                    'marker_code': code,
                    'marker_label': label,
                }
                self._record_rows([marker_sample])
            
            # End trial immediately (event-centered)
            self.end_trial(at=marker_time + self.blink_window_ms / 1000.0)
            
            print(f"Blink marker added: {label} (code={code}) at {marker_time:.3f}")
            return True
        except Exception as e:
            print(f"Failed to insert blink marker: {e}")
            return False
    
    def add_marker(self, marker_key):
        """Add a marker to the stream (legacy method for compatibility).
        
        Args:
            marker_key: Key from self.markers dict
        """
        if self.board is None or not self.is_streaming:
            print(f"Cannot add marker ({marker_key}): not streaming.")
            return
        
        if marker_key not in self.markers:
            print(f"Unknown marker key: {marker_key}")
            return
        
        if not self.is_recording:
            print(f"Cannot add marker ({marker_key}): not recording.")
            return
        
        code, label = self.markers[marker_key]
        marker_time = time.time()
        
        try:
            self.board.insert_marker(float(code))
            
            with self.buffer_lock:
                marker_sample = {
                    'timestamp': marker_time,
                    'eeg_data': [],
                    'marker_code': code,
                    'marker_label': label,
                }
                self._record_rows([marker_sample])
            
            print(f"Marker sent: {label} (code={code})")
        except Exception as e:
            print(f"Failed to insert marker: {e}")
