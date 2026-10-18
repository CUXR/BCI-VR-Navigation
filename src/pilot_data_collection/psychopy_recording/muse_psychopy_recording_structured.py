"""
Psychopy experiment for streaming MUSE EEG data with markers.
Implements unified trial log structure per PROGRAMMING_REQUIREMENTS.md
and experiment procedure per EXPERIMENT_PROCEDURE.md

Structured experiment flow:
1. Connect to Muse and start recording
2. Loop 5 times (LEFT, RIGHT alternating):
   - Play audio instruction for LEFT/RIGHT via TTS
   - Recording starts immediately after instruction
   - Participant imagines the movement
   - Participant presses SPACE when done
   - 5 second rest period
3. Blink block: Record 10 intentional blinks (press B for each)
4. Save data and exit
"""

import os
import sys
import time
import threading
import csv
import subprocess
import platform
import argparse
from datetime import datetime
from pathlib import Path
from enum import Enum

import numpy as np
from brainflow.board_shim import (
    BoardIds,
    BoardShim,
    BrainFlowError,
    BrainFlowInputParams,
    BrainFlowPresets,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from src.signal_quality import CsvRecorder, QualityConfig, QualityMonitor
from src.signal_quality.quality import format_quality

# Text-to-speech setup
def speak_text(text, wait=True):
    """Speak text using system TTS.
    
    On macOS: uses 'say' command
    On Windows: uses edge-tts or pyttsx3
    On Linux: uses espeak or pyttsx3
    
    Args:
        text: Text to speak
        wait: If True, block until speech is complete
    """
    system = platform.system()
    
    try:
        if system == "Darwin":  # macOS
            # Use macOS 'say' command with a natural voice
            cmd = ["say", "-v", "Samantha", "-r", "180", text]
            if wait:
                subprocess.run(cmd, check=True)
            else:
                subprocess.Popen(cmd)
        elif system == "Windows":
            # Try edge-tts first (Microsoft voices), fallback to pyttsx3
            try:
                import edge_tts
                import asyncio
                
                async def speak():
                    communicate = edge_tts.Communicate(text, "en-US-AriaNeural")
                    await communicate.save("temp_speech.mp3")
                    # Play the audio
                    import playsound
                    playsound.playsound("temp_speech.mp3")
                    os.remove("temp_speech.mp3")
                
                if wait:
                    asyncio.run(speak())
                else:
                    threading.Thread(target=lambda: asyncio.run(speak())).start()
            except ImportError:
                # Fallback to pyttsx3
                import pyttsx3
                engine = pyttsx3.init()
                engine.setProperty('rate', 150)
                if wait:
                    engine.say(text)
                    engine.runAndWait()
                else:
                    threading.Thread(target=lambda: (engine.say(text), engine.runAndWait())).start()
        else:  # Linux
            cmd = ["espeak", "-s", "150", text]
            if wait:
                subprocess.run(cmd, check=True)
            else:
                subprocess.Popen(cmd)
    except Exception as e:
        print(f"TTS Error: {e}. Continuing without audio.")


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


class MusePsychopyRecorder:
    """Records MUSE EEG data with markers and trial log in Psychopy experiment."""
    
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
                if flush_requested:
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
            self.recording_error = None
            self.is_recording = True
        print("Recording started.")
        return True

    def stop_recording(self):
        if not self.is_recording:
            return
        if self.current_trial is not None:
            self.end_trial()
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
    
    def start_trial(self, trial_type, block_number=None, notes=""):
        """Start a new trial.
        
        Args:
            trial_type: TrialType enum value
            block_number: Optional block number
            notes: Optional notes for the trial
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
        trial.start_time = time.time()
        trial.notes = notes
        
        with self.trial_lock:
            self.current_trial = trial
        
        print(f"Started trial: {trial.trial_id} ({trial_type.value})")
        return trial
    
    def end_trial(self):
        """End the current trial."""
        if self.current_trial is None:
            return None
        
        with self.trial_lock:
            trial = self.current_trial
            trial.end_time = time.time()
            trial.finalize()
            self.trials.append(trial)
            self.current_trial = None
        
        print(f"Ended trial: {trial.trial_id} (duration: {trial.duration:.3f}s)")
        return trial
    
    def add_blink_marker(self):
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
        marker_time = time.time()
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
            self.end_trial()
            
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


def run_experiment():
    """Run the structured Psychopy experiment with TTS audio instructions.

    Experiment Flow:
    1. Press SPACE to connect to Muse
    2. Press ENTER to start the structured experiment
    3. Automated loop (10 repetitions of LEFT + RIGHT):
       - Audio instruction for LEFT -> Recording starts -> Press SPACE when done -> 5s rest
       - Audio instruction for RIGHT -> Recording starts -> Press SPACE when done -> 5s rest
    4. Blink block: Press B for each intentional blink (10 blinks target)
    5. Data saved automatically at end

    Press ESC at any time to abort and save data.
    """
    from psychopy import visual, core, event

    # Parse CLI arguments
    parser = argparse.ArgumentParser(description="Muse EEG Motor Imagery Experiment")
    parser.add_argument("--name", required=True, help="Subject's name")
    parser.add_argument("--number", type=int, required=True, help="Subject number (e.g. 6)")
    parser.add_argument("--serial", type=str, default=None,
                        help="Muse serial number (skip device selection screen)")
    parser.add_argument("--simulate", action="store_true",
                        help="Try the experiment with synthetic EEG; save to simulated_data/")
    args = parser.parse_args()

    selected_serial = args.serial  # May be None; will prompt in-app if so
    # Accept short form (e.g. "15C3") or full form ("Muse-15C3")
    if selected_serial and not selected_serial.startswith("Muse-"):
        selected_serial = f"Muse-{selected_serial}"
    
    # Experiment parameters
    NUM_REPETITIONS = 10  # Number of LEFT/RIGHT pairs
    REST_DURATION = 3.0  # Rest period in seconds
    TARGET_BLINKS = 10   # Number of intentional blinks to collect
    
    # Create monitor specification
    from psychopy import monitors
    mon = monitors.Monitor('defaultMonitor')
    mon.setWidth(30)
    mon.setDistance(60)
    mon.setSizePix([1280, 800])
    
    # Create window
    win = visual.Window(
        size=(1280, 800),
        fullscr=False,
        color=[0.15, 0.15, 0.2],
        units='pix',
        screen=0,
        colorSpace='rgb',
        allowGUI=True,
        monitor=mon,
        waitBlanking=True,
        useFBO=True,
    )
    
    # Color scheme
    success_color = [0.2, 0.8, 0.4]
    warning_color = [1.0, 0.7, 0.2]
    error_color = [0.9, 0.3, 0.3]
    text_color = [0.95, 0.95, 0.95]
    accent_color = [0.4, 0.6, 1.0]
    left_color = [0.3, 0.7, 0.9]  # Cyan-blue
    right_color = [0.5, 0.8, 0.4]  # Green
    rest_color = [0.6, 0.6, 0.6]  # Gray
    
    text_font = None  # System default font
    
    # Visual elements
    title = visual.TextStim(
        win, text="MUSE EEG Motor Imagery Experiment",
        pos=(0, 320), height=48, color=accent_color,
        font=text_font, bold=True, colorSpace='rgb'
    )
    
    status_box = visual.Rect(
        win, width=600, height=80, pos=(0, 240),
        fillColor=[0.2, 0.2, 0.25], lineColor=[0.4, 0.4, 0.5],
        lineWidth=2, colorSpace='rgb'
    )
    
    status_text = visual.TextStim(
        win, text="● Disconnected",
        pos=(0, 240), height=28, color=error_color,
        font=text_font, bold=True, colorSpace='rgb'
    )
    
    # Motor imagery cue
    cue_circle = visual.Circle(
        win, radius=150, pos=(0, 0),
        fillColor=None, lineColor=None, lineWidth=1,
        colorSpace='rgb'
    )
    
    cue_text = visual.TextStim(
        win, text="",
        pos=(0, 0), height=120,
        font=text_font, bold=True, colorSpace='rgb'
    )
    
    instruction_text = visual.TextStim(
        win, text="",
        pos=(0, -200), height=24,
        color=text_color, font=text_font,
        colorSpace='rgb', wrapWidth=800
    )
    
    # Progress display
    progress_text = visual.TextStim(
        win, text="",
        pos=(0, 180), height=22,
        color=text_color, font=text_font,
        colorSpace='rgb'
    )
    
    # Countdown display
    countdown_text = visual.TextStim(
        win, text="",
        pos=(0, -50), height=80,
        color=rest_color, font=text_font, bold=True,
        colorSpace='rgb'
    )
    
    # Recording indicator
    recording_dot = visual.Circle(
        win, radius=8, pos=(-280, 240),
        fillColor=success_color, lineColor=None,
        colorSpace='rgb'
    )
    
    clock = core.Clock()
    
    def update_display():
        """Redraw the screen."""
        status_box.draw()
        status_text.draw()
        title.draw()
        win.flip()
    
    def show_instruction_screen(text, color=text_color):
        """Show instruction text on screen."""
        instruction_text.text = text
        instruction_text.color = color
        status_box.draw()
        status_text.draw()
        title.draw()
        progress_text.draw()
        instruction_text.draw()
        win.flip()
    
    def show_cue(direction, color, text_label):
        """Show the motor imagery cue."""
        cue_circle.fillColor = [c * 0.2 for c in color]
        cue_circle.lineColor = color
        cue_circle.lineWidth = 8
        cue_text.text = text_label
        cue_text.color = color
        
        status_box.draw()
        status_text.draw()
        title.draw()
        progress_text.draw()
        cue_circle.draw()
        cue_text.draw()
        instruction_text.draw()
        recording_dot.draw()
        win.flip()
    
    def show_rest_countdown(duration):
        """Show rest period with countdown."""
        start_time = time.time()
        while time.time() - start_time < duration:
            remaining = int(duration - (time.time() - start_time)) + 1
            countdown_text.text = str(remaining)
            
            instruction_text.text = "REST - Relax and prepare for next trial"
            instruction_text.color = rest_color
            
            status_box.draw()
            status_text.draw()
            title.draw()
            progress_text.draw()
            instruction_text.draw()
            countdown_text.draw()
            recording_dot.draw()
            win.flip()
            
            # Check for escape
            if 'escape' in event.getKeys():
                return False
            
            core.wait(0.1)
        
        countdown_text.text = ""
        return True
    
    def wait_for_keypress(key='space'):
        """Wait for a specific key press, return False if escape pressed."""
        event.clearEvents()
        while True:
            keys = event.getKeys()
            if 'escape' in keys:
                return False
            if key in keys:
                return True
            core.wait(0.01)
    
    # ========== PHASE 0: Device Selection ==========
    # Known Muse 2 devices — use Bluetooth name (not hardware serial)
    # The Muse broadcasts as "Muse-XXXX" over BLE; find yours in Bluetooth settings
    MUSE_DEVICES = {
        '1': ("Muse-15C3", "Muse #1 (15C3)"),
        '2': ("Muse-12A6",  "Muse #2 (12A6)"),
    }

    if selected_serial is None and not args.simulate:
        device_lines = "\n".join(
            f"Press {key} — {label}" for key, (_, label) in MUSE_DEVICES.items()
        )
        instruction_text.text = (
            "Select which Muse headband to use:\n\n"
            f"{device_lines}\n"
            "Press 0 — Auto (connect to any available Muse)"
        )
        instruction_text.color = text_color
        status_box.draw()
        status_text.draw()
        title.draw()
        instruction_text.draw()
        win.flip()

        device_chosen = False
        while not device_chosen:
            keys = event.getKeys()
            if 'escape' in keys:
                win.close()
                core.quit()
                return
            for key, (serial, _) in MUSE_DEVICES.items():
                if key in keys:
                    selected_serial = serial
                    device_chosen = True
                    break
            if '0' in keys:
                selected_serial = None  # auto-connect
                device_chosen = True
            core.wait(0.01)

    # Create recorder with selected device
    recorder = MusePsychopyRecorder(
        subject_name=args.name,
        subject_number=args.number,
        participant_id=args.name,
        blink_window_ms=500,
        serial_number=selected_serial,
        simulate=args.simulate,
    )

    # ========== PHASE 1: Connection ==========
    device_label = f" ({selected_serial})" if selected_serial else ""
    instruction_text.text = (
        "Simulation mode (synthetic EEG)\nPress SPACE to start the synthetic stream"
        if args.simulate else f"Press SPACE to connect to Muse headband{device_label}"
    )
    instruction_text.color = text_color
    update_display()
    instruction_text.draw()
    win.flip()
    
    # Wait for space to connect
    connected = False
    while not connected:
        keys = event.getKeys()
        if 'escape' in keys:
            win.close()
            core.quit()
            return
        if 'space' in keys:
            status_text.text = "● Connecting..."
            status_text.color = warning_color
            update_display()
            
            if recorder.connect():
                connected = True
                if recorder.start_streaming():
                    status_text.text = "● Simulation • Streaming" if args.simulate else "● Connected • Streaming"
                    status_text.color = success_color
                else:
                    status_text.text = "● Connection failed"
                    status_text.color = error_color
                    connected = False
            else:
                status_text.text = "● Connection failed - Press SPACE to retry"
                status_text.color = error_color
            update_display()
        core.wait(0.01)
    
    # ========== PHASE 2: Start Experiment ==========
    instruction_text.text = "Connected!\n\nPress ENTER to start the structured experiment\n\nYou will do 5 rounds of LEFT and RIGHT motor imagery,\nfollowed by a blink recording session.\nAudio instructions will guide you.\nPress SPACE when you finish each imagery task."
    instruction_text.color = success_color
    status_box.draw()
    status_text.draw()
    title.draw()
    instruction_text.draw()
    win.flip()
    
    fit_quality = visual.TextStim(win, text="", pos=(0, -330), height=16,
                                  wrapWidth=1180, color=text_color, colorSpace="rgb")

    # Wait for ENTER to start
    while True:
        keys = event.getKeys()
        if 'escape' in keys:
            recorder.stop_streaming()
            recorder.disconnect()
            win.close()
            core.quit()
            return
        if 'return' in keys:
            break
        status_box.draw()
        status_text.draw()
        title.draw()
        instruction_text.draw()
        fit_quality.text = format_quality(recorder.signal_quality)
        fit_quality.draw()
        win.flip()
        core.wait(0.1)
    
    # Start recording
    if not recorder.start_recording():
        status_text.text = "● Recording failed"
        status_text.color = error_color
        update_display()
        core.wait(2)
        recorder.stop_streaming()
        recorder.disconnect()
        win.close()
        core.quit()
        return
    
    status_text.text = "● Connected • Recording"
    status_text.color = success_color
    
    # ========== PHASE 3: Main Experiment Loop ==========
    # Audio instructions: full for first trial, short "grab left/right" for trials 2–10
    LEFT_INSTRUCTION_FULL = "Now, imagine reaching out with your left hand to grab a cup on your left side. Focus on the sensation of your left hand moving."
    RIGHT_INSTRUCTION_FULL = "Now, imagine reaching out with your right hand to grab a cup on your right side. Focus on the sensation of your right hand moving."
    LEFT_INSTRUCTION_SHORT = "Grab left"
    RIGHT_INSTRUCTION_SHORT = "Grab right"
    
    left_completed = 0
    right_completed = 0
    
    experiment_aborted = False
    
    for rep in range(NUM_REPETITIONS):
        if experiment_aborted:
            break
            
        # Update progress
        total_done = left_completed + right_completed
        total_trials = NUM_REPETITIONS * 2
        progress_text.text = f"Progress: {total_done}/{total_trials} trials | Round {rep + 1}/{NUM_REPETITIONS}"
        
        # ===== LEFT TRIAL =====
        # Show preparing screen
        show_instruction_screen(f"Round {rep + 1}/{NUM_REPETITIONS}: LEFT motor imagery\n\nListen to the audio instruction...", left_color)
        core.wait(0.5)
        
        # Play audio instruction (blocking - waits until speech is done). First trial: full; trials 2–10: "Grab left"
        left_instruction = LEFT_INSTRUCTION_FULL if rep == 0 else LEFT_INSTRUCTION_SHORT
        speak_text(left_instruction, wait=True)
        
        # Start LEFT trial immediately after audio
        trial = recorder.start_trial(
            TrialType.MOTOR_IMAGERY_LEFT,
            block_number=1,
            notes=f"Left motor imagery - Round {rep + 1}"
        )
        
        # Show visual cue
        instruction_text.text = "IMAGINE LEFT HAND MOVEMENT\nPress SPACE when you finish imagining"
        instruction_text.color = left_color
        show_cue("left", left_color, "← LEFT")
        
        # Wait for participant to press SPACE
        if not wait_for_keypress('space'):
            experiment_aborted = True
            break
        
        # End trial
        recorder.end_trial()
        left_completed += 1
        
        # Update progress
        total_done = left_completed + right_completed
        progress_text.text = f"Progress: {total_done}/{total_trials} trials | Round {rep + 1}/{NUM_REPETITIONS}"
        
        # Rest period
        if not show_rest_countdown(REST_DURATION):
            experiment_aborted = True
            break
        
        # ===== RIGHT TRIAL =====
        # Show preparing screen
        show_instruction_screen(f"Round {rep + 1}/{NUM_REPETITIONS}: RIGHT motor imagery\n\nListen to the audio instruction...", right_color)
        core.wait(0.5)
        
        # Play audio instruction (blocking). First trial: full; trials 2–10: "Grab right"
        right_instruction = RIGHT_INSTRUCTION_FULL if rep == 0 else RIGHT_INSTRUCTION_SHORT
        speak_text(right_instruction, wait=True)
        
        # Start RIGHT trial immediately after audio
        trial = recorder.start_trial(
            TrialType.MOTOR_IMAGERY_RIGHT,
            block_number=1,
            notes=f"Right motor imagery - Round {rep + 1}"
        )
        
        # Show visual cue
        instruction_text.text = "IMAGINE RIGHT HAND MOVEMENT\nPress SPACE when you finish imagining"
        instruction_text.color = right_color
        show_cue("right", right_color, "RIGHT →")
        
        # Wait for participant to press SPACE
        if not wait_for_keypress('space'):
            experiment_aborted = True
            break
        
        # End trial
        recorder.end_trial()
        right_completed += 1
        
        # Update progress
        total_done = left_completed + right_completed
        progress_text.text = f"Progress: {total_done}/{total_trials} trials | Round {rep + 1}/{NUM_REPETITIONS}"
        
        # Rest period (skip after last trial - will have rest before blink block)
        if rep < NUM_REPETITIONS - 1:
            if not show_rest_countdown(REST_DURATION):
                experiment_aborted = True
                break
    
    # ========== PHASE 4: Blink Block ==========
    blinks_completed = 0
    
    if not experiment_aborted:
        # Rest before blink block
        if not show_rest_countdown(REST_DURATION):
            experiment_aborted = True
        
        if not experiment_aborted:
            # Blink block instruction
            BLINK_INSTRUCTION = "Now we will record intentional blinks. When you hear the beep or see the prompt, blink deliberately once. Press B to record each blink."
            
            progress_text.text = f"Motor Imagery Complete! | Now: Blink Block (0/{TARGET_BLINKS})"
            show_instruction_screen(f"BLINK BLOCK\n\nWe will now record {TARGET_BLINKS} intentional blinks.\nPress 'B' each time you blink deliberately.\n\nListen to the instruction...", warning_color)
            core.wait(0.5)
            
            # Play audio instruction
            speak_text(BLINK_INSTRUCTION, wait=True)
            
            # Blink collection loop
            instruction_text.text = f"Press 'B' when you blink intentionally\nBlinks recorded: {blinks_completed}/{TARGET_BLINKS}\n\nPress ENTER when done with all blinks"
            instruction_text.color = warning_color
            
            while blinks_completed < TARGET_BLINKS and not experiment_aborted:
                # Update display
                progress_text.text = f"Blink Block | Blinks: {blinks_completed}/{TARGET_BLINKS}"
                instruction_text.text = f"Press 'B' when you blink intentionally\nBlinks recorded: {blinks_completed}/{TARGET_BLINKS}\n\nPress ENTER when done with all blinks"
                
                status_box.draw()
                status_text.draw()
                title.draw()
                progress_text.draw()
                instruction_text.draw()
                recording_dot.draw()
                win.flip()
                
                keys = event.getKeys()
                
                if 'escape' in keys:
                    experiment_aborted = True
                    break
                elif 'return' in keys:
                    # Allow early exit from blink block
                    break
                elif 'b' in keys:
                    # Record intentional blink
                    if recorder.add_blink_marker():
                        blinks_completed += 1
                        # Brief feedback
                        instruction_text.text = f"Blink recorded! ({blinks_completed}/{TARGET_BLINKS})"
                        instruction_text.color = success_color
                        status_box.draw()
                        status_text.draw()
                        title.draw()
                        progress_text.draw()
                        instruction_text.draw()
                        recording_dot.draw()
                        win.flip()
                        core.wait(0.3)
                        instruction_text.color = warning_color
                
                core.wait(0.01)
    
    # ========== PHASE 5: Completion ==========
    if experiment_aborted:
        instruction_text.text = "Experiment aborted. Saving data..."
        instruction_text.color = warning_color
    else:
        instruction_text.text = f"Experiment Complete!\n\nCompleted {left_completed} LEFT, {right_completed} RIGHT trials\nand {blinks_completed} intentional blinks.\n\nSaving data..."
        instruction_text.color = success_color
        speak_text("Experiment complete. Thank you for participating.", wait=False)
    
    status_box.draw()
    status_text.draw()
    title.draw()
    instruction_text.draw()
    progress_text.draw()
    win.flip()
    
    # Cleanup
    recorder.stop_recording()
    recorder.stop_streaming()
    recorder.disconnect()
    
    # Show final message
    if recorder.recording_error:
        instruction_text.text = f"Recording incomplete: {recorder.recording_error}\n\nPartial EEG data: {recorder.eeg_output_file}\n\nPress ESC to exit."
        instruction_text.color = error_color
    else:
        instruction_text.text = f"Data saved!\n\nEEG data: {recorder.eeg_output_file}\nTrial log: {recorder.trial_log_file}\n\nPress ESC to exit."
    status_box.draw()
    status_text.draw()
    title.draw()
    instruction_text.draw()
    win.flip()
    
    # Wait for ESC to close
    while 'escape' not in event.getKeys():
        core.wait(0.1)
    
    win.close()
    core.quit()


if __name__ == "__main__":
    run_experiment()
