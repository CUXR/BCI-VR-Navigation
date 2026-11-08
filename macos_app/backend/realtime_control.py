"""Run real-time control for the macOS app: Muse EEG in, predictions to the app and to Unity.

Uses the same pieces as `eeg_to_meta/main.py` (ring buffer, InferenceEngine, PredictionSmoother,
and the Unity WebSocket server on ws://127.0.0.1:8765), so Unity receives exactly the frames it
gets from `python -m pipeline realtime`. Nothing is recorded to disk.

Commands arrive as JSON lines on stdin:

    {"cmd": "thresholds", "mi": 0.55, "blink": 0.60}    takes effect on the next tick
    {"cmd": "stop"}

Events go out as JSON lines on stdout:

    {"event": "status", "state": "connecting", "serial": "Muse-15C3", "attempt": 1}
    {"event": "status", "state": "connected", "serial": "Muse-15C3"}
    {"event": "status", "state": "signal_lost"}
    {"event": "model", "kind": "group", "subjects": 11, "trained": "2026-04-22T11:10:23"}
    {"event": "prediction", "intent": "left", "confidence": 0.72, "stable": true,
     "leaning": "left", "leaning_confidence": 0.72}
    {"event": "vr", "clients": 1}
    {"event": "warning", "message": "..."}
    {"event": "error", "message": "..."}
    {"event": "status", "state": "disconnected"}

`intent` is left, right, blink, or idle. A blink is reported on the tick the model detects it
(the smoother needs 3 of 5 agreeing ticks and never holds a single-tick blink). `leaning` is the
motor imagery class with the highest probability, even below threshold, for the confidence bar.

Usage, from the repo root:
    python macos_app/backend/realtime_control.py --serial Muse-15C3
    python macos_app/backend/realtime_control.py --mock
"""

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import signal
import sys
import threading
import time

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "eeg_to_meta"))

PROTOCOL_OUT = sys.stdout
# Library prints are diagnostics; keep stdout for the line protocol.
sys.stdout = sys.stderr
logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("realtime_control")

from buffer import RingBuffer  # noqa: E402
from config import CLASS_NAMES, CLASS_THRESHOLDS, MODEL_PATH, STRIDE_S, WS_HOST, WS_PORT  # noqa: E402
from inference import InferenceEngine  # noqa: E402
from model_loader import ModelBundle  # noqa: E402
from muse_stream import MockMuseStream, MuseBrainFlowStream  # noqa: E402
from smoothing import PredictionSmoother  # noqa: E402
from websocket_server import JsonWSServer  # noqa: E402

SIGNAL_LOST_S = 3.0
INTENTS = {
    "left_motor_imagery": "left",
    "mi_rotate_left": "left",
    "right_motor_imagery": "right",
    "mi_rotate_right": "right",
    "intentional_blink": "blink",
}


def emit(event, **fields):
    PROTOCOL_OUT.write(json.dumps({"event": event, **fields}, allow_nan=False) + "\n")
    PROTOCOL_OUT.flush()


def set_thresholds(mi=None, blink=None):
    if mi is not None:
        for key in CLASS_THRESHOLDS:
            if key.startswith("mi_") or key.endswith("_motor_imagery"):
                CLASS_THRESHOLDS[key] = float(mi)
    if blink is not None:
        CLASS_THRESHOLDS[CLASS_NAMES["blink"]] = float(blink)


def summarize(raw, smoothed):
    """Reduce one tick to what the app shows."""
    leaning, leaning_conf = None, 0.0
    for key, prob in raw.get("raw_probs", {}).items():
        intent = INTENTS.get(key)
        if intent in ("left", "right") and prob > leaning_conf:
            leaning, leaning_conf = intent, float(prob)
    if raw["label"] == CLASS_NAMES["blink"]:
        intent, confidence, stable = "blink", float(raw["confidence"]), True
    else:
        intent = INTENTS.get(smoothed["label"], "idle")
        confidence, stable = float(smoothed["confidence"]), bool(smoothed["stable"])
    return {
        "intent": intent,
        "confidence": round(confidence, 3),
        "stable": stable,
        "leaning": leaning,
        "leaning_confidence": round(leaning_conf, 3),
    }


def read_commands(loop, commands):
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            loop.call_soon_threadsafe(commands.put_nowait, json.loads(line))
        except json.JSONDecodeError:
            log.warning("Ignoring malformed command: %s", line)
    loop.call_soon_threadsafe(commands.put_nowait, {"cmd": "stop"})


async def connect(args, buf, label, stopping):
    loop = asyncio.get_running_loop()
    for attempt in range(1, args.attempts + 1):
        emit("status", state="connecting", serial=label, attempt=attempt)
        stream = MockMuseStream() if args.mock else MuseBrainFlowStream(serial=args.serial, simulate=args.simulate)
        try:
            await loop.run_in_executor(None, stream.start, buf)
            return stream
        except Exception as exc:
            log.warning("Connection attempt %d failed: %s", attempt, exc)
            try:
                stream.stop()
            except Exception:
                pass
            if stopping.is_set():
                return None
            await asyncio.sleep(1)
    return None


async def run(args):
    loop = asyncio.get_running_loop()
    commands = asyncio.Queue()
    stopping = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)
    threading.Thread(target=read_commands, args=(loop, commands), daemon=True).start()
    parent = os.getppid()
    set_thresholds(args.mi_threshold, args.blink_threshold)

    model = ModelBundle(args.model)
    if not model.load():
        emit("error", message=f"The model could not be loaded from {model.path}.")
        return 2
    info = getattr(model, "bundle", None) or {}
    emit("model", kind="group", subjects=len(info.get("subjects", []) or []),
         trained=str(info.get("training_date", "")) or None)

    ws_server = None
    if not args.no_ws:
        ws_server = JsonWSServer(host=WS_HOST, port=args.ws_port)
        ws_server.set_loop(loop)
        try:
            await ws_server.start()
        except OSError as exc:
            ws_server = None
            emit("warning", message=f"The VR link could not start on port {args.ws_port} ({exc.strerror}). "
                                    "Another real-time session may be running.")

    label = "synthetic" if (args.mock or args.simulate) else (args.serial or "any Muse")
    buf = RingBuffer(max_seconds=10.0)
    stream = await connect(args, buf, label, stopping)
    if stream is None:
        if ws_server:
            await ws_server.stop()
        if stopping.is_set():
            return 0
        emit("error", message=f"Could not connect to {label}. Make sure it is turned on, nearby, "
                              "and not connected to another app.")
        return 2
    emit("status", state="connected", serial=label)

    engine = InferenceEngine(model)
    smoother = PredictionSmoother()
    exit_code = 0
    lost, stale_since, vr_clients = False, None, -1
    try:
        while not stopping.is_set():
            t0 = time.perf_counter()
            while not commands.empty():
                cmd = commands.get_nowait()
                if cmd.get("cmd") == "stop":
                    stopping.set()
                elif cmd.get("cmd") == "thresholds":
                    set_thresholds(cmd.get("mi"), cmd.get("blink"))
                    log.info("Thresholds now %s", CLASS_THRESHOLDS)
            if stopping.is_set() or os.getppid() != parent:
                break
            if not stream.is_running:
                emit("error", message="The headband stream stopped. Check that it is still on and charged.")
                exit_code = 2
                break

            report = stream.quality.latest if stream.quality is not None else None
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
                if ws_server:
                    await ws_server.broadcast_quality(report)

            if ws_server and ws_server.n_clients != vr_clients:
                vr_clients = ws_server.n_clients
                emit("vr", clients=vr_clients)

            blink_win, mi_win = buf.get_blink_window(), buf.get_mi_window()
            if blink_win is not None or mi_win is not None:
                raw = engine.classify(mi_win, blink_win)
                smoothed = smoother.update(raw)
                if ws_server:
                    await ws_server.broadcast(smoothed)
                emit("prediction", **summarize(raw, smoothed))

            try:
                await asyncio.wait_for(stopping.wait(), max(0.0, STRIDE_S - (time.perf_counter() - t0)))
            except asyncio.TimeoutError:
                pass
        return exit_code
    finally:
        try:
            stream.stop()
        except Exception as exc:
            log.error("Error while releasing the headband: %s", exc)
        if ws_server:
            await ws_server.stop()
        emit("status", state="disconnected")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--serial", default=None, help="Muse Bluetooth name, e.g. Muse-15C3")
    parser.add_argument("--model", default=str(MODEL_PATH), help="Model bundle (.pkl); default is the group model")
    parser.add_argument("--mi-threshold", type=float, default=None)
    parser.add_argument("--blink-threshold", type=float, default=None)
    parser.add_argument("--ws-port", type=int, default=WS_PORT)
    parser.add_argument("--no-ws", action="store_true", help="Do not serve predictions to Unity")
    parser.add_argument("--mock", action="store_true", help="Synthetic in-process EEG, no headband")
    parser.add_argument("--simulate", action="store_true", help="BrainFlow synthetic board")
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()
    if args.serial and not args.serial.startswith("Muse-"):
        args.serial = f"Muse-{args.serial}"
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
