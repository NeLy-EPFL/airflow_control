import json
import time

import pytest
from fake import fake_mfc
from fastapi.testclient import TestClient

from airflow_control.ball_delivery import BallDelivery
from airflow_control.ball_delivery.loop import Tracking
from airflow_control.ball_delivery.sim import SimBallSource, calibrate_sim
from airflow_control.controller import Channel, EventLog, Rig
from airflow_control.frames import Frame, FrameSource
from airflow_control.web.app import create_app

API = "/api/plugins/ball_delivery"


@pytest.fixture
def client_rig():
    events = EventLog(None)
    rig = Rig([Channel(fake_mfc("air", 1), events), Channel(fake_mfc("co2", 2), events)], events)
    with TestClient(create_app(rig, poll=False)) as client:
        yield client, rig
    # leaving the context runs the lifespan shutdown


def test_continuous_puff_stop(client_rig):
    client, rig = client_rig
    air = rig.channel("air").mfc.inst
    assert client.post("/api/air/continuous", json={"setpoint": 1.1}).status_code == 200
    assert air.setpoint == 1.1
    r = client.post("/api/air/puff", json={"setpoint": 1.8, "duration_s": 2})
    assert r.json()["channels"][0]["puff"]["setpoint"] == 1.8
    assert client.post("/api/stop").status_code == 200
    assert air.setpoint == 0.0


def test_errors(client_rig):
    client, _ = client_rig
    assert client.post("/api/air/continuous", json={"setpoint": 3}).status_code == 400
    assert client.post("/api/air/continuous", json={"setpoint": -1}).status_code == 422
    assert client.post("/api/nope/stop").status_code == 404


def test_shutdown_stops(client_rig):
    client, rig = client_rig
    client.post("/api/co2/continuous", json={"setpoint": 0.5})
    client.__exit__(None, None, None)
    assert rig.channel("co2").mfc.inst.setpoint == 0.0


def test_index_and_ws(client_rig):
    client, _ = client_rig
    assert "airflow_control" in client.get("/").text
    with client.websocket_connect("/ws") as ws:
        assert [c["name"] for c in ws.receive_json()["channels"]] == ["air", "co2"]


def test_history(client_rig):
    client, rig = client_rig
    rig.channel("air").poll()
    h = client.get("/api/history").json()
    assert len(h["air"]) == 1 and h["co2"] == []


def test_shutdown():
    events = EventLog(None)
    rig = Rig([Channel(fake_mfc("air", 1), events)], events)
    called = []
    with TestClient(create_app(rig, poll=False, request_exit=lambda: called.append(1))) as client:
        client.post("/api/air/continuous", json={"setpoint": 1.0})
        assert client.post("/api/shutdown").status_code == 200
        assert called == [1]
        assert rig.channel("air").mfc.inst.setpoint == 0.0


def test_shutdown_unavailable(client_rig):
    client, _ = client_rig
    assert client.post("/api/shutdown").status_code == 501


# --- closed loop ---------------------------------------------------------------


class PushSource(FrameSource):
    kind = "test"

    def _run(self):
        pass


@pytest.fixture
def loop_client(tmp_path):
    events = EventLog(None)
    air = Channel(fake_mfc("air", 1), events)
    rig = Rig([air, Channel(fake_mfc("co2", 2), events)], events)
    tracking = Tracking(PushSource(), air, events, state_dir=tmp_path / "state")
    with TestClient(create_app(rig, plugins=[BallDelivery(tracking)], poll=False)) as client:
        yield client, rig, tracking


def push(tracking, x, seq=1):
    sim = SimBallSource(lambda: 0.0)
    sim.x = x
    tracking._on_frame(Frame(image=sim.render(), t=time.time(), seq=seq))


def test_loop_calibrate_arm_and_manual_override(loop_client, tmp_path):
    client, rig, tracking = loop_client
    assert client.get(f"{API}/frame").status_code == 404
    assert client.post(f"{API}/arm").status_code == 400  # nothing calibrated, no frame

    cx = SimBallSource.contact_x()
    push(tracking, cx)
    r = client.get(f"{API}/frame")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert json.loads(r.headers["X-Detection"]) is None  # no template yet

    box = list(SimBallSource.ball_box(cx))
    assert client.post(f"{API}/box", json={"kind": "ball", "box": box}).status_code == 200
    assert client.post(f"{API}/box", json={"kind": "platform", "box": list(SimBallSource.platform_box())}).status_code == 200  # fmt: skip
    r = client.post(f"{API}/arm")
    assert r.status_code == 400 and "contact" in r.json()["detail"]
    st = client.post(f"{API}/contact").json()["plugins"]["ball_delivery"]
    assert st["calibration"]["missing"] == [] and st["detection"]["distance"] < 0.05
    assert (tmp_path / "state" / "calibration.npz").exists()

    push(tracking, cx - 150, seq=2)
    st = client.post(f"{API}/arm").json()["plugins"]["ball_delivery"]["loop"]
    assert st["state"] == "ramp" and rig.channel("air").mfc.inst.setpoint == 0.6
    # changing the calibration while armed is refused
    assert client.post(f"{API}/clear").status_code == 400
    # a manual setpoint on the loop's MFC takes it over
    client.post("/api/air/continuous", json={"setpoint": 0.3})
    assert tracking.loop.state == "off" and rig.channel("air").mfc.inst.setpoint == 0.3


def test_loop_adjust_for_a_new_animal(loop_client):
    """Templates kept; platform and contact moved without the ball near the animal."""
    client, rig, tracking = loop_client
    cx = SimBallSource.contact_x()
    push(tracking, cx)
    client.post(f"{API}/box", json={"kind": "ball", "box": list(SimBallSource.ball_box(cx))})
    client.post(f"{API}/box", json={"kind": "platform", "box": list(SimBallSource.platform_box())})  # fmt: skip
    push(tracking, -500, seq=2)  # no ball in view
    st = client.post(f"{API}/contact").json()["plugins"][
        "ball_delivery"
    ]  # first guess next to the platform
    assert st["calibration"]["missing"] == []
    guess = st["detection"]["contact_x"]
    assert guess == pytest.approx(cx, abs=10)
    # once set, the button needs the ball; the circle is dragged instead
    assert client.post(f"{API}/contact").status_code == 400
    st = client.post(f"{API}/contact", json={"x": guess - 20, "y": 200}).json()["plugins"][
        "ball_delivery"
    ]
    assert st["detection"]["contact_x"] == pytest.approx(guess - 20)
    px = st["detection"]["platform_x"]
    st = client.post(f"{API}/platform", json={"x": px + 8, "y": 250}).json()["plugins"][
        "ball_delivery"
    ]
    assert st["detection"]["platform_x"] == pytest.approx(px, abs=4)  # snapped back onto it
    assert client.post(f"{API}/platform", json={"x": 2000, "y": 0}).status_code == 400
    # positions can be adjusted while armed; templates can't
    push(tracking, cx - 150, seq=3)
    assert client.post(f"{API}/arm").status_code == 200
    assert client.post(f"{API}/contact", json={"x": guess - 10, "y": 200}).status_code == 200
    assert client.post(f"{API}/platform", json={"x": px, "y": 250}).status_code == 200
    assert client.post(f"{API}/box", json={"kind": "ball", "box": list(SimBallSource.ball_box(cx))}).status_code == 400  # fmt: skip
    assert tracking.loop.armed
    client.post(f"{API}/disarm")
    # re-marking the ball keeps the contact
    push(tracking, cx, seq=4)
    st = client.post(f"{API}/box", json={"kind": "ball", "box": list(SimBallSource.ball_box(cx))}).json()["plugins"]["ball_delivery"]  # fmt: skip
    assert st["calibration"]["missing"] == []


def test_loop_stop_all_disarms(loop_client):
    client, rig, tracking = loop_client
    tracking.tracker.clear()
    calibrate_sim(tracking.tracker)
    push(tracking, 100)
    assert client.post(f"{API}/arm").status_code == 200
    client.post("/api/co2/continuous", json={"setpoint": 0.5})  # other MFC: loop keeps going
    assert tracking.loop.armed
    client.post("/api/stop")
    assert not tracking.loop.armed and rig.channel("air").mfc.inst.setpoint == 0.0


def test_loop_params(loop_client, tmp_path):
    client, _, tracking = loop_client
    r = client.post(f"{API}/params", json={"start_flow": 0.8, "threshold": 0.7})
    assert r.status_code == 200 and tracking.tracker.threshold == 0.7
    assert json.loads((tmp_path / "state" / "loop_params.json").read_text())["start_flow"] == 0.8
    assert client.post(f"{API}/params", json={"nope": 1}).status_code == 400
    assert client.post(f"{API}/params", json={"start_flow": 5}).status_code == 400
    assert tracking.loop.params.start_flow == 0.8


def test_no_plugin_by_default(client_rig):
    """Plain MFC control: no ball delivery panel, routes or status."""
    client, _ = client_rig
    assert client.get("/api/status").json()["plugins"] == {}
    assert client.get(f"{API}/frame").status_code == 404
    assert client.get("/plugins/ball_delivery/panel.js").status_code == 404
    assert "ball" not in client.get("/").text.lower()


def test_plugin_panel_is_served(loop_client):
    client, _, _ = loop_client
    assert "Ball delivery" in client.get("/plugins/ball_delivery/panel.html").text
    assert "airflowPlugin" in client.get("/plugins/ball_delivery/panel.js").text
    assert client.get("/plugins/ball_delivery/panel.css").status_code == 200


def test_saved_params_from_an_older_version_load(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "loop_params.json").write_text(json.dumps({"start_flow": 0.7, "lost_after_s": 2.0}))
    events = EventLog(None)
    tracking = Tracking(PushSource(), Channel(fake_mfc("air", 1), events), events, state_dir=state)
    assert tracking.loop.params.start_flow == 0.7 and tracking.error is None
