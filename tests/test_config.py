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
