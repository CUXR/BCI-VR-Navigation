# Programming Requirements

## Data Structure

### Unified Session Log Approach

**Core Principle**: Keep everything in one unified session log (same sheet/same file), with every usable training example as a segmented "trial row" with consistent columns.

**Benefits:**
- No need for separate sheets for blinks vs. imagery vs. baseline
- Filter by `trial_type` and `label` when building ML datasets
- Easier to see and process
- Consistent data structure across all trial types

### Trial Table Structure

Each trial should be represented as a row with consistent columns:

**Required Columns:**
- `trial_id`: Unique identifier for each trial
- `trial_type`: Type of trial (see Trial Types below)
- `label`: Class label for training
- `start_time`: Start timestamp (relative to session start or absolute)
- `end_time`: End timestamp (relative to session start or absolute)
- `event_marker`: For event-centered trials, the timestamp of the event
- `duration`: Duration of the trial segment
- `session_id`: Identifier linking to the session file

**Optional Columns:**
- `participant_id`
- `block_number`
- `trial_number`
- `notes`

## Trial Types and Data Extraction

### 1. Motor Imagery Trials

**Trial Type**: `motor_imagery_left` or `motor_imagery_right`

**Extraction Method**: Interval-based
- **Start marker**: Visual cue onset
- **End marker**: Spacebar press (participant-controlled)
- **Duration**: Extract the full interval from cue onset to spacebar press
- **Optional cap**: For consistency, can cap to fixed duration (e.g., first 1–2 seconds after cue)

**Data Structure:**
```
trial_type: "motor_imagery_left" | "motor_imagery_right"
start_time: <cue_onset_timestamp>
end_time: <spacebar_press_timestamp>
event_marker: null (or cue_onset_timestamp)
duration: end_time - start_time
```

**Forward walking imagery** (`motor_imagery_forward`, label `forward`) uses the same row shape, but the
trial has a fixed length (4 s by default, paced by one tone per imagined step) instead of ending on Space.
It is recorded in a randomized, balanced order with `baseline_quiet` "Relax" trials of the same length
and the same tones. Every participant hears one fixed script ("feel your own legs walking, one step per
tone"), practices, and passes an explain-back check first. The check answers are saved per session in
`imagery_check_YYYYMMDD_HHMMSS.json` next to the trial log; filter forward trials on them before training.

### 2. Blink Trials

**Trial Type**: `blink_intentional`

**Extraction Method**: Event-centered
- **Event marker**: Blink button press (time 0)
- **Window**: Extract EEG from -200 ms to +200 ms around the button press
- **Note**: Window size can be adjusted (e.g., ±200 ms or other final window choice)

**Data Structure:**
```
trial_type: "blink_intentional"
start_time: <button_press_timestamp> - 200ms
end_time: <button_press_timestamp> + 200ms
event_marker: <button_press_timestamp>
duration: 400ms (or chosen window size)
```

### 3. Baseline Trials

**Trial Types**: `baseline_quiet` or `baseline_active`

**Extraction Method**: Interval-based
- **Start marker**: Beginning of baseline period
- **End marker**: End of baseline period
- **Duration**: Full interval or fixed duration segments

**Data Structure:**
```
trial_type: "baseline_quiet" | "baseline_active"
start_time: <baseline_start_timestamp>
end_time: <baseline_end_timestamp>
event_marker: null
duration: end_time - start_time
```

## Data Processing Pipeline

### 1. Data Collection
- Record all trials in the unified session log
- Each trial gets a row with consistent column structure
- Timestamps should be synchronized with EEG data stream

### 2. Data Filtering
- Filter by `trial_type` when building specific ML datasets
- Filter by `label` for supervised learning
- Example: `trial_type == "motor_imagery_left" OR trial_type == "motor_imagery_right"` for motor imagery classifier

### 3. Feature Extraction
- Extract EEG features from the time windows specified in each trial row
- For motor imagery: use interval from `start_time` to `end_time`
- For blinks: use window around `event_marker` (±200 ms or chosen window)
- For baseline: use interval from `start_time` to `end_time`

### 4. Dataset Construction

**Supervised Training Sets:**
- **Motor Imagery Classifier**: Use `motor_imagery_left` and `motor_imagery_right` trials
- **Blink Classifier**: Use `blink_intentional` trials
- **Baseline Usage**: Can be included or excluded based on model performance

**Baseline Handling Strategy:**
- **Option A (Included)**: Use baseline as negative class alongside command classes
- **Option B (Excluded)**: Exclude baseline from supervised training, but use for:
  - Estimating typical feature ranges
  - Tuning rejection thresholds
  - Measuring false positive rates
- **Recommendation**: Start with Option B if baseline is too messy, then include if it improves performance

### 5. Evaluation and Thresholding

**Baseline for Evaluation:**
- Use baseline trials to:
  - Estimate typical feature ranges for non-command states
  - Tune rejection thresholds to reduce false positives
  - Measure false positive rate in realistic scenarios
- This helps build a safer detector without forcing the model to perfectly separate every baseline brain state from commands on day one

## Implementation Notes

### File Format
- Recommended: CSV, Parquet, or database table
- Ensure timestamps are precise and synchronized with EEG stream
- Include metadata columns for filtering and analysis

### Timestamp Synchronization
- All timestamps must be synchronized with the EEG data stream
- Use a common time reference (e.g., session start time or system clock)
- Ensure millisecond precision for blink trials (±200 ms windows)

### Data Validation
- Validate that `start_time < end_time` for all trials
- Validate that event-centered trials have valid `event_marker`
- Check that durations match expected ranges (e.g., blink trials ~400 ms)

### Export Functions
- Provide functions to filter trials by `trial_type`
- Provide functions to extract EEG segments based on trial specifications
- Provide functions to export datasets for ML training (e.g., X, y arrays)
