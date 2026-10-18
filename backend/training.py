"""Run `pipeline.personalize` for one user as a subprocess.

A subprocess (rather than an in-process call) keeps the CPU-heavy fit off
the API event loop, and keeps ml_pipeline's bare `config` module from
colliding with eeg_to_meta's `config`, which the realtime stage imports
into this process.

Each training run gets its own folder so a worse retrain never silently
replaces the model the user is currently using:

    <user_dir>/models/<YYYYMMDD_HHMMSS>/model.pkl
    <user_dir>/models/<YYYYMMDD_HHMMSS>/report.json
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .settings import PROJECT_ROOT

log = logging.getLogger(__name__)

# Defaults the app flow uses: several overlapping windows per 4 s trial,
# skipping the first 0.5 s of reaction time after the "imagine" cue.
DEFAULT_MI_STRIDE_S = 0.5
DEFAULT_MI_OFFSET_S = 0.5


class TrainingError(RuntimeError):
    pass


@dataclass
class TrainingOptions:
    classes: list[str]
    mi_stride_s: float = DEFAULT_MI_STRIDE_S
    mi_offset_s: float = DEFAULT_MI_OFFSET_S


def models_dir(user_dir: Path) -> Path:
    return Path(user_dir) / "models"


def sessions_dir(user_dir: Path) -> Path:
    return Path(user_dir) / "sessions"


def latest_model(user_dir: Path) -> Optional[tuple[Path, dict]]:
    """Most recent successfully trained (model.pkl, report) for a user."""
    root = models_dir(user_dir)
    if not root.is_dir():
        return None
    for run_dir in sorted(root.iterdir(), reverse=True):
        model, report = run_dir / "model.pkl", run_dir / "report.json"
        if model.is_file() and report.is_file():
            data = json.loads(report.read_text())
            if "error" not in data:
                return model, data
    return None


async def train_user_model(user_dir: Path, options: TrainingOptions) -> tuple[Path, dict]:
    """Train on every recorded session of the user. Returns (model_path, report)."""
    run_dir = models_dir(user_dir) / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=False)
    model_path = run_dir / "model.pkl"
    report_path = run_dir / "report.json"

    cmd = [
        sys.executable, "-m", "pipeline.personalize",
        "--sessions-dir", str(sessions_dir(user_dir)),
        "--output", str(model_path),
        "--report-json", str(report_path),
        "--classes", ",".join(options.classes),
        "--mi-stride", str(options.mi_stride_s),
        "--mi-offset", str(options.mi_offset_s),
    ]
    log.info("Training: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(PROJECT_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    tail = output.decode(errors="replace").strip().splitlines()[-20:]

    report: dict = {}
    if report_path.is_file():
        report = json.loads(report_path.read_text())
    if proc.returncode != 0 or "error" in report or not model_path.is_file():
        message = report.get("error") or (tail[-1] if tail else f"exit code {proc.returncode}")
        log.error("Training failed: %s\n%s", message, "\n".join(tail))
        raise TrainingError(message)
    return model_path, report
