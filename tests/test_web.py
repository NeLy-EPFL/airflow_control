import pytest
from fake import fake_mfc
from fastapi.testclient import TestClient

from airflow_control.controller import Channel, EventLog, Rig
from airflow_control.web.app import create_app


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
