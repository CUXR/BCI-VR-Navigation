"""HTTP + WebSocket API the macOS app talks to (localhost only)."""

from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field

from pipeline import labels as L

from .calibration import MIN_IMAGINE_S, CalibrationPlan
from .events import EventBus
from .session import SessionManager, StateError
from .settings import BOARD_SIMULATED, Settings
from .training import DEFAULT_MI_OFFSET_S, DEFAULT_MI_STRIDE_S, TrainingOptions

log = logging.getLogger(__name__)


# ── Request bodies ─────────────────────────────────────────────────


class SessionRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=256, description="Stable OAuth subject id")
    serial: Optional[str] = Field(None, description="Muse name, e.g. Muse-15C3 or 15C3")


class CalibrationRequest(BaseModel):
    classes: list[str] = Field(default_factory=lambda: list(L.DEFAULT_APP_CLASSES))
    trials_per_class: Optional[int] = Field(None, ge=1, le=200)
    duration_s: Optional[float] = Field(None, gt=0, le=3600,
                                        description="Alternative to trials_per_class")
    cue_s: float = Field(1.5, gt=0, le=10)
    imagine_s: float = Field(4.0, ge=MIN_IMAGINE_S, le=15)
    blink_s: float = Field(2.0, gt=0, le=10)
    rest_s: float = Field(3.0, gt=0, le=30)
    rest_jitter_s: float = Field(1.0, ge=0, le=10)
    seed: Optional[int] = None


class TrainRequest(BaseModel):
    classes: list[str] = Field(default_factory=lambda: list(L.DEFAULT_APP_CLASSES))
    mi_stride_s: float = Field(DEFAULT_MI_STRIDE_S, gt=0, le=4)
    mi_offset_s: float = Field(DEFAULT_MI_OFFSET_S, ge=0, le=4)


class RealtimeRequest(BaseModel):
    mi_threshold: Optional[float] = Field(None, ge=0, le=1)
    blink_threshold: Optional[float] = Field(None, ge=0, le=1)
    unity_ws_host: Optional[str] = Field(None, description="Bind address for the Quest socket; 0.0.0.0 for Wi-Fi")
    unity_ws_port: Optional[int] = Field(None, ge=1024, le=65535)


class SimulateRequest(BaseModel):
    intent: Optional[str] = Field(None, description="left | right | blink | null (rest)")


def _validate_model_classes(classes: list[str]) -> None:
    unknown = [c for c in classes if c not in L.APP_CLASS_TO_TRIAL_TYPE]
    if unknown:
        raise HTTPException(422, f"Unknown class(es) {unknown}; valid: {sorted(L.APP_CLASS_TO_TRIAL_TYPE)}")
    if sum(c in L.APP_MI_CLASSES for c in classes) < 2:
        raise HTTPException(422, "Need at least two motor imagery classes")
    if L.CLASS_BLINK not in classes:
        raise HTTPException(422, "'blink' is required (the realtime engine needs a blink model)")


# ── App factory ────────────────────────────────────────────────────


def create_app(settings: Settings) -> FastAPI:
    bus = EventBus()
    manager = SessionManager(settings, bus)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        bus.bind_loop(asyncio.get_running_loop())
        yield
        await manager.close()

    app = FastAPI(title="BCI-VR backend", version="0.1.0", lifespan=lifespan)
    app.state.manager = manager
    app.state.bus = bus

    def _token_ok(given: Optional[str]) -> bool:
        if settings.token is None:
            return True
        return given is not None and hmac.compare_digest(given, settings.token)

    def require_token(request: Request) -> None:
        header = request.headers.get("authorization", "")
        given = header[7:] if header.lower().startswith("bearer ") else None
        if not _token_ok(given):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid token")

    def _state_error(exc: StateError) -> HTTPException:
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))

    auth = [Depends(require_token)]

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "board": settings.board}

    @app.get("/status", dependencies=auth)
    async def get_status() -> dict:
        return manager.status()

    @app.get("/classes", dependencies=auth)
    async def get_classes() -> dict:
        return {"available": sorted(L.APP_CLASS_TO_TRIAL_TYPE), "default": L.DEFAULT_APP_CLASSES}

    # ── session ──
    @app.post("/session", dependencies=auth)
    async def open_session(body: SessionRequest) -> dict:
        try:
            return await manager.open(body.user_id, body.serial)
        except StateError as exc:
            raise _state_error(exc)
        except ConnectionError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))

    @app.delete("/session", dependencies=auth)
    async def close_session() -> dict:
        await manager.close()
        return {"closed": True}

    # ── calibration ──
    @app.post("/calibration/start", dependencies=auth)
    async def start_calibration(body: CalibrationRequest) -> dict:
        timing = body.model_dump(exclude={"trials_per_class", "duration_s"})
        if body.duration_s is not None and body.trials_per_class is not None:
            raise HTTPException(422, "Give trials_per_class or duration_s, not both")
        if body.duration_s is not None:
            plan = CalibrationPlan.for_duration(body.duration_s, **timing)
        else:
            plan = CalibrationPlan(trials_per_class=body.trials_per_class or 15, **timing)
        try:
            plan.validate()
            return await manager.start_calibration(plan)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        except StateError as exc:
            raise _state_error(exc)

    @app.post("/calibration/stop", dependencies=auth)
    async def stop_calibration() -> dict:
        return {"result": await manager.stop_calibration()}

    # ── training ──
    @app.post("/train", dependencies=auth, status_code=status.HTTP_202_ACCEPTED)
    async def train(body: TrainRequest) -> dict:
        _validate_model_classes(body.classes)
        try:
            return await manager.start_training(TrainingOptions(
                classes=body.classes, mi_stride_s=body.mi_stride_s, mi_offset_s=body.mi_offset_s,
            ))
        except StateError as exc:
            raise _state_error(exc)

    @app.get("/model", dependencies=auth)
    async def get_model() -> dict:
        model = manager.status()["model"]
        if model is None:
            raise HTTPException(404, "No trained model for this user")
        return model

    # ── realtime ──
    @app.post("/realtime/start", dependencies=auth)
    async def start_realtime(body: RealtimeRequest) -> dict:
        try:
            return await manager.start_realtime(
                mi_threshold=body.mi_threshold,
                blink_threshold=body.blink_threshold,
                ws_host=body.unity_ws_host,
                ws_port=body.unity_ws_port,
            )
        except StateError as exc:
            raise _state_error(exc)
        except (RuntimeError, OSError) as exc:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc))

    @app.post("/realtime/stop", dependencies=auth)
    async def stop_realtime() -> dict:
        await manager.stop_realtime()
        return {"stopped": True}

    # ── simulated headset helper (only with --board simulated) ──
    if settings.board == BOARD_SIMULATED:
        @app.post("/debug/simulate", dependencies=auth)
        async def simulate(body: SimulateRequest) -> dict:
            if body.intent not in (None, "left", "right", "blink"):
                raise HTTPException(422, "intent must be left, right, blink or null")
            if manager.source is None or not manager.source.simulate_intent(body.intent):
                raise HTTPException(409, "No simulated session open")
            return {"intent": body.intent}

    # ── events ──
    @app.websocket("/events")
    async def events(ws: WebSocket) -> None:
        if not _token_ok(ws.query_params.get("token")):
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        await ws.accept()
        q = bus.subscribe()
        try:
            await ws.send_json({"type": "status", **manager.status()})
            while True:
                await ws.send_json(await q.get())
        except WebSocketDisconnect:
            pass
        finally:
            bus.unsubscribe(q)

    return app
