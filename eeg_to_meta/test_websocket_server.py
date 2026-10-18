"""Navigation command rate-limit checks for the Quest WebSocket."""

import asyncio
import json
import time
import unittest

from websocket_server import JsonWSServer


class RecordingSocket:
    def __init__(self):
        self.frames = []

    async def send(self, message):
        self.frames.append(json.loads(message))


class SlowSocket(RecordingSocket):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, message):
        self.entered.set()
        await self.release.wait()
        await super().send(message)


class JsonWSServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_quality_is_sent_during_idle_without_using_navigation_slot(self):
        server = JsonWSServer()
        socket = RecordingSocket()
        server._clients.add(socket)
        await server.broadcast({"label": "idle"})
        report = {"type": "signal_quality", "channels": {"AF7": {"status": "poor"}}}
        await server.broadcast_quality(report)
        self.assertEqual(socket.frames, [report])
        self.assertEqual(server._last_navigation_sent_at, float("-inf"))

    async def test_slow_quality_client_is_removed_without_waiting_forever(self):
        server = JsonWSServer()
        socket = SlowSocket()
        server._clients.add(socket)
        await asyncio.wait_for(server.broadcast_quality({"type": "signal_quality"}), timeout=0.5)
        self.assertNotIn(socket, server._clients)

    async def test_only_one_valid_navigation_command_per_three_seconds(self):
        server = JsonWSServer()
        socket = RecordingSocket()
        server._clients.add(socket)

        async def send(label, confidence=0.8, stable=True):
            await server.broadcast({
                "label": label,
                "confidence": confidence,
                "stable": stable,
                "raw_probs": {},
            })

        await send("left_motor_imagery")
        await send("right_motor_imagery")
        await send("mi_forward")
        await send("intentional_blink")
        await send("intentional_blink")
        self.assertEqual(
            [frame["predicted_class"] for frame in socket.frames],
            ["left_motor_imagery", "intentional_blink", "intentional_blink"],
        )

        server._last_navigation_sent_at = time.monotonic() - 3.01
        await send("mi_backward", stable=False)
        await send("mi_rotate_left", confidence=0.2)
        await send("right_motor_imagery")
        await send("mi_rotate_right")
        self.assertEqual(socket.frames[-1]["predicted_class"], "right_motor_imagery")
        self.assertEqual(len(socket.frames), 4)

    async def test_no_client_does_not_use_navigation_slot(self):
        server = JsonWSServer()
        prediction = {
            "label": "mi_forward", "confidence": 0.8,
            "stable": True, "raw_probs": {},
        }
        await server.broadcast(prediction)

        socket = RecordingSocket()
        server._clients.add(socket)
        await server.broadcast(prediction)
        self.assertEqual(len(socket.frames), 1)

    async def test_overlapping_broadcasts_share_one_slot(self):
        server = JsonWSServer()
        socket = SlowSocket()
        server._clients.add(socket)
        prediction = {
            "label": "left_motor_imagery", "confidence": 0.8,
            "stable": True, "raw_probs": {},
        }

        first = asyncio.create_task(server.broadcast(prediction))
        await socket.entered.wait()
        await server.broadcast({**prediction, "label": "right_motor_imagery"})
        socket.release.set()
        await first
        self.assertEqual(len(socket.frames), 1)


if __name__ == "__main__":
    unittest.main()
