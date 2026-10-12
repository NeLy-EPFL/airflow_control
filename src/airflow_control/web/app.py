"""Browser GUI: FastAPI REST for commands, a WebSocket pushing live status."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from airflow_control.controller import Rig

STATIC = Path(__file__).parent / "static"


class FlowBody(BaseModel):
    setpoint: float = Field(ge=0)


class PuffBody(BaseModel):
    setpoint: float = Field(ge=0)
    duration_s: float = Field(gt=0)


def create_app(
    rig: Rig, *, poll: bool = True, request_exit: Callable[[], None] | None = None
) -> FastAPI:
    """``request_exit`` is called by ``POST /api/shutdown`` to stop the server."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if poll:
            rig.start_polling()
        try:
            yield
        finally:
            rig.close()  # every MFC back to 0 when the server goes down

    app = FastAPI(title="airflow_control", lifespan=lifespan)

    def run(fn, *args):
        try:
            fn(*args)
        except KeyError as e:
            raise HTTPException(404, str(e.args[0])) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except Exception as e:  # serial / Modbus failure
            raise HTTPException(503, f"MFC communication failed: {e}") from e
        return rig.status()

    @app.get("/api/status")
    def status():
        return rig.status()

    @app.get("/api/history")
    def history():
        """Recent (t, flow, setpoint) samples per MFC, so a reloaded page has its plot."""
        return {name: list(c.history) for name, c in rig.channels.items()}

    @app.post("/api/{name}/continuous")
    def set_continuous(name: str, body: FlowBody):
        return run(lambda: rig.channel(name).set_continuous(body.setpoint))

    @app.post("/api/{name}/puff")
    def puff(name: str, body: PuffBody):
        return run(lambda: rig.channel(name).puff(body.setpoint, body.duration_s))

    @app.post("/api/{name}/stop")
    def stop(name: str):
        return run(lambda: rig.channel(name).stop())

    @app.post("/api/stop")
    def stop_all():
        rig.stop_all("user")
        return rig.status()

    @app.post("/api/shutdown")
    def shutdown():
        if request_exit is None:
            raise HTTPException(501, "shutdown is not available in this server")
        rig.stop_all("shutdown from GUI")
        request_exit()  # uvicorn finishes this response, then runs the lifespan exit
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(rig.status())
                await asyncio.sleep(1.0 / rig.poll_hz)
        except (WebSocketDisconnect, RuntimeError):
            pass

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
