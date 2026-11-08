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
import subprocess
import platform
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from src.pilot_data_collection.session_recorder import (  # noqa: E402
    ExperimentState,
    SessionRecorder,
    Trial,
    TrialType,
)
from src.signal_quality.quality import format_quality  # noqa: E402

# Kept so older imports of the recorder from this module still work.
MusePsychopyRecorder = SessionRecorder


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
