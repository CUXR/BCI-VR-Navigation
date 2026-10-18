"""Canonical label taxonomy for the final-realtime BCI pipeline.

Motor-imagery classes come in two families:
    * 2-class left / right hand MI (used by the app calibration flow and
      understood by Unity's FreeNavController as rotate left / right)
    * 4-class navigation MI (forward / backward / rotate-left / rotate-right)
      recorded by the Unity AutoMove collector
plus intentional blink, a "rest" trial type used as negative data, and an
internal "idle" used by the inference loop when nothing crosses threshold.

This module is the single source of truth for:
    - trial_type strings written to trial_log_*.csv during collection
    - integer marker codes injected into the BrainFlow stream / muxed CSV
    - canonical labels emitted on the prediction WebSocket
    - default per-class confidence thresholds for realtime gating
    - direction strings used by the Unity MarkerWebSocketClient

Keep this in sync with:
    [unity/environment_1/Assets/scripts/AutoMoveDataCollectionController.cs]
    [pipeline/collector.py]
    [eeg_to_meta/config.py]
"""

from __future__ import annotations

from typing import Dict, List


# ── Trial-type strings (written to trial_log_*.csv) ─────────────────
TRIAL_MI_FORWARD = "motor_imagery_forward"
TRIAL_MI_BACKWARD = "motor_imagery_backward"
TRIAL_MI_ROTATE_LEFT = "motor_imagery_rotate_left"
TRIAL_MI_ROTATE_RIGHT = "motor_imagery_rotate_right"
TRIAL_MI_LEFT = "motor_imagery_left"
TRIAL_MI_RIGHT = "motor_imagery_right"
TRIAL_BLINK = "blink_intentional"
TRIAL_REST = "rest"

MI_TRIAL_TYPES: List[str] = [
    TRIAL_MI_FORWARD,
    TRIAL_MI_BACKWARD,
    TRIAL_MI_ROTATE_LEFT,
    TRIAL_MI_ROTATE_RIGHT,
]

MI_TRIAL_TYPES_2CLASS: List[str] = [TRIAL_MI_LEFT, TRIAL_MI_RIGHT]

# Every MI trial type any collector can write, in canonical class order.
ALL_MI_TRIAL_TYPES: List[str] = MI_TRIAL_TYPES_2CLASS + MI_TRIAL_TYPES

ALL_TRIAL_TYPES: List[str] = MI_TRIAL_TYPES + [TRIAL_BLINK]


# ── Canonical class labels emitted on the prediction WebSocket ─────
LABEL_MI_FORWARD = "mi_forward"
LABEL_MI_BACKWARD = "mi_backward"
LABEL_MI_ROTATE_LEFT = "mi_rotate_left"
LABEL_MI_ROTATE_RIGHT = "mi_rotate_right"
# 2-class labels keep the legacy names Unity's FreeNavController already maps.
LABEL_MI_LEFT = "left_motor_imagery"
LABEL_MI_RIGHT = "right_motor_imagery"
LABEL_BLINK = "intentional_blink"
LABEL_IDLE = "idle"

MI_CLASS_LABELS: List[str] = [
    LABEL_MI_FORWARD,
    LABEL_MI_BACKWARD,
    LABEL_MI_ROTATE_LEFT,
    LABEL_MI_ROTATE_RIGHT,
]

ALL_CLASS_LABELS: List[str] = MI_CLASS_LABELS + [LABEL_BLINK]


# Stable integer index for the MI multiclass head (must match training).
MI_LABEL_TO_INT: Dict[str, int] = {
    LABEL_MI_FORWARD: 0,
    LABEL_MI_BACKWARD: 1,
    LABEL_MI_ROTATE_LEFT: 2,
    LABEL_MI_ROTATE_RIGHT: 3,
}
MI_INT_TO_LABEL: Dict[int, str] = {v: k for k, v in MI_LABEL_TO_INT.items()}


# ── Trial-type → canonical-label mapping ───────────────────────────
TRIAL_TYPE_TO_LABEL: Dict[str, str] = {
    TRIAL_MI_FORWARD: LABEL_MI_FORWARD,
    TRIAL_MI_BACKWARD: LABEL_MI_BACKWARD,
    TRIAL_MI_ROTATE_LEFT: LABEL_MI_ROTATE_LEFT,
    TRIAL_MI_ROTATE_RIGHT: LABEL_MI_ROTATE_RIGHT,
    TRIAL_MI_LEFT: LABEL_MI_LEFT,
    TRIAL_MI_RIGHT: LABEL_MI_RIGHT,
    TRIAL_BLINK: LABEL_BLINK,
    TRIAL_REST: "rest",
}


# ── Short class names used by the app / backend API ────────────────
# The frontend asks for e.g. ["left", "right", "blink"]; these map each
# short name to the trial type the calibration engine records. Adding
# forward/backward later is just a matter of listing them here.
CLASS_LEFT = "left"
CLASS_RIGHT = "right"
CLASS_FORWARD = "forward"
CLASS_BACKWARD = "backward"
CLASS_BLINK = "blink"

APP_CLASS_TO_TRIAL_TYPE: Dict[str, str] = {
    CLASS_LEFT: TRIAL_MI_LEFT,
    CLASS_RIGHT: TRIAL_MI_RIGHT,
    CLASS_FORWARD: TRIAL_MI_FORWARD,
    CLASS_BACKWARD: TRIAL_MI_BACKWARD,
    CLASS_BLINK: TRIAL_BLINK,
}
TRIAL_TYPE_TO_APP_CLASS: Dict[str, str] = {
    v: k for k, v in APP_CLASS_TO_TRIAL_TYPE.items()
}
APP_MI_CLASSES: List[str] = [CLASS_LEFT, CLASS_RIGHT, CLASS_FORWARD, CLASS_BACKWARD]
DEFAULT_APP_CLASSES: List[str] = [CLASS_LEFT, CLASS_RIGHT, CLASS_BLINK]


# ── BrainFlow / CSV marker codes ───────────────────────────────────
# Codes deliberately differ from the legacy 2-class L/R MI scheme
# (left_mi=1, right_mi=2) because the new pipeline records different
# semantics; data collected by pipeline.collector is gitignored and
# loaded only by the new trainer, so there is no in-flight collision.
MARKER_CODE_MI_FORWARD = 11
MARKER_CODE_MI_BACKWARD = 12
MARKER_CODE_MI_ROTATE_LEFT = 13
MARKER_CODE_MI_ROTATE_RIGHT = 14
MARKER_CODE_BLINK = 10  # matches legacy psychopy blink code
MARKER_CODE_MI_LEFT = 1   # matches legacy psychopy left_mi code
MARKER_CODE_MI_RIGHT = 2  # matches legacy psychopy right_mi code
MARKER_CODE_REST = 20

TRIAL_TYPE_TO_MARKER_CODE: Dict[str, int] = {
    TRIAL_MI_FORWARD: MARKER_CODE_MI_FORWARD,
    TRIAL_MI_BACKWARD: MARKER_CODE_MI_BACKWARD,
    TRIAL_MI_ROTATE_LEFT: MARKER_CODE_MI_ROTATE_LEFT,
    TRIAL_MI_ROTATE_RIGHT: MARKER_CODE_MI_ROTATE_RIGHT,
    TRIAL_MI_LEFT: MARKER_CODE_MI_LEFT,
    TRIAL_MI_RIGHT: MARKER_CODE_MI_RIGHT,
    TRIAL_BLINK: MARKER_CODE_BLINK,
    TRIAL_REST: MARKER_CODE_REST,
}

MARKER_CODE_TO_TRIAL_TYPE: Dict[int, str] = {
    v: k for k, v in TRIAL_TYPE_TO_MARKER_CODE.items()
}


# ── Unity AutoMove direction strings ↔ trial types ─────────────────
# AutoMoveDataCollectionController emits one of these direction values
# along with action_cue / action_start / action_end events. The marker
# server translates them to MI trial types (blinks are cued differently).
UNITY_DIRECTION_FORWARD = "forward"
UNITY_DIRECTION_BACKWARD = "backward"
UNITY_DIRECTION_LEFT_ROTATE = "left_rotate"
UNITY_DIRECTION_RIGHT_ROTATE = "right_rotate"

UNITY_DIRECTION_TO_TRIAL_TYPE: Dict[str, str] = {
    UNITY_DIRECTION_FORWARD: TRIAL_MI_FORWARD,
    UNITY_DIRECTION_BACKWARD: TRIAL_MI_BACKWARD,
    UNITY_DIRECTION_LEFT_ROTATE: TRIAL_MI_ROTATE_LEFT,
    UNITY_DIRECTION_RIGHT_ROTATE: TRIAL_MI_ROTATE_RIGHT,
}

TRIAL_TYPE_TO_UNITY_DIRECTION: Dict[str, str] = {
    v: k for k, v in UNITY_DIRECTION_TO_TRIAL_TYPE.items()
}


# ── Default per-class confidence thresholds for realtime gating ────
# Override at runtime via `pipeline.cli realtime --mi-threshold ...`
# or `--blink-threshold ...`; per-class fine tuning lives in
# eeg_to_meta/config.CLASS_THRESHOLDS.
DEFAULT_MI_THRESHOLD = 0.55
DEFAULT_BLINK_THRESHOLD = 0.60

DEFAULT_CLASS_THRESHOLDS: Dict[str, float] = {
    LABEL_MI_FORWARD: DEFAULT_MI_THRESHOLD,
    LABEL_MI_BACKWARD: DEFAULT_MI_THRESHOLD,
    LABEL_MI_ROTATE_LEFT: DEFAULT_MI_THRESHOLD,
    LABEL_MI_ROTATE_RIGHT: DEFAULT_MI_THRESHOLD,
    LABEL_BLINK: DEFAULT_BLINK_THRESHOLD,
}
