import io
import json
import threading
import time

import numpy as np
from PIL import Image
from websockets.sync.server import serve

from airflow_control.ball_delivery.sim import SimBallSource
from airflow_control.frames import OCTACAM_HEADER, OctacamSource, VideoSource, octacam_ws_url


def test_octacam_url_forms():
    assert octacam_ws_url("127.0.0.1:8765") == "ws://127.0.0.1:8765/api/ws"
    assert octacam_ws_url("http://rig:8765/") == "ws://rig:8765/api/ws"
    assert octacam_ws_url("ws://rig:9000/api/ws") == "ws://rig:9000/api/ws"


def jpeg(img):
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def preview(camera, number, img, recording=False):
    h, w = img.shape
    header = OCTACAM_HEADER.pack(2, 1, camera, int(recording), number, 123, 100.0, 0,
                                 0, 0, 2 * w, 2 * h, 2 * w, 2 * h)  # fmt: skip
    return header + jpeg(img)


def test_octacam_source_against_a_fake_octacam():
    """Picks its camera by name, asks for it alone, decodes only its frames."""
    img = np.tile(np.arange(64, dtype=np.uint8) * 4, (48, 1))
    views = []

    def handler(ws):
        cams = [{"index": 0, "name": "camera_F"}, {"index": 1, "name": "camera_LM"}]
        ws.send(json.dumps({"type": "state", "state": "preview"}))
        ws.send(json.dumps({"type": "system", "cameras": cams}))
        views.append(json.loads(ws.recv(timeout=2)))
        for n in range(1, 4):
            ws.send(preview(0, n, np.zeros_like(img)))
            ws.send(preview(1, n, img, recording=n == 3))
        time.sleep(1)

    with serve(handler, "127.0.0.1", 0) as server:
        port = server.socket.getsockname()[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        got = []
        src = OctacamSource(f"127.0.0.1:{port}", "camera_LM")
        src.start(got.append)
        deadline = time.time() + 3
        while len(got) < 3 and time.time() < deadline:
            time.sleep(0.02)
        src.stop()
        server.shutdown()

    assert views == [{"type": "view", "cameras": {"0": {"want": False}, "1": {"want": True}}}]
    assert [f.seq for f in got] == [1, 2, 3]
    assert got[0].image.shape == (48, 64) and abs(int(got[0].image[0, 40]) - 160) < 8
    assert got[2].recording and not got[0].recording
    assert src.recording is False  # octacam's state message says preview
    assert src.sensor == (128, 96) and src.error is None


def test_octacam_recording_follows_its_state_messages():
    src = OctacamSource("127.0.0.1:1", "camera_LM")
    assert src.recording is None
    for state, rec in [("preview", False), ("waiting", True), ("recording", True),
                       ("finishing", False), ("idle", False)]:  # fmt: skip
        src._on_text(None, json.dumps({"type": "telemetry", "state": state, "ready": True}))
        assert src.recording is rec and src.status()["recording"] is rec


def test_octacam_unknown_camera_is_reported():
    src = OctacamSource("127.0.0.1:1", "camera_X")
    src._on_text(None, json.dumps({"type": "system", "cameras": [{"index": 0, "name": "cam"}]}))
    assert "camera_X" in src.error and src.camera_index is None


def test_octacam_unreachable_sets_error():
    src = OctacamSource("127.0.0.1:1", "camera_LM", reconnect_s=0.1)
    src.start(lambda f: None)
    time.sleep(0.5)
    src.stop()
    assert src.error and src.error.startswith("octacam") and not src.connected


def test_sim_ball_moves_with_flow_and_stops_at_platform():
    flow = [0.0]
    sim = SimBallSource(lambda: flow[0])
    sim.needed = 1.0
    x0 = sim.x
    for _ in range(30):
        sim.step(1 / 30)
    assert sim.x == x0  # no air: stays put
    flow[0] = 1.5
    for _ in range(300):
        sim.step(1 / 30)
    assert sim.x == SimBallSource.contact_x()
    assert sim.render().shape == (SimBallSource.H, SimBallSource.W)


def test_video_source_without_file_reports_error(tmp_path):
    src = VideoSource(str(tmp_path / "missing.mp4"))
    src.start(lambda f: None)
    time.sleep(0.5)
    src.stop()
    assert src.error
