#!/usr/bin/env python3
"""End-to-end Muse EEG → Meta Quest inference pipeline.

    ┌─────────┐   push    ┌────────┐  window  ┌───────────┐
    │  Muse   │ ────────► │ Ring   │ ───────► │ Inference │
    │ Stream  │  samples  │ Buffer │          │  Engine   │
    └─────────┘           └────────┘          └─────┬─────┘
                                                    │
                                              prediction
                                                    │
                                              ┌─────▼──────┐
                                              │  Smoother  │
                                              └─────┬──────┘
                                                    │
                                              ┌─────▼──────┐   JSON
                                              │ WebSocket  │ ────────► Unity / Quest
                                              │  Server    │
                                              └────────────┘

Usage:
    python main.py                    # live Muse 2 + WebSocket
    python main.py --mock             # synthetic EEG for testing
    python main.py --mock --no-ws     # headless, no WebSocket
    python main.py --model <path>     # custom model path
"""

import argparse
import asyncio
import logging
import signal
import sys
import time
import threading

from config import (
    SFREQ, STRIDE_S,
    MI_WINDOW_SAMPLES, BLINK_WINDOW_SAMPLES,
    WS_HOST, WS_PORT, PROJECT_ROOT, CLASS_THRESHOLDS,
)
from buffer import RingBuffer
from model_loader import ModelBundle
from muse_stream import MuseBrainFlowStream, MockMuseStream
from inference import InferenceEngine
from smoothing import PredictionSmoother
from websocket_server import JsonWSServer
from src.signal_quality.quality import format_quality

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")


def parse_args():
    p = argparse.ArgumentParser(description="Muse EEG → Meta Quest pipeline")
    p.add_argument("--mock", action="store_true",
                   help="Use synthetic EEG stream (no Muse needed)")
    p.add_argument("--simulate", action="store_true",
                   help="Use BrainFlow synthetic board")
    p.add_argument("--serial", type=str, default=None,
                   help="Muse BLE serial number")
    p.add_argument("--model", type=str, default=None,
                   help="Path to .pkl model bundle")
    p.add_argument("--participant", type=str, default=None,
                   help="Participant id used for personalised bundle lookup (e.g. 05)")
    p.add_argument("--mi-threshold", type=float, default=None,
                   help="Override confidence threshold for all MI classes")
    p.add_argument("--blink-threshold", type=float, default=None,
                   help="Override confidence threshold for intentional blink")
    p.add_argument("--no-ws", action="store_true",
                   help="Disable WebSocket server (terminal only)")
    p.add_argument("--ws-host", type=str, default=WS_HOST,
                   help="WebSocket bind address (default from config: localhost; use 0.0.0.0 for LAN/Quest)")
    p.add_argument("--ws-port", type=int, default=WS_PORT,
                   help=f"WebSocket port (default {WS_PORT})")
    return p.parse_args()


async def run_pipeline(args):
    # ── 0. Resolve model + thresholds ───────────────────────────────
    if args.model is None and args.participant:
        pid = str(args.participant).replace("sub", "").strip()
        if pid.isdigit():
            participant_model = PROJECT_ROOT / "ml_pipeline" / "models" / "participants" / f"sub{int(pid):02d}.pkl"
            if participant_model.exists():
                args.model = str(participant_model)
                log.info("Using personalised model for sub%s: %s", f"{int(pid):02d}", participant_model)

    if args.model is None:
        base_4class = PROJECT_ROOT / "ml_pipeline" / "models" / "realtime_models_4class.pkl"
        if base_4class.exists():
            args.model = str(base_4class)
            log.info("Using base 4-class model: %s", base_4class)

    if args.mi_threshold is not None:
        for k in list(CLASS_THRESHOLDS.keys()):
            if k.startswith("mi_") or k.endswith("_motor_imagery"):
                CLASS_THRESHOLDS[k] = float(args.mi_threshold)
    if args.blink_threshold is not None:
        CLASS_THRESHOLDS["intentional_blink"] = float(args.blink_threshold)

    # ── 1. Load model ──────────────────────────────────────────────
    model = ModelBundle(args.model)
    if not model.load():
        log.error("Cannot start without a valid model. Exiting.")
        sys.exit(1)

    # ── 2. Build components ────────────────────────────────────────
    buf = RingBuffer(max_seconds=10.0)
    engine = InferenceEngine(model)
    smoother = PredictionSmoother()

    ws_server: JsonWSServer | None = None
    if not args.no_ws:
        ws_server = JsonWSServer(host=args.ws_host, port=args.ws_port)
        ws_server.set_loop(asyncio.get_event_loop())
        await ws_server.start()
        if args.participant:
            await ws_server.broadcast_state(
                "personalizing_end",
                participant=f"sub{str(args.participant).replace('sub', '').zfill(2)}",
                message="Personalising complete",
            )
            await ws_server.broadcast_state(
                "realtime_ready",
                participant=f"sub{str(args.participant).replace('sub', '').zfill(2)}",
                message="Realtime pipeline ready",
            )

    # ── 3. Start EEG stream ────────────────────────────────────────
    if args.mock:
        stream = MockMuseStream()
    else:
        stream = MuseBrainFlowStream(serial=args.serial, simulate=args.simulate)
    stream.start(buf)

    log.info("Pipeline running.  Ctrl+C to stop.")
    if ws_server:
        log.info("Unity client → connect to  ws://%s:%d", args.ws_host, args.ws_port)
        log.info("Active class thresholds: %s", CLASS_THRESHOLDS)
    log.info("Waiting for buffer to fill (%d MI / %d blink samples)...",
             MI_WINDOW_SAMPLES, BLINK_WINDOW_SAMPLES)

    # ── 4. Inference loop ──────────────────────────────────────────
    last_quality_at = float("-inf")
    tick = 0
    last_recognized_intent = None
    try:
        while True:
            t0 = time.perf_counter()

            if stream.quality is not None and t0 - last_quality_at >= 0.5:
                quality = stream.quality.latest
                log.info("%s", format_quality(quality))
                if ws_server and quality is not None:
                    await ws_server.broadcast_quality(quality)
                last_quality_at = t0

            blink_win = buf.get_blink_window()
            mi_win = buf.get_mi_window()

            if blink_win is None and mi_win is None:
                await asyncio.sleep(STRIDE_S)
                continue

            raw_pred = engine.classify(mi_win, blink_win)
            smoothed = smoother.update(raw_pred)

            recognized_intent = (
                smoothed["label"]
                if smoothed["stable"] and smoothed["label"] in
                ("left_motor_imagery", "right_motor_imagery")
                else None
            )
            if recognized_intent != last_recognized_intent and recognized_intent:
                direction = "left" if recognized_intent == "left_motor_imagery" else "right"
                log.info("Recognized %s intent (confidence=%.2f)", direction, smoothed["confidence"])
            if smoothed["stable"]:
                last_recognized_intent = recognized_intent

            # broadcast to Unity clients
            if ws_server:
                await ws_server.broadcast(smoothed)

            # Blink: every tick; left/right: ~2 Hz; other status: ~1 Hz.
            tick += 1
            label = smoothed["label"]
            if (label == "intentional_blink" or smoothed["raw_label"] == "intentional_blink" or
                (label in ("left_motor_imagery", "right_motor_imagery") and tick % 5 == 0) or
                tick % 10 == 0):
                wall = (time.perf_counter() - t0) * 1000
                stable_tag = "STABLE" if smoothed["stable"] else "      "
                log.info(
                    "%s  %-25s  conf=%.2f  infer=%.1fms  ws_clients=%d  %s",
                    stable_tag,
                    smoothed["label"],
                    smoothed["confidence"],
                    smoothed["latency_ms"],
                    ws_server.n_clients if ws_server else 0,
                    f"raw={smoothed['raw_label']}" if smoothed["label"] != smoothed["raw_label"] else "",
                )

            elapsed = time.perf_counter() - t0
            sleep_for = max(0, STRIDE_S - elapsed)
            await asyncio.sleep(sleep_for)

    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutting down...")
    finally:
        stream.stop()
        if ws_server:
            await ws_server.stop()
        log.info("Done.")


def main():
    args = parse_args()
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    try:
        asyncio.run(run_pipeline(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
