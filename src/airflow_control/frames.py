"""Live camera frames, for plugins that track something in a camera view.

- :class:`OctacamSource`: the preview stream of a running octacam GUI. It
  connects to octacam's WebSocket as one more client and asks for a single
  camera only. octacam is not modified, and a crash here cannot touch a
  recording.
- :class:`VideoSource`: a video file played in real time (needs ``ffmpeg``),
  to tune a tracker and test without the rig.

Every source calls ``on_frame(Frame)`` from its own thread.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from PIL import Image

log = logging.getLogger("airflow_control")


@dataclass(frozen=True)
class Frame:
    image: np.ndarray  # uint8, (h, w), grayscale
    t: float  # arrival time, time.time()
    seq: int  # frame number from the source
    jpeg: bytes | None = None  # encoded image for the GUI, if the source has one
    source_ts_ns: int | None = None  # source's own timestamp (octacam: preview encode time)
    recording: bool = False  # octacam was recording

    def to_jpeg(self) -> bytes:
        if self.jpeg is not None:
            return self.jpeg
        buf = io.BytesIO()
        Image.fromarray(self.image).save(buf, "JPEG", quality=80)
        return buf.getvalue()


FrameCallback = Callable[[Frame], None]


class FrameSource:
    """Base class: a background thread producing frames until ``stop()``."""

    kind = "source"

    def __init__(self) -> None:
        self.on_frame: FrameCallback | None = None
        self.connected = False
        self.error: str | None = None
        self.frames = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._times: list[float] = []
        # whether the source's recorder is recording: None when it has none (or
        # it is not known yet)
        self.recording: bool | None = None

    def describe(self) -> str:
        return self.kind

    def start(self, on_frame: FrameCallback) -> None:
        self.on_frame = on_frame
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"frames-{self.kind}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    def _run(self) -> None:
        raise NotImplementedError

    def _emit(self, frame: Frame) -> None:
        self.frames += 1
        self._times.append(frame.t)
        if len(self._times) > 30:
            del self._times[0]
        if self.on_frame is not None:
            try:
                self.on_frame(frame)
            except Exception:
                log.exception("frame callback failed")

    def fps(self) -> float | None:
        ts = self._times
        if len(ts) < 2 or time.time() - ts[-1] > 2:
            return None
        return (len(ts) - 1) / (ts[-1] - ts[0])

    def status(self) -> dict:
        fps = self.fps()
        return {
            "kind": self.kind,
            "source": self.describe(),
            "connected": self.connected,
            "error": self.error,
            "frames": self.frames,
            "fps": round(fps, 1) if fps else None,
            "recording": self.recording,
        }


# --- octacam -------------------------------------------------------------------

# octacam preview frame header, version 2 (little-endian), see octacam/web/app.py:
# u8 version | u8 kind | u8 camera | u8 flags (bit0 = recording) | u32 frame number |
# u64 timestamp ns | f32 fps | u32 dropped | u16 crop x, y, w, h | u16 sensor w, h
OCTACAM_HEADER = struct.Struct("<BBBBIQfIHHHHHH")
OCTACAM_VERSION = 2
# octacam states (its "state"/"telemetry" messages, ~2 Hz) during a recording:
# armed and waiting for the first trigger, then recording
OCTACAM_RECORDING_STATES = ("waiting", "recording")


def octacam_ws_url(url: str) -> str:
    """Accept ``host:port``, ``http://host:port`` or a full ``ws://…/api/ws`` URL."""
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = "ws://" + url
    scheme, rest = url.split("://", 1)
    scheme = {"http": "ws", "https": "wss"}.get(scheme, scheme)
    if "/" not in rest:
        rest += "/api/ws"
    return f"{scheme}://{rest}"


class OctacamSource(FrameSource):
    """One camera's preview frames from a running octacam GUI.

    The preview runs at octacam's display rate (``gui.display_refresh_interval_ms``,
    ~30 Hz), at most 640 px on the long edge, JPEG-encoded. Frames only flow
    while the camera is grabbing (with an external trigger: while it is
    triggered). ``recording`` follows octacam's state messages, which come
    whether frames flow or not.
    """

    kind = "octacam"

    def __init__(self, url: str, camera: str, *, reconnect_s: float = 2.0):
        super().__init__()
        self.url = octacam_ws_url(url)
        self.camera = camera
        self.reconnect_s = reconnect_s
        self.camera_index: int | None = None
        self.sensor: tuple[int, int] | None = None
        self.state: str | None = None  # octacam's: idle, preview, waiting, recording, …

    def describe(self) -> str:
        return f"octacam {self.camera} @ {self.url}"

    def _run(self) -> None:
        from websockets.sync.client import connect

        while not self._stop.is_set():
            try:
                with connect(self.url, open_timeout=3, max_size=None, close_timeout=1) as ws:
                    self.connected = True
                    self.error = None
                    self._session(ws)
            except Exception as e:
                if not self._stop.is_set():
                    self.error = f"octacam: {e or type(e).__name__}"
            finally:
                self.connected = False
                self.camera_index = None
            self._stop.wait(self.reconnect_s)

    def _session(self, ws) -> None:
        while not self._stop.is_set():
            try:
                msg = ws.recv(timeout=0.5)
            except TimeoutError:
                continue
            if isinstance(msg, str):
                self._on_text(ws, msg)
            else:
                self._on_binary(msg)

    def _on_text(self, ws, msg: str) -> None:
        try:
            m = json.loads(msg)
        except ValueError:
            return
        if m.get("type") in ("state", "telemetry") and m.get("state"):
            self.state = m["state"]
            self.recording = self.state in OCTACAM_RECORDING_STATES
            return
        if m.get("type") != "system":
            return
        cams = m.get("cameras") or []
        names = [c.get("name") for c in cams]
        if not cams:
            self.error = "octacam has no camera open yet"
            return
        if self.camera not in names:
            self.error = f"octacam has no camera {self.camera!r} (cameras: {', '.join(names)})"
            self.camera_index = None
            return
        cam = cams[names.index(self.camera)]
        self.camera_index = int(cam["index"])
        self.error = None
        # only our camera: octacam then skips encoding the others for this client
        view = {str(c["index"]): {"want": c["index"] == self.camera_index} for c in cams}
        ws.send(json.dumps({"type": "view", "cameras": view}))

    def _on_binary(self, msg: bytes) -> None:
        if len(msg) < OCTACAM_HEADER.size or self.camera_index is None:
            return
        (version, kind, camera, flags, number, ts_ns, _fps, _dropped, *geom) = (
            OCTACAM_HEADER.unpack_from(msg)
        )
        if version != OCTACAM_VERSION:
            self.error = f"unsupported octacam preview format (version {version})"
            return
        if kind != 1 or camera != self.camera_index:
            return
        jpeg = bytes(msg[OCTACAM_HEADER.size :])
        image = np.asarray(Image.open(io.BytesIO(jpeg)).convert("L"))
        self.sensor = (geom[4], geom[5])
        if self.state is None:  # no state message yet
            self.recording = bool(flags & 1)
        self._emit(
            Frame(
                image=image,
                t=time.time(),
                seq=number,
                jpeg=jpeg,
                source_ts_ns=ts_ns,
                recording=bool(flags & 1),
            )
        )

    def status(self) -> dict:
        return {**super().status(), "camera": self.camera, "octacam_state": self.state}


# --- video file ---------------------------------------------------------------


class VideoSource(FrameSource):
    """A video file played in real time, looping, resampled like octacam's preview."""

    kind = "video"

    def __init__(self, path: str, *, fps: float = 30.0, max_dim: int = 640, loop: bool = True):
        super().__init__()
        self.path = str(path)
        self.out_fps = fps
        self.max_dim = max_dim
        self.loop = loop
        self.position_s = 0.0

    def describe(self) -> str:
        return f"video {self.path}"

    def _probe(self) -> tuple[int, int]:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "csv=p=0", self.path],
            capture_output=True, text=True, check=True,
        ).stdout  # fmt: skip
        w, h = (int(v) for v in out.strip().split(",")[:2])
        return w, h

    def _run(self) -> None:
        if not shutil.which("ffmpeg"):
            self.error = "ffmpeg not found (needed for --video)"
            return
        try:
            w, h = self._probe()
        except Exception as e:
            self.error = f"cannot read {self.path}: {e}"
            return
        factor = max(1, -(-max(w, h) // self.max_dim))  # same decimation as octacam
        ow, oh = len(range(0, w, factor)), len(range(0, h, factor))
        cmd = ["ffmpeg", "-v", "error", "-re"]
        if self.loop:
            cmd += ["-stream_loop", "-1"]
        cmd += ["-i", self.path, "-vf", f"fps={self.out_fps},scale={ow}:{oh}:flags=neighbor",
                "-f", "rawvideo", "-pix_fmt", "gray", "-"]  # fmt: skip
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.connected = True
        n = ow * oh
        seq = 0
        try:
            while not self._stop.is_set():
                buf = proc.stdout.read(n)
                if len(buf) < n:
                    break
                seq += 1
                self.position_s = seq / self.out_fps
                image = np.frombuffer(buf, np.uint8).reshape(oh, ow)
                self._emit(Frame(image=image, t=time.time(), seq=seq))
        finally:
            proc.kill()
            proc.wait()
            self.connected = False
