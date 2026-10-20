# Experiment Procedure

## Overview

This experiment protocol records EEG data by tightly controlling the timing of intentional actions while allowing natural, unintentional activity to be labeled separately. The protocol is designed for motor imagery classification applications, such as translating thoughts into actions for controlling games or helping handicapped individuals control their surroundings in smart home environments.

## Setup

1. **Environment**: Choose a comfortable and stable, well-lit location
2. **Seating**: Participant sits in a comfortable chair with arms parallel and resting on a table
3. **Pre-training**: Prior to the experiment, train the volunteer in meditation using a guided meditation app to help them achieve a relaxed state
4. **Visual Stimuli**: Place two cups of water on either side of the participant:
   - Each cup is 5 cm away from a hand
   - Cups are within the participant's line of sight
5. **Posture**: 
   - Participant sits comfortably
   - Raises chin slightly
   - Keeps head steady to avoid noise in the EEG data
   - Eyes can rotate left or right to look over the cups without moving the head

## Trial Types

### 1. Motor Imagery (Left and Right)

**Procedure:**
- Visual cues displayed on the interface indicate which imagery to perform
- Use distinct colors or symbols for left vs. right cues
- Optional text-to-speech support ensures participants clearly understand the instruction
- Each trial begins automatically with the visual cue
- Participants immediately start imagining the specified action (e.g., grabbing a cup with the left or right hand)
- Imagery should be brief and natural, reflecting real use cases rather than extended five-second holds
- When participants feel they have completed the imagined action, they press the space bar to mark the end of the segment

**Data Extraction:**
- EEG training data extracted from the interval between instruction onset and participant-controlled end marker
- This ensures consistent yet flexible segmentation aligned with intentional cognitive activity

### 2. Intentional Blinking

**Procedure:**
- Intentional blinks are treated as a dedicated, explicitly labeled dataset collected at a controlled time
- Participants can take a short rest whenever needed (e.g., after left and right imagery blocks)
- After resting, participants generate an intentional blink by pressing the blink button at the same moment they blink
- The button press serves as the event marker
- Collect approximately 10–20 intentional blink trials
- Standardized approach: perform at the end of the full experiment, asking participants to intentionally blink 10 times, each time paired with the button press

**Note on Unintentional Blinks:**
- Blinks that happen during the session while doing other tasks are generally not manually marked
- These are unintentional and participants often won't reliably notice them
- They appear in the EEG as "background" events during other segments
- Only the intentional blink class with clean button-press markers is trained and evaluated explicitly

### 3. Baseline Conditions

**Purpose:**
- Provide negative/non-command data for controlling false positives
- In real VR scenarios, people are always thinking, so a pure "think of nothing" baseline is not realistic

**Two Types of Baseline:**

1. **Quiet Rest (baseline_quiet)**
   - Eyes-open fixation
   - Low stimulation
   - Quiet rest period

2. **Active Engagement (baseline_active)**
   - Watch short videos
   - Scroll phone
   - Casual conversation
   - Resembles real-world "not issuing a command" states

**Usage:**
- Used as the "no-intent / other" class or for threshold calibration
- Can be excluded from supervised training if too messy, but still used for:
  - Estimating typical feature ranges
  - Tuning a rejection threshold
  - Measuring false positive rate
- This approach prevents baseline from "poisoning" command classes while still building a safer detector

## Experiment Flow

1. **Pre-experiment**: Guided meditation training
2. **Setup**: Position participant, place cups, adjust posture
3. **Motor Imagery Blocks**: 
   - Left imagery trials
   - Right imagery trials
   - Optional rest periods between blocks
4. **Baseline Collection**:
   - Quiet rest periods
   - Active engagement periods
5. **Intentional Blinking**: 
   - 10–20 intentional blink trials at the end of the experiment
   - Each blink paired with button press
