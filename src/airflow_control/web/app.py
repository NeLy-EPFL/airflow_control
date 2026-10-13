"""Browser GUI: FastAPI REST for commands, a WebSocket pushing live status."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from airflow_control.controller import Rig
from airflow_control.plugin import Plugin

STATIC = Path(__file__).parent / "static"


class FlowBody(BaseModel):
    setpoint: float = Field(ge=0)


class PuffBody(BaseModel):
    setpoint: float = Field(ge=0)
    duration_s: float = Field(gt=0)


def create_app(
    rig: Rig,
    *,
    plugins: Sequence[Plugin] = (),
    poll: bool = True,
    request_exit: Callable[[], None] | None = None,
) -> FastAPI:
    """``request_exit`` is called by ``POST /api/shutdown`` to stop the server.
    ``plugins`` add their panels and routes (see :mod:`airflow_control.plugin`)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if poll:
            rig.start_polling()
        for p in plugins:
            p.start()
        try:
            yield
        finally:
            for p in plugins:
                p.close()  # let go of the MFCs before they go to 0
            rig.close()  # every MFC back to 0 when the server goes down

    app = FastAPI(title="airflow_control", lifespan=lifespan)

    def full_status() -> dict:
        st = rig.status()
        st["plugins"] = {p.name: p.status() for p in plugins}
        return st

    def run(fn: Callable[[], object]) -> dict:
        try:
            fn()
        except KeyError as e:
            raise HTTPException(404, str(e.args[0])) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except Exception as e:  # serial / Modbus failure
            raise HTTPException(503, f"MFC communication failed: {e}") from e
        return full_status()

    def manual(name: str | None = None) -> None:
        """A manual action on an MFC takes it back from any plugin."""
        for p in plugins:
            p.on_manual(name)

    @app.get("/api/status")
    def status():
        return full_status()

    @app.get("/api/history")
    def history():
        """Recent (t, flow, setpoint) samples per MFC, so a reloaded page has its plot."""
        return {name: list(c.history) for name, c in rig.channels.items()}

    # --- plugins (before /api/{name}/...) -----------------------------------
    for p in plugins:
        router = APIRouter(prefix=f"/api/plugins/{p.name}")
        p.add_routes(router, run)
        app.include_router(router)
        if p.static_dir:
            app.mount(f"/plugins/{p.name}", StaticFiles(directory=p.static_dir), name=p.name)

    # --- manual control ---------------------------------------------------
    @app.post("/api/{name}/continuous")
    def set_continuous(name: str, body: FlowBody):
        manual(name)
        return run(lambda: rig.channel(name).set_continuous(body.setpoint))

    @app.post("/api/{name}/puff")
    def puff(name: str, body: PuffBody):
        manual(name)
        return run(lambda: rig.channel(name).puff(body.setpoint, body.duration_s))

    @app.post("/api/{name}/stop")
    def stop(name: str):
        manual(name)
        return run(lambda: rig.channel(name).stop())

    @app.post("/api/stop")
    def stop_all():
        manual()
        rig.stop_all("user")
        return full_status()

    @app.post("/api/shutdown")
    def shutdown():
        if request_exit is None:
            raise HTTPException(501, "shutdown is not available in this server")
        manual()
        rig.stop_all("shutdown from GUI")
        request_exit()  # uvicorn finishes this response, then runs the lifespan exit
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(full_status())
                await asyncio.sleep(1.0 / rig.poll_hz)
        except (WebSocketDisconnect, RuntimeError):
            pass

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
