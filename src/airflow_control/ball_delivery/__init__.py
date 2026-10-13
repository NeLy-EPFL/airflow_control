"""Closed-loop ball delivery: a plugin (see :mod:`airflow_control.plugin`).

The ball is tracked in a side camera's live view (:mod:`.track`), and a state
machine (:mod:`.loop`) blows it to the animal with one MFC, turns the air off
at contact and blows it again once the animal has pushed it away.

Enabled with a ``[plugins.ball_delivery]`` table in the config, or with
``gui --ball`` (``--camera``/``--video`` imply it)::

    [plugins.ball_delivery]
    camera = "camera_LM"            # octacam camera with a side view of the track
    octacam_url = "127.0.0.1:8765"  # where the octacam GUI runs
    mfc = "air"                     # MFC that blows the ball (default: the first)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from airflow_control import config as config_mod
from airflow_control.plugin import Plugin

if TYPE_CHECKING:  # the tracking stack (numpy, scipy) loads only when enabled
    from airflow_control.ball_delivery.loop import Tracking
    from airflow_control.controller import Rig

NAME = "ball_delivery"


@dataclass
class BallDeliveryConfig:
    camera: str = ""  # octacam camera name, e.g. "camera_LM"
    octacam_url: str = "127.0.0.1:8765"  # octacam GUI address
    mfc: str = ""  # MFC the closed loop drives; default: the first one
    downsample: int = 4  # tracker downsampling of the preview frames


class BoxBody(BaseModel):
    kind: str  # "ball" | "ball_extra" | "platform"
    box: tuple[float, float, float, float]  # x0, x1, y0, y1 in frame px


class PointBody(BaseModel):
    x: float  # frame px
    y: float


class BallDelivery(Plugin):
    name = NAME
    static_dir = Path(__file__).parent / "static"

    def __init__(self, tracking: Tracking):
        self.tracking = tracking

    def start(self) -> None:
        self.tracking.start()

    def close(self) -> None:
        self.tracking.close()  # disarms

    def status(self) -> dict:
        return self.tracking.status()

    def on_manual(self, channel: str | None) -> None:
        if channel is None or channel == self.tracking.loop.channel.name:
            self.tracking.loop.disarm("manual", air_off=False)

    def add_routes(self, router: APIRouter, run) -> None:
        tr = self.tracking

        @router.get("/frame")
        def frame():
            """The latest camera frame (JPEG), with its detection in ``X-Detection``."""
            latest = tr.latest
            if latest is None:
                raise HTTPException(404, "no camera frame yet")
            f, det = latest
            return Response(
                f.to_jpeg(),
                media_type="image/jpeg",
                headers={
                    "Cache-Control": "no-store",
                    "X-Seq": str(f.seq),
                    "X-Detection": json.dumps(det.as_dict() if det else None),
                },
            )

        @router.get("/history")
        def history():
            return list(tr.history)

        @router.post("/box")
        def box(body: BoxBody):
            return run(lambda: tr.set_box(body.kind, body.box))

        @router.post("/contact")
        def contact(body: PointBody | None = None):
            return run(lambda: tr.set_contact(None if body is None else (body.x, body.y)))

        @router.post("/platform")
        def platform(body: PointBody):
            return run(lambda: tr.move_platform(body.x, body.y))

        @router.post("/clear")
        def clear():
            return run(tr.clear_calibration)

        @router.post("/params")
        def params(body: dict[str, float]):
            return run(lambda: tr.set_params(body))

        @router.post("/arm")
        def arm():
            return run(tr.arm)

        @router.post("/disarm")
        def disarm():
            return run(lambda: tr.loop.disarm("user"))


def build(
    cfg: config_mod.Config,
    rig: Rig,
    *,
    enabled: bool | None = None,
    sim: bool = False,
    camera: str | None = None,
    octacam: str | None = None,
    video: str | None = None,
) -> BallDelivery | None:
    """The plugin for ``gui``, or None. *enabled*: ``--ball/--no-ball`` (None:
    on when configured, or with *camera*/*video*). Frame source, by priority:
    *video*, *camera* (octacam), the simulated ball with *sim*, then the config."""
    from airflow_control.ball_delivery.loop import Tracking
    from airflow_control.ball_delivery.sim import SimBallSource, calibrate_sim
    from airflow_control.frames import OctacamSource, VideoSource

    raw = cfg.plugins.get(NAME)
    if enabled is None:
        enabled = raw is not None or bool(camera or video)
    if not enabled:
        return None
    bc = BallDeliveryConfig(**(raw or {}))
    channel = rig.channel(bc.mfc or next(iter(rig.channels)))
    state_root = config_mod.user_config_path().parent / NAME
    if video:
        source = VideoSource(video)
        state_dir = state_root / f"video-{Path(video).stem}"
    elif camera or (bc.camera and not sim):
        camera = camera or bc.camera
        source = OctacamSource(octacam or bc.octacam_url, camera)
        state_dir = state_root / camera
    elif sim:

        def flow():
            return channel.reading.flow if channel.reading else 0.0

        source, state_dir = SimBallSource(flow), None
    else:
        raise ValueError(
            f"ball delivery needs a camera: set `camera` in [plugins.{NAME}], "
            "or pass --camera, --video or --sim"
        )
    tracking = Tracking(
        source,
        channel,
        rig.events,
        state_dir=state_dir,
        log_dir=cfg.log_dir if rig.events.path else None,
        downsample=bc.downsample,
    )
    if isinstance(source, SimBallSource):
        calibrate_sim(tracking.tracker)
    return BallDelivery(tracking)
