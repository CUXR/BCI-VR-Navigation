"""Runtime settings for the backend service."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

BOARD_MUSE = "muse"            # real Muse 2 over BLE
BOARD_SYNTHETIC = "synthetic"  # BrainFlow's synthetic board (250 Hz, random)
BOARD_SIMULATED = "simulated"  # in-process Muse-like signal that reacts to cues
BOARD_KINDS = (BOARD_MUSE, BOARD_SYNTHETIC, BOARD_SIMULATED)


@dataclass
class Settings:
    data_root: Path = PROJECT_ROOT / "data" / "app"
    board: str = BOARD_MUSE
    serial: Optional[str] = None
    # Shared secret the app passes as `Authorization: Bearer <token>` (HTTP)
    # or `?token=<token>` (WebSocket). None disables auth (dev only).
    token: Optional[str] = None
    # Prediction socket Unity / Quest connects to (eeg_to_meta protocol).
    unity_ws_host: str = "127.0.0.1"
    unity_ws_port: int = 8765

    def user_dir(self, user_id: str) -> Path:
        return self.data_root / "users" / user_key(user_id)


def user_key(user_id: str) -> str:
    """Stable, filesystem-safe folder name for an OAuth user id.

    Hashing keeps raw identifiers (emails, provider subs) out of paths and
    rules out path traversal from a crafted id.
    """
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]
