"""Send one short prediction at a time to the indoor Quest scene."""

import asyncio
import argparse
import json
import time

import websockets


LABELS = {
    "forward": "mi_forward",
    "backward": "mi_backward",
    "left": "mi_rotate_left",
    "right": "mi_rotate_right",
    "legacy-left": "left_motor_imagery",
    "legacy-right": "right_motor_imagery",
}
clients = set()


async def handle_client(socket):
    clients.add(socket)
    print("Quest connected", flush=True)
    try:
        async for _ in socket:
            pass
    except websockets.ConnectionClosed:
        pass
    finally:
        clients.discard(socket)
        print("Quest disconnected", flush=True)


async def main(host):
    async with websockets.serve(handle_client, host, 8765):
        print(f"Listening on ws://{host}:8765")
        while True:
            command = (await asyncio.to_thread(input, "Direction (forward/backward/left/right/legacy-left/legacy-right/low/unstable/q): ")).strip().lower()
            if command == "q":
                break
            if (command not in LABELS and command not in ("low", "unstable")) or not clients:
                print("Unknown direction or no Quest connected")
                continue
            label = LABELS.get(command, "mi_forward")
            for _ in range(5):
                frame = json.dumps({
                    "type": "prediction",
                    "timestamp": time.time(),
                    "predicted_class": label,
                    "confidence": 0.2 if command == "low" else 0.95,
                    "stable": command != "unstable",
                })
                await asyncio.gather(*(socket.send(frame) for socket in tuple(clients)))
                await asyncio.sleep(0.1)
            print("Sent", command)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address (use 0.0.0.0 for a Quest on the LAN)")
    asyncio.run(main(parser.parse_args().host))
