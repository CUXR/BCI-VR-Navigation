"""Output smoothing and debounce for real-time predictions.

Prevents noisy one-off predictions from triggering actions in Unity.
Uses a sliding window of recent predictions with majority vote and
confidence averaging.
"""

import time
from collections import deque

from config import SMOOTHING_WINDOW, STABILITY_MIN_AGREE, CLASS_NAMES


class PredictionSmoother:
    """Majority-vote + confidence smoothing over a sliding window.

    Parameters
    ----------
    window_size : int
        Number of recent predictions to keep (default from config).
    stability_threshold : int
        Minimum agreeing predictions within the window to declare the
        output "stable" (default from config).
    """

    def __init__(
        self,
        window_size: int = SMOOTHING_WINDOW,
        stability_threshold: int = STABILITY_MIN_AGREE,
    ):
        self.window_size = window_size
        self.stability_threshold = stability_threshold
        self._history: deque[dict] = deque(maxlen=window_size)
        self._last_emitted_label = CLASS_NAMES["idle"]
        self._last_emit_time = 0.0

    def update(self, prediction: dict) -> dict:
        """Add a new prediction and return the smoothed result.

        Args:
            prediction: dict from InferenceEngine.classify(), must have
                        "label", "confidence", "raw_probs".

        Returns:
            Smoothed dict with extra keys:
                "stable"    : bool — True if majority agrees
                "smoothed"  : bool — always True (marks this as post-smoothing)
                "raw_label" : str  — the unsmoothed label from this tick
        """
        raw_label = prediction["label"]

        # Blinks are single-tick events: the engine fires at most one per
        # BLINK_COOLDOWN_S, so a majority vote over SMOOTHING_WINDOW ticks
        # would always suppress them. Pass them straight through, and keep
        # them out of the history so they don't disturb the MI vote.
        if raw_label == CLASS_NAMES["blink"]:
            return {
                "label": raw_label,
                "internal_label": prediction.get("internal_label", "blink"),
                "raw_label": raw_label,
                "confidence": round(prediction.get("confidence", 0.0), 4),
                "raw_probs": prediction.get("raw_probs", {}),
                "stable": True,
                "smoothed": True,
                "latency_ms": prediction.get("latency_ms", 0.0),
            }

        self._history.append(prediction)

        # Majority vote across the window
        label_counts: dict[str, int] = {}
        confidence_sums: dict[str, float] = {}

        for entry in self._history:
            lbl = entry["label"]
            label_counts[lbl] = label_counts.get(lbl, 0) + 1
            confidence_sums[lbl] = confidence_sums.get(lbl, 0.0) + entry.get("confidence", 0.0)

        majority_label = max(label_counts, key=label_counts.get)
        majority_count = label_counts[majority_label]
        avg_confidence = confidence_sums[majority_label] / majority_count if majority_count else 0.0

        stable = majority_count >= self.stability_threshold

        # Debounce: don't flip from a non-idle state back to idle on
        # a single frame of disagreement.
        if not stable and self._last_emitted_label != CLASS_NAMES["idle"]:
            majority_label = self._last_emitted_label

        self._last_emitted_label = majority_label

        # Merge raw_probs from the latest prediction
        return {
            "label": majority_label,
            "internal_label": prediction.get("internal_label", "idle"),
            "raw_label": raw_label,
            "confidence": round(avg_confidence, 4),
            "raw_probs": prediction.get("raw_probs", {}),
            "stable": stable,
            "smoothed": True,
            "latency_ms": prediction.get("latency_ms", 0.0),
        }

    def reset(self):
        """Clear history (e.g. on stream restart)."""
        self._history.clear()
        self._last_emitted_label = CLASS_NAMES["idle"]
        self._last_emit_time = 0.0
