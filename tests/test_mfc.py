import math

import pytest
from fake import fake_mfc


def test_info_and_read():
    mfc = fake_mfc()
    assert mfc.info.full_scale == 2.0
    assert mfc.info.unit == "ln/min"
    assert mfc.info.gas == "Air *"
    assert mfc.info.powerup_alarm_active
    mfc.set_flow(1.25)
    r = mfc.read()
    assert r.flow == pytest.approx(1.25)
    assert r.setpoint == pytest.approx(1.25)
    assert r.valve == pytest.approx(62.5)
    assert r.temperature == pytest.approx(24.0)


@pytest.mark.parametrize("bad", [-0.1, 2.01, math.nan])
def test_set_flow_rejects_out_of_range(bad):
    mfc = fake_mfc()
    with pytest.raises(ValueError):
        mfc.set_flow(bad)
    assert mfc.inst.writes == []


def test_cap_is_limited_by_full_scale():
    mfc = fake_mfc(cap=5.0, full_scale=2.0)
    assert mfc.cap == 2.0
    assert fake_mfc(cap=1.5).cap == 1.5


def test_stop_retries():
    mfc = fake_mfc()
    mfc.set_flow(1.0)
    mfc.inst.fail_writes = 2
    mfc.stop(retries=3)
    assert mfc.inst.setpoint == 0.0
    mfc.inst.fail_writes = 3
    with pytest.raises(OSError):
        mfc.stop(retries=3)


def test_port_lock_is_exclusive_across_opens():
    import os

    import serial

    from airflow_control.mfc import PortInUseError, _lock_port

    master, slave = os.openpty()
    path = os.ttyname(slave)
    a, b = serial.Serial(path), serial.Serial(path)
    try:
        _lock_port(a, path)
        _lock_port(a, path)  # same open file: fine
        with pytest.raises(PortInUseError):
            _lock_port(b, path)
    finally:
        a.close(), b.close(), os.close(master), os.close(slave)
