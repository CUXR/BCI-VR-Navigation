"""Tests for the app backend and the generalised personalization step.

Run from the repo root:  uv run pytest tests/ -q

Personalization runs through its CLI in a subprocess, as the backend does:
importing ml_pipeline's `config` into this process would shadow the
eeg_to_meta `config` the realtime stage needs.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend.api import create_app
from backend.calibration import CalibrationPlan
from backend.settings import BOARD_SIMULATED, Settings
from pipeline import labels as L
from pipeline.collector import TrialRow, write_eeg_csv, write_trial_log_csv

ROOT = Path(__file__).resolve().parent.parent
SFREQ = 256


# ── Calibration plan ───────────────────────────────────────────────


def test_schedule_is_balanced_and_shuffled():
    plan = CalibrationPlan(classes=["left", "right", "blink"], trials_per_class=12, seed=3)
    order = plan.schedule()
    assert len(order) == 36
    assert {c: order.count(c) for c in plan.classes} == {"left": 12, "right": 12, "blink": 12}
    assert order != ["left", "right", "blink"] * 12
    assert all(a != b for a, b in zip(order, order[1:]))


def test_for_duration_fits_budget():
    plan = CalibrationPlan.for_duration(300, classes=["left", "right", "blink"])
    assert plan.trials_per_class >= 1
    assert plan.estimated_duration_s() <= 300


@pytest.mark.parametrize("bad", [
    {"classes": ["up"]},
    {"classes": ["left", "left"]},
    {"imagine_s": 2.0},
    {"trials_per_class": 0},
])
def test_plan_validation(bad):
    with pytest.raises(ValueError):
        CalibrationPlan(**bad).validate()


# ── Personalization on a synthetic session ─────────────────────────


def _write_synthetic_session(session_dir: Path, *, trials_per_class: int = 10, seed: int = 0) -> None:
    """Muse-like recording: L/R imagery suppresses contralateral 10 Hz mu, blinks are frontal bumps."""
    rng = np.random.default_rng(seed)
    t0 = 1_700_000_000.0
    order = ["left", "right", "blink"] * trials_per_class
    rng.shuffle(order)

    events: list[tuple[str, str, int, float]] = []   # (trial_type, phase, idx, t)
    intents: list[tuple[float, float, str]] = []
    blinks: list[float] = []
    t = t0 + 2.0
    for idx, cls in enumerate(order):
        tt = L.APP_CLASS_TO_TRIAL_TYPE[cls]
        active = 1.0 if cls == "blink" else 4.0
        events += [(tt, "cue", idx, t), (tt, "start", idx, t + 1.0), (tt, "end", idx, t + 1.0 + active)]
        if cls == "blink":
            blinks.append(t + 1.0 + 0.4)
        else:
            intents.append((t + 1.0, t + 1.0 + active, cls))
        t += 1.0 + active
        events += [(L.TRIAL_REST, "start", idx, t), (L.TRIAL_REST, "end", idx, t + 2.0)]
        t += 2.0

    n = int((t + 2.0 - t0) * SFREQ)
    ts = t0 + np.arange(n) / SFREQ
    eeg = rng.normal(0, 6, size=(4, n))
    mu = 12 * np.sin(2 * np.pi * 10 * (ts - t0))
    left_gain = np.ones(n)
    right_gain = np.ones(n)
    for a, b, cls in intents:
        m = (ts >= a) & (ts <= b)
        (right_gain if cls == "left" else left_gain)[m] = 0.35
    eeg[0] += left_gain * mu
    eeg[3] += right_gain * mu
    for bt in blinks:
        bump = 180 * np.exp(-0.5 * ((ts - bt) / 0.08) ** 2)
        eeg[1] += bump
        eeg[2] += bump

    session_dir.mkdir(parents=True)
    write_eeg_csv(session_dir / "eeg_data.csv", (
        {"timestamp": ts[i], "marker_code": None, "marker_label": None,
         "TP9": eeg[0, i], "AF7": eeg[1, i], "AF8": eeg[2, i], "TP10": eeg[3, i]}
        for i in range(n)
    ))
    write_trial_log_csv(session_dir / "trial_log.csv", [
        TrialRow(
            trial_id=f"trial_r00_t{idx:04d}_{tt}", trial_type=tt,
            label=L.TRIAL_TYPE_TO_LABEL[tt], phase=phase, run_index=0,
            trial_index=idx, ts_unity=tp - t0, ts_recv=tp,
            marker_code=L.TRIAL_TYPE_TO_MARKER_CODE[tt],
            session_id=session_dir.name, participant_id="",
        )
        for tt, phase, idx, tp in events
    ])


def _personalize(sessions_dir: Path, out: Path, *extra: str) -> tuple[subprocess.CompletedProcess, dict]:
    report_path = out.with_suffix(".json")
    proc = subprocess.run(
        [sys.executable, "-m", "pipeline.personalize",
         "--sessions-dir", str(sessions_dir), "--output", str(out),
         "--report-json", str(report_path), *extra],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    return proc, report


def test_personalize_lr_blink_with_windows(tmp_path):
    _write_synthetic_session(tmp_path / "sessions" / "session_20260101_000000")
    proc, report = _personalize(
        tmp_path / "sessions", tmp_path / "model.pkl",
        "--classes", "left,right,blink", "--mi-stride", "0.5", "--mi-offset", "0.5",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert report["mi_classes"] == [L.LABEL_MI_LEFT, L.LABEL_MI_RIGHT]
    assert report["mi_trials_kept"] == {L.LABEL_MI_LEFT: 10, L.LABEL_MI_RIGHT: 10}
    # 3.5 s usable per trial, 2 s windows, 0.5 s stride → 4 windows per trial
    assert report["mi_windows_kept"] == {L.LABEL_MI_LEFT: 40, L.LABEL_MI_RIGHT: 40}
    assert report["blink_epochs"] == 10
    assert report["mi_cv_accuracy"] > 0.8
    assert report["blink_cv_accuracy"] > 0.9
    assert report["ready"] is True

    import pickle
    bundle = pickle.loads((tmp_path / "model.pkl").read_bytes())
    assert bundle["class_names"] == [L.LABEL_MI_LEFT, L.LABEL_MI_RIGHT]
    assert bundle["mi_pipeline"].n_features_in_ == 78     # eeg_to_meta MI features
    assert bundle["blink_pipeline"].n_features_in_ == 75  # eeg_to_meta blink features
    assert "participant_name" not in bundle and bundle["subjects"] == []


def test_personalize_detects_classes_and_legacy_single_window(tmp_path):
    _write_synthetic_session(tmp_path / "sessions" / "session_20260101_000000", trials_per_class=6)
    proc, report = _personalize(tmp_path / "sessions", tmp_path / "model.pkl")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert report["mi_classes"] == [L.LABEL_MI_LEFT, L.LABEL_MI_RIGHT]
    assert report["mi_windows_kept"] == {L.LABEL_MI_LEFT: 6, L.LABEL_MI_RIGHT: 6}
    assert report["ready"] is False  # fewer than the 8 trials per class required


def test_personalize_reports_missing_data(tmp_path):
    proc, report = _personalize(tmp_path / "empty", tmp_path / "model.pkl", "--classes", "left,right,blink")
    assert proc.returncode != 0
    assert "error" in report


# ── API end to end with the simulated headset ──────────────────────


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(client: TestClient, key: str, timeout: float) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get("/status").json()
        if not st[key]["running"]:
            return st
        time.sleep(0.25)
    raise AssertionError(f"{key} did not finish in {timeout}s")


def test_api_full_flow(tmp_path):
    settings = Settings(
        data_root=tmp_path, board=BOARD_SIMULATED, token="t0k",
        unity_ws_port=_free_port(),
    )
    with TestClient(create_app(settings), headers={"Authorization": "Bearer t0k"}) as client:
        assert client.get("/status", headers={"Authorization": "Bearer nope"}).status_code == 401
        assert client.post("/calibration/start", json={}).status_code == 409  # no session

        assert client.post("/session", json={"user_id": "auth0|abc"}).json()["stage"] == "connected"
        assert client.post("/realtime/start", json={}).status_code == 409    # no model yet
        assert client.post("/train", json={}).status_code == 409             # no data yet

        too_short = client.post("/calibration/start", json={"imagine_s": 2.0})
        assert too_short.status_code == 422
        r = client.post("/calibration/start", json={
            "trials_per_class": 3, "cue_s": 0.1, "imagine_s": 2.5,
            "blink_s": 0.8, "rest_s": 0.2, "rest_jitter_s": 0, "seed": 0,
        })
        assert r.status_code == 200, r.text
        assert client.post("/calibration/start", json={}).status_code == 409  # already running
        st = _wait(client, "calibration", 60)
        result = st["calibration"]["last_result"]
        assert result["status"] == "completed"
        assert result["trials_completed"] == {"left": 3, "right": 3, "blink": 3}
        # User id is never written to disk in clear text.
        assert "auth0|abc" not in str(result["session_dir"])

        assert client.post("/train", json={"classes": ["left", "right"]}).status_code == 422
        assert client.post("/train", json={}).status_code == 202
        st = _wait(client, "training", 300)
        assert st["training"]["last_result"]["status"] == "completed", st["training"]
        assert client.get("/model").json()["report"]["mi_classes"] == [L.LABEL_MI_LEFT, L.LABEL_MI_RIGHT]

        with client.websocket_connect("/events?token=t0k") as ws:
            assert ws.receive_json()["type"] == "status"
            r = client.post("/realtime/start", json={"mi_threshold": 0.6})
            assert r.status_code == 200, r.text
            types = set()
            deadline = time.time() + 15
            while "prediction" not in types and time.time() < deadline:
                types.add(ws.receive_json()["type"])
            assert {"realtime_started", "prediction"} <= types
        assert client.post("/calibration/start", json={}).status_code == 409  # busy in realtime
        assert client.post("/realtime/stop").status_code == 200
        assert client.delete("/session").status_code == 200
        assert client.get("/status").json()["session_open"] is False
