"""Async WebSocket server that streams predictions to Unity / Meta Quest.

JSON prediction message format sent to connected clients:

    {
      "timestamp": 1711234567.89,
      "type": "prediction",
      "predicted_class": "mi_forward",
      "confidence": 0.73,
      "raw_probs": {"left": 0.73, "right": 0.27},
      "stable": true,
      "key_hint": "LeftArrow"
    }

State message format (sent on lifecycle transitions):

    {
      "type": "state",
      "state": "personalizing_start" | "personalizing_end" | "realtime_ready",
      "participant": "sub05",
      "message": "Personalising the ML model",
      "timestamp": 1711234567.89
    }

Unity client expectations:
    - Connect to  ws://<backend-ip>:8765
    - Messages are UTF-8 JSON, one per WebSocket frame
    - Valid navigation predictions are sent at most once every 3 seconds
      across all directions; blink predictions keep the inference cadence.
    - The "stable" flag indicates majority-vote agreement;
      ignore predictions where stable=false if you want cleaner input
    - "predicted_class" uses the canonical names:
        "intentional_blink",
        "left_motor_imagery", "right_motor_imagery"
      ("idle" is never sent over the socket — the client can treat silence as idle.)
    - "key_hint" is optional guidance for the Unity client (Unity KeyCode
      string). This server cannot inject OS-level keyboard events into
      Quest; your app should translate key_hint → gameplay / XR input.
"""

import asyncio
import json
import logging
import time

import websockets
from websockets.server import serve

from config import WS_HOST, WS_PORT, CLASS_NAMES, CLASS_THRESHOLDS, MI_CONFIDENCE_THRESHOLD

log = logging.getLogger(__name__)

_NAVIGATION_LABELS = {
    CLASS_NAMES["left"], CLASS_NAMES["right"],
    CLASS_NAMES["forward"], CLASS_NAMES["backward"],
    CLASS_NAMES["rotate_left"], CLASS_NAMES["rotate_right"],
}
_NAVIGATION_COOLDOWN_S = 3.0

# Suggested Unity KeyCode strings for each predicted_class label.
# Extend here if you add new model outputs (e.g. forward/back classes).
_KEY_HINT_BY_PREDICTED_CLASS: dict[str, str] = {
    CLASS_NAMES["idle"]: "I",
    CLASS_NAMES["blink"]: "B",
    CLASS_NAMES["left"]: "LeftArrow",
    CLASS_NAMES["right"]: "RightArrow",
    CLASS_NAMES["forward"]: "W",
    CLASS_NAMES["backward"]: "S",
    CLASS_NAMES["rotate_left"]: "A",
    CLASS_NAMES["rotate_right"]: "D",
}


def _key_hint_for_prediction(prediction: dict) -> str | None:
    label = prediction.get("label")
    if not isinstance(label, str):
        return None
    return _KEY_HINT_BY_PREDICTED_CLASS.get(label)


class JsonWSServer:
    """Thin async WebSocket server for broadcasting prediction dicts."""

    def __init__(self, host: str = WS_HOST, port: int = WS_PORT):
        self.host = host
        self.port = port
        self._clients: set[websockets.WebSocketServerProtocol] = set()
        self._server = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_navigation_sent_at = float("-inf")

    # ── lifecycle ───────────────────────────────────────────────────

    async def start(self):
        self._server = await serve(
            self._handler, self.host, self.port,
            ping_interval=20, ping_timeout=20,
        )
        log.info("WebSocket server listening on ws://%s:%d", self.host, self.port)
        if self.host in ("0.0.0.0", "::"):
            log.warning(
                "WebSocket bound on all interfaces: predictions are reachable "
                "from your LAN with no authentication (use a trusted network)."
            )

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            log.info("WebSocket server stopped")

    # ── client management ──────────────────────────────────────────

    async def _handler(self, ws: websockets.WebSocketServerProtocol):
        self._clients.add(ws)
        remote = ws.remote_address
        log.debug("Client connected: %s", remote)
        try:
            async for _msg in ws:
                pass  # we only send; ignore inbound messages
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)
            log.debug("Client disconnected: %s", remote)

    # ── broadcasting ───────────────────────────────────────────────

    async def broadcast(self, prediction: dict):
        """Send a prediction dict to all connected clients as JSON."""
        if not self._clients:
            return

        label = prediction.get("label", CLASS_NAMES["idle"])
        if label == CLASS_NAMES["idle"]:
            return

        navigation = label in _NAVIGATION_LABELS
        if navigation:
            if (not prediction.get("stable", False) or
                    prediction.get("confidence", 0.0) <
                    CLASS_THRESHOLDS.get(label, MI_CONFIDENCE_THRESHOLD)):
                return
            now = time.monotonic()
            if now - self._last_navigation_sent_at < _NAVIGATION_COOLDOWN_S:
                return
            previous_sent_at = self._last_navigation_sent_at
            self._last_navigation_sent_at = now

        key_hint = _key_hint_for_prediction(prediction)
        payload = {
            "type": "prediction",
            "timestamp": time.time(),
            "predicted_class": label,
            "confidence": round(prediction.get("confidence", 0.0), 4),
            "raw_probs": {
                k: round(v, 4) for k, v in prediction.get("raw_probs", {}).items()
            },
            "stable": prediction.get("stable", False),
        }
        if key_hint is not None:
            payload["key_hint"] = key_hint

        message = json.dumps(payload)

        stale = set()
        sent_count = 0
        for ws in self._clients:
            try:
                await ws.send(message)
                sent_count += 1
            except websockets.ConnectionClosed:
                stale.add(ws)
        self._clients -= stale
        if navigation:
            if sent_count:
                log.info("Sent navigation command %s to %d client(s)", label, sent_count)
            else:
                self._last_navigation_sent_at = previous_sent_at

    async def broadcast_quality(self, report: dict):
        """Send advisory quality independently of prediction thresholds/cooldowns."""
        message = json.dumps(report, allow_nan=False)
        stale = set()
        for ws in tuple(self._clients):
            try:
                await asyncio.wait_for(ws.send(message), timeout=0.2)
            except (websockets.ConnectionClosed, asyncio.TimeoutError):
                stale.add(ws)
        self._clients -= stale

    async def broadcast_state(self, state: str, *, participant: str | None = None, message: str | None = None):
        """Broadcast pipeline state events to all connected clients."""
        if not self._clients:
            return

        payload = {
            "type": "state",
            "state": state,
            "timestamp": time.time(),
        }
        if participant:
            payload["participant"] = participant
        if message:
            payload["message"] = message

        wire = json.dumps(payload)
        stale = set()
        for ws in self._clients:
            try:
                await ws.send(wire)
            except websockets.ConnectionClosed:
                stale.add(ws)
        self._clients -= stale

    def broadcast_sync(self, prediction: dict):
        """Fire-and-forget broadcast from a synchronous context.

        Safe to call from the inference thread; schedules the send on
        the event loop without blocking.
        """
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self.broadcast(prediction), self._loop)

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    @property
    def n_clients(self) -> int:
        return len(self._clients)
