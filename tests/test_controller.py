import json
import time

import pytest
from fake import fake_mfc

from airflow_control.controller import Channel, EventLog, Rig


@pytest.fixture
def chan(tmp_path):
    return Channel(fake_mfc(), EventLog(tmp_path), label="Air", max_puff_s=5)


def wait_for(cond, timeout=2.0):
    t0 = time.monotonic()
    while not cond():
        assert time.monotonic() - t0 < timeout, "timed out"
        time.sleep(0.005)


def test_continuous_and_stop(chan):
    chan.set_continuous(1.2)
    assert chan.mfc.inst.setpoint == 1.2
    chan.stop()
    assert chan.mfc.inst.setpoint == 0.0
    assert chan.continuous == 0.0


def test_puff_returns_to_zero(chan):
    chan.puff(1.5, 0.05)
    assert chan.mfc.inst.setpoint == 1.5
    assert chan.status()["puff"]["setpoint"] == 1.5
    wait_for(lambda: chan.status()["puff"] is None)
    assert chan.mfc.inst.writes == [1.5, 0.0]


def test_puff_returns_to_continuous(chan):
    chan.set_continuous(0.8)
    chan.puff(1.6, 0.05)
    wait_for(lambda: chan.status()["puff"] is None)
    assert chan.mfc.inst.writes == [0.8, 1.6, 0.8]


def test_new_puff_restarts_timer(chan):
    chan.puff(1.0, 0.05)
    chan.puff(1.4, 0.3)
    time.sleep(0.15)  # the first puff's timer has expired but must not end the second
    assert chan.mfc.inst.setpoint == 1.4
    wait_for(lambda: chan.status()["puff"] is None)
    assert chan.mfc.inst.writes == [1.0, 1.4, 0.0]


def test_stop_cancels_puff(chan):
    chan.puff(1.5, 0.1)
    chan.stop()
    time.sleep(0.2)
    assert chan.mfc.inst.writes == [1.5, 0.0]


def test_continuous_cancels_puff(chan):
    chan.puff(1.5, 0.1)
    chan.set_continuous(0.9)
    time.sleep(0.2)
    assert chan.mfc.inst.writes == [1.5, 0.9]


def test_puff_validation(chan):
    with pytest.raises(ValueError):
        chan.puff(1.0, 0)
    with pytest.raises(ValueError):
        chan.puff(1.0, 6)  # > max_puff_s
    with pytest.raises(ValueError):
        chan.puff(2.5, 0.5)  # > cap
    assert chan.mfc.inst.writes == []


def test_failed_puff_end_falls_back_to_stop(chan):
    chan.puff(1.5, 0.05)
    chan.mfc.inst.fail_writes = 1  # the puff_end write fails, the stop retry succeeds
    wait_for(lambda: chan.status()["puff"] is None)
    wait_for(lambda: chan.mfc.inst.setpoint == 0.0)


def test_event_log_jsonl(chan):
    chan.set_continuous(1.0)
    chan.stop()
    lines = [json.loads(x) for x in chan.events.path.read_text().splitlines()]
    assert [(r["event"], r["setpoint"]) for r in lines] == [("continuous", 1.0), ("stop", 0.0)]
    assert all({"t", "mono", "channel"} <= r.keys() for r in lines)


def test_rig_close_stops_everything(tmp_path):
    events = EventLog(tmp_path)
    chans = [Channel(fake_mfc("a", 1), events), Channel(fake_mfc("b", 2), events)]
    rig = Rig(chans, events, poll_hz=50)
    rig.start_polling()
    chans[0].set_continuous(1.0)
    chans[1].puff(1.0, 5)
    wait_for(lambda: chans[0].reading is not None and chans[0].reading.flow == 1.0)
    rig.close()
    assert [c.mfc.inst.setpoint for c in chans] == [0.0, 0.0]
