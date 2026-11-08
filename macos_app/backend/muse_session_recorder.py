"""Record one EEG session for the macOS app, with no window of its own.

The app runs the experiment UI (cues, speech, key presses) in SwiftUI and drives this helper,
which owns the Muse connection and writes the same files the PsychoPy recorder did:
`data/subNN/eeg_data_*.csv`, `trial_log_*.csv` (unified schema), `signal_quality_*.jsonl`,
and `metadata.yaml`. Run it with the repo root as the working directory.

Commands arrive as JSON lines on stdin. `t` is the Unix time of the key press or cue onset,
taken by the app on the same system clock BrainFlow stamps samples with:

    {"cmd": "start"}                                   start writing EEG and trials
    {"cmd": "start_trial", "trial_type": "motor_imagery_left", "block": 1, "notes": "...", "t": 1730000000.123}
    {"cmd": "end_trial", "t": 1730000003.456}
    {"cmd": "blink", "t": 1730000010.789}
    {"cmd": "stop"}                                    save and release the headband

Events go out as JSON lines on stdout:

    {"event": "status", "state": "connecting", "serial": "Muse-15C3", "attempt": 1}
    {"event": "status", "state": "connected", "serial": "Muse-15C3"}
    {"event": "status", "state": "signal_lost"}
    {"event": "recording"}
    {"event": "trial_started", "trial_id": "trial_0001", "trial_type": "motor_imagery_left"}
    {"event": "trial_ended", "trial_id": "trial_0001", "trial_type": "motor_imagery_left", "duration": 2.31}
    {"event": "blink", "trial_id": "trial_0021", "count": 1}
    {"event": "warning", "message": "..."}
    {"event": "saved", "eeg_file": "data/sub06/eeg_data_....csv", "trial_log_file": "data/sub06/trial_log_....csv"}
    {"event": "incomplete", "reason": "...", "eeg_file": "..."}
    {"event": "error", "message": "..."}
    {"event": "status", "state": "disconnected"}

SIGTERM, SIGINT, a closed stdin, or the app quitting all save what was recorded and release
the headband. The recorder's own progress prints go to stderr.

Usage, from the repo root:
    python macos_app/backend/muse_session_recorder.py --name "Alan Wu" --number 6 --serial Muse-15C3
    python macos_app/backend/muse_session_recorder.py --name test --number 99 --simulate
"""

import argparse
import json
import os
from pathlib import Path
import queue
import signal
import sys
import threading
import time

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from src.pilot_data_collection.session_recorder import SessionRecorder, TrialType  # noqa: E402

PROTOCOL_OUT = sys.stdout
# Everything the recorder prints is diagnostics; keep stdout for the line protocol.
sys.stdout = sys.stderr

POLL_S = 0.5
SIGNAL_LOST_S = 3.0


def emit(event, **fields):
    PROTOCOL_OUT.write(json.dumps({"event": event, **fields}, allow_nan=False) + "\n")
    PROTOCOL_OUT.flush()


def read_commands(commands):
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            commands.put(json.loads(line))
        except json.JSONDecodeError:
            print(f"Ignoring malformed command: {line}")
    commands.put({"cmd": "stop", "reason": "stdin closed"})


def stop_requested(commands):
    """Drain queued commands before recording starts; only `stop` matters then."""
    stop = False
    while True:
        try:
            if commands.get_nowait().get("cmd") == "stop":
                stop = True
        except queue.Empty:
            return stop


class Session:
    def __init__(self, recorder):
        self.recorder = recorder
        self.blinks = 0
        self.fatal = None

    def handle(self, cmd):
        name = cmd.get("cmd")
        recorder = self.recorder
        if name == "start":
            if recorder.is_recording:
                return
            if recorder.start_recording():
                emit("recording")
            else:
                self.fatal = f"Could not start recording: {recorder.recording_error or 'the EEG reader is not running'}"
            return
        if not recorder.is_recording:
            emit("warning", message=f"Ignored {name}: not recording yet.")
            return
        if name == "start_trial":
            try:
                trial_type = TrialType(cmd["trial_type"])
            except (KeyError, ValueError):
                emit("warning", message=f"Unknown trial type: {cmd.get('trial_type')}")
                return
            trial = recorder.start_trial(trial_type, block_number=cmd.get("block"),
                                         notes=cmd.get("notes", ""), at=cmd.get("t"))
            emit("trial_started", trial_id=trial.trial_id, trial_type=trial_type.value)
        elif name == "end_trial":
            trial = recorder.end_trial(at=cmd.get("t"))
            if trial is not None:
                emit("trial_ended", trial_id=trial.trial_id, trial_type=trial.trial_type.value,
                     duration=round(trial.duration, 3))
        elif name == "blink":
            if recorder.add_blink_marker(at=cmd.get("t")):
                self.blinks += 1
                emit("blink", trial_id=recorder.trials[-1].trial_id, count=self.blinks)
            else:
                emit("warning", message="That blink was not recorded. Press B again.")
        else:
            emit("warning", message=f"Unknown command: {name}")


def connect(recorder, serial, attempts, commands):
    for attempt in range(1, attempts + 1):
        emit("status", state="connecting", serial=serial, attempt=attempt)
        if recorder.connect(max_retries=1):
            return True
        if stop_requested(commands):
            return False
    return False


def report_saved(recorder):
    trial_log = recorder.trial_log_file if os.path.exists(recorder.trial_log_file) else None
    if recorder.recording_error:
        emit("incomplete", reason=recorder.recording_error, eeg_file=recorder.eeg_output_file,
             trial_log_file=trial_log)
    else:
        emit("saved", eeg_file=recorder.eeg_output_file, trial_log_file=trial_log)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True, help="Participant's full name")
    parser.add_argument("--number", type=int, required=True, help="Subject number, e.g. 6 -> data/sub06/")
    parser.add_argument("--serial", default=None, help="Muse Bluetooth name, e.g. Muse-15C3")
    parser.add_argument("--simulate", action="store_true", help="Synthetic EEG; saves under simulated_data/")
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()

    serial = args.serial
    if serial and not serial.startswith("Muse-"):
        serial = f"Muse-{serial}"
    label = "synthetic" if args.simulate else (serial or "any Muse")

    commands = queue.Queue()
    signal.signal(signal.SIGTERM, lambda *_: commands.put({"cmd": "stop"}))
    signal.signal(signal.SIGINT, lambda *_: commands.put({"cmd": "stop"}))
    threading.Thread(target=read_commands, args=(commands,), daemon=True).start()
    parent = os.getppid()

    recorder = SessionRecorder(subject_name=args.name, subject_number=args.number,
                               participant_id=args.name, blink_window_ms=500,
                               serial_number=serial, simulate=args.simulate)
    session = Session(recorder)
    exit_code = 0
    try:
        if not connect(recorder, label, args.attempts, commands):
            if recorder.board is None and not stop_requested(commands):
                emit("error", message=f"Could not connect to {label}. Make sure it is turned on, nearby, "
                                      "and not connected to another app.")
                exit_code = 2
            return exit_code
        if not recorder.start_streaming():
            emit("error", message=f"Connected to {label}, but the EEG stream did not start.")
            return 2
        emit("status", state="connected", serial=label)

        lost = False
        stale_since = None
        while True:
            try:
                cmd = commands.get(timeout=POLL_S)
            except queue.Empty:
                cmd = None
            if os.getppid() != parent:  # the app quit without signalling us
                cmd = {"cmd": "stop"}
            if cmd is not None:
                if cmd.get("cmd") == "stop":
                    break
                session.handle(cmd)
                if session.fatal:
                    emit("error", message=session.fatal)
                    exit_code = 2
                    break

            if not recorder.recording_thread.is_alive():
                emit("error", message=f"The headband stream stopped: {recorder.recording_error or 'reader exited'}")
                exit_code = 2
                break
            report = recorder.signal_quality
            if report is not None:
                reasons = report.get("stream_reasons", [])
                if "stale_stream" in reasons or "no_data" in reasons:
                    stale_since = stale_since or time.monotonic()
                    if not lost and time.monotonic() - stale_since >= SIGNAL_LOST_S:
                        lost = True
                        emit("status", state="signal_lost")
                else:
                    stale_since = None
                    if lost:
                        lost = False
                        emit("status", state="connected", serial=label)
        return exit_code
    finally:
        try:
            if recorder.is_recording:
                recorder.stop_recording()
                report_saved(recorder)
        finally:
            try:
                recorder.disconnect()
            except Exception as exc:  # releasing must not hide the save result
                print(f"Error while releasing the headband: {exc}")
            emit("status", state="disconnected")


if __name__ == "__main__":
    sys.exit(main())
