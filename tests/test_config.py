import threading
from types import SimpleNamespace

import pytest
from fake import fake_mfc
from typer.testing import CliRunner

from airflow_control import cli, config, mfc
from airflow_control.sim import SimInstrument


def test_cap_defaults_to_full_scale_and_unit_in_errors():
    m = fake_mfc(full_scale=100.0, unit="ml/min")
    assert m.cap == 100.0
    with pytest.raises(ValueError, match="ml/min"):
        m.set_flow(150)


def test_no_config_means_no_mfcs(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("AIRFLOW_CONTROL_CONFIG", raising=False)
    cfg = config.load()
    assert cfg.mfcs == [] and cfg.source is None
    with pytest.raises(LookupError, match="airflow-control init"):
        cfg.mfc(None)


def test_render_and_load_roundtrip(tmp_path):
    infos = [fake_mfc(address=a, serial_number=100 + a).info for a in (3, 7)]
    path = tmp_path / "c.toml"
    path.write_text(config.render_config("COM4", 19200, infos))
    cfg = config.load(path)
    assert cfg.port == "COM4" and cfg.baudrate == 19200
    assert [(m.name, m.address, m.cap) for m in cfg.mfcs] == [("mfc3", 3, 2.0), ("mfc7", 7, 2.0)]
    assert cfg.mfc("mfc7").label == "MFC 7"


def test_example_config_loads():
    cfg = config.load("configs/example.toml")
    assert [m.name for m in cfg.mfcs] == ["air", "co2"]


def test_find_adapter(monkeypatch):
    ports = [
        SimpleNamespace(
            device="/dev/ttyACM0",
            manufacturer="Arduino Srl",
            product="Mega",
            description="Mega",
            vid=1,
        ),
        SimpleNamespace(
            device="COM4",
            manufacturer="Voegtlin Instruments",
            product="PDM-U Modul",
            description="PDM-U Modul",
            vid=0x403,
        ),
    ]
    monkeypatch.setattr(mfc.list_ports, "comports", lambda: ports)
    assert mfc.find_adapter() == "COM4"
    monkeypatch.setattr(mfc.list_ports, "comports", lambda: ports[:1])
    with pytest.raises(FileNotFoundError, match="/dev/ttyACM0"):
        mfc.find_adapter()


class FakeBus:
    port, baudrate, lock = "/dev/fake", 9600, threading.RLock()

    def scan(self, addresses=None, timeout_s=None):
        return [1, 2]

    def instrument(self, address):
        return SimInstrument(serial_number=1000 + address)


def test_init_writes_config(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_bus", lambda cfg, port, baud: FakeBus())
    out = tmp_path / "config.toml"
    r = CliRunner().invoke(cli.app, ["init", "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert "SN 1002" in r.output
    assert [m.address for m in config.load(out).mfcs] == [1, 2]
    r = CliRunner().invoke(cli.app, ["init", "-o", str(out)])
    assert r.exit_code == 1 and "--force" in r.output


def test_ball_delivery_is_opt_in(tmp_path):
    from airflow_control import ball_delivery, config
    from airflow_control.controller import build_rig

    plain = tmp_path / "plain.toml"
    plain.write_text('[[mfc]]\nname = "air"\naddress = 1\n')
    cfg = config.load(plain)
    rig = build_rig(cfg, sim=True)
    assert ball_delivery.build(cfg, rig, sim=True) is None  # --sim alone: MFCs only
    bd = ball_delivery.build(cfg, rig, enabled=True, sim=True)  # --sim --ball
    assert bd.tracking.source.kind == "sim"
    with pytest.raises(ValueError, match="needs a camera"):
        ball_delivery.build(cfg, rig, enabled=True)  # --ball without a camera

    on = tmp_path / "on.toml"
    on.write_text(plain.read_text() + '[plugins.ball_delivery]\ncamera = "camera_LM"\n')
    cfg = config.load(on)
    bd = ball_delivery.build(cfg, rig)
    assert bd.tracking.source.kind == "octacam" and bd.tracking.loop.channel.name == "air"
    assert ball_delivery.build(cfg, rig, enabled=False) is None  # --no-ball
    rig.close()

    old = tmp_path / "old.toml"
    old.write_text(plain.read_text() + '[tracking]\ncamera = "camera_LM"\n')
    with pytest.raises(ValueError, match="plugins.ball_delivery"):
        config.load(old)
