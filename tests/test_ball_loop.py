import dataclasses
import time

import pytest
from fake import fake_mfc

from airflow_control.ball_delivery.loop import ClosedLoop, LoopParams, Tracking
from airflow_control.ball_delivery.track import Detection
from airflow_control.controller import Channel, EventLog
from airflow_control.frames import FrameSource


def det(distance, edge=False):
    """A detection at *distance* ball diameters from contact; None = ball not seen.
    *edge*: the ball is at the image edge it comes from."""
    seen = distance is not None
    return Detection(
        t=time.time(), seq=0, ball_x=0.0, ball_y=0.0, ball_score=0.9 if seen else 0.2,
        ball_template=0, visible=seen, platform_x=0.0, platform_y=0.0,
        platform_score=0.9, contact_x=0.0, contact_y=0.0, distance=distance,
        at_entry_edge=edge if seen else None,
    )  # fmt: skip


@pytest.fixture
def loop():
    ch = Channel(fake_mfc("air", 1), EventLog(None))
    params = LoopParams(start_flow=0.6, step_flow=0.2, step_interval_s=2, max_flow=1.4,
                        max_at_max_s=5, contact_hold_s=0.2, release_hold_s=1)  # fmt: skip
    return ClosedLoop(ch, ch.events, params)


def setpoint(loop):
    return loop.channel.mfc.inst.setpoint


def seen(loop, distance, t, frames=3):
    """The ball at *distance* for a few frames (40 ms apart) ending at *t*."""
    for i in range(frames):
        loop.update(det(distance), t - 0.04 * (frames - 1 - i))


def test_ramp_until_contact_then_air_off(loop):
    loop.arm(det(1.0))
    t = time.monotonic()
    assert loop.state == "ramp" and setpoint(loop) == 0.6
    loop.update(det(1.0), t + 1.0)
    assert setpoint(loop) == 0.6
    loop.update(det(0.8), t + 2.1)
    assert setpoint(loop) == pytest.approx(0.8)
    loop.update(det(0.5), t + 4.2)
    assert setpoint(loop) == pytest.approx(1.0)
    loop.update(det(0.05), t + 4.5)  # contact, not held long enough yet
    assert loop.state == "ramp"
    loop.update(det(0.05), t + 4.8)
    assert loop.state == "contact" and setpoint(loop) == 0.0
    assert loop.cycles == 1 and loop.last_contact_flow == pytest.approx(1.0)


def test_brief_contact_does_not_count(loop):
    loop.arm(det(1.0))
    t = time.monotonic()
    loop.update(det(0.05), t + 0.1)
    loop.update(det(0.5), t + 0.25)  # bounced off
    loop.update(det(0.05), t + 0.35)
    assert loop.state == "ramp"


def test_pushed_away_ramps_again_from_start(loop):
    loop.arm(det(0.0))  # armed with the ball already at the platform
    t = time.monotonic()
    assert loop.state == "contact" and setpoint(loop) == 0.0
    loop.update(det(0.2), t + 0.5)  # between contact and release: the fly is pushing
    loop.update(det(0.5), t + 1.0)
    loop.update(det(0.9, edge=True), t + 1.5)
    assert loop.state == "contact"
    loop.update(det(None), t + 2.1)  # gone down the track: out of sight counts as away
    assert loop.state == "ramp" and setpoint(loop) == 0.6


def test_release_needs_sustained_distance(loop):
    loop.arm(det(0.0))
    t = time.monotonic()
    loop.update(det(0.6), t + 0.1)
    loop.update(det(0.1), t + 0.6)  # back at the platform: timer resets
    loop.update(det(0.6), t + 0.7)
    loop.update(det(0.6), t + 1.5)
    assert loop.state == "contact"


def test_watchdog_pauses_and_resumes(loop):
    loop.arm(det(1.0))
    t = time.monotonic()
    loop.update(det(1.0), t + 0.5)
    loop.tick(t + 1.0)
    assert loop.state == "ramp"
    loop.tick(t + 1.6)  # 1.1 s without a frame
    assert loop.state == "paused" and setpoint(loop) == 0.0 and loop.armed
    loop.update(det(1.0), t + 3)
    assert loop.state == "ramp" and setpoint(loop) == 0.6


def test_disarm_turns_air_off(loop):
    loop.arm(det(1.0))
    loop.disarm("user")
    assert loop.state == "off" and setpoint(loop) == 0.0
    loop.update(det(0.0))  # ignored while off
    assert loop.state == "off"


def test_max_flow_limited_by_cap():
    ch = Channel(fake_mfc("air", 1, cap=1.0), EventLog(None))
    loop = ClosedLoop(ch, ch.events, LoopParams(max_flow=2.0))
    assert loop.max_flow == 1.0


def test_params_validation():
    p = LoopParams()
    with pytest.raises(ValueError, match="unknown"):
        p.update(nope=1)
    with pytest.raises(ValueError, match="release_dist"):
        p.update(release_dist=0.01)
    with pytest.raises(ValueError, match=">= 0"):
        LoopParams().update(start_flow=-1)


def test_brief_occlusion_is_not_lost(loop):
    loop.arm(det(0.0))
    t = time.monotonic()
    loop.update(det(None), t + 0.5)  # the fly covers the ball for a moment
    loop.update(det(None), t + 2.0)
    loop.update(det(0.05), t + 2.2)
    loop.update(det(0.05), t + 5.0)
    assert loop.state == "contact"


def test_ball_rolling_back_out_of_view_is_not_lost(loop):
    loop.arm(det(0.8))
    t = time.monotonic()
    loop.update(det(1.0, edge=True), t + 0.5)  # too little air: rolls back
    loop.update(det(None), t + 1.0)
    loop.update(det(None), t + 6.0)
    assert loop.state == "ramp" and setpoint(loop) > 0.6  # keeps ramping


def test_status_reports_what_the_loop_waits_for(loop):
    loop.arm(det(1.0))
    s = loop.status()
    assert s["contact_s"] is None and s["away_s"] is None and s["last_seen_edge"] is None
    loop.update(det(0.05))  # at contact, not held yet
    assert loop.status()["contact_s"] is not None
    seen(loop, 0.5, time.monotonic())  # seen off the edge, then gone
    loop.update(det(None))
    s = loop.status()
    assert s["contact_s"] is None and s["last_seen_edge"] is False
    assert s["missing_s"] is not None


def test_ball_followed_out_through_the_entry_edge_is_not_lost(loop):
    loop.arm(det(1.0))
    t = time.monotonic()
    loop.update(det(0.05), t + 0.3)
    loop.update(det(0.05), t + 0.6)
    assert loop.state == "contact"
    loop.update(det(0.4), t + 1.0)  # flicked away, last confident sighting mid-track
    # then only matched weakly, its trail reaching the entry edge
    loop.update(dataclasses.replace(det(None), at_entry_edge=True), t + 1.04)
    for dt in (1.5, 2.5, 4.0):
        loop.update(det(None), t + dt)
    assert loop.state == "ramp"


def test_a_lone_sighting_does_not_place_the_ball(loop):
    loop.arm(det(1.0, edge=True))
    t = time.monotonic()
    loop.update(det(None), t + 1.0)  # waiting out of view
    loop.update(det(0.1), t + 5.0)  # one stray match (or a ball bouncing straight back)
    for dt in (5.1, 6.0, 8.0, 10.0):
        loop.update(det(None), t + dt)
    assert loop.state == "ramp" and loop.status()["missing_s"] is None


def test_rest_after_too_long_at_max_then_retry(loop):
    loop.params.update(rest_s=5)
    loop.arm(det(1.0))
    t = time.monotonic()
    for k in range(1, 5):
        loop.update(det(1.0), t + 2.01 * k)
    assert setpoint(loop) == pytest.approx(1.4)  # 0.6 -> 1.4 in four steps, capped
    loop.update(det(1.0), t + 8.1)
    loop.update(det(1.0), t + 13.2)
    assert loop.state == "rest" and loop.armed and setpoint(loop) == 0.0
    assert "no contact" in loop.reason
    loop.tick(t + 15)
    assert loop.state == "rest"
    loop.tick(t + 18.3)  # no frames needed: the air is off
    assert loop.state == "ramp" and setpoint(loop) == 0.6


def test_contact_during_rest(loop):
    loop.params.update(max_at_max_s=0, rest_s=30)
    loop.arm(det(1.0))
    t = time.monotonic()
    for k in range(1, 5):
        loop.update(det(1.0), t + 2.01 * k)
    loop.update(det(1.0), t + 8.1)
    assert loop.state == "rest"
    loop.update(det(0.05), t + 9.0)  # the ball got there after all
    loop.update(det(0.05), t + 9.3)
    assert loop.state == "contact" and setpoint(loop) == 0.0


def test_ball_vanishing_at_the_platform_holds_then_carries_on(loop):
    loop.arm(det(0.0))
    t = time.monotonic()
    seen(loop, 0.4, t + 0.5)  # pushed, still on the track
    loop.update(det(None), t + 0.6)  # vanished away from the entry edge: covered? fell?
    loop.update(det(None), t + 1.8)
    assert loop.state == "contact" and loop.status()["missing_s"] is not None  # holds
    loop.update(det(None), t + 2.7)  # held long enough: as if it rolled back
    assert loop.vanished == 1 and loop.status()["missing_s"] is None
    loop.update(det(None), t + 3.8)
    assert loop.state == "ramp" and loop.armed and setpoint(loop) == 0.6


def test_ball_vanishing_mid_ramp_holds_the_ramp_briefly(loop):
    loop.arm(det(1.0, edge=True))
    t = time.monotonic()
    seen(loop, 0.6, t + 1.0)  # on its way
    loop.update(det(None), t + 1.5)  # vanished
    loop.update(det(None), t + 3.0)
    assert loop.state == "ramp" and setpoint(loop) == 0.6  # no step while missing
    loop.update(det(None), t + 3.6)  # stop waiting
    loop.update(det(None), t + 3.7)
    assert loop.state == "ramp" and setpoint(loop) == pytest.approx(0.8)  # steps again


class IdleSource(FrameSource):
    kind = "test"

    def _run(self) -> None:
        self._stop.wait()


def test_recording_end_disarms_a_loop_armed_during_it(loop):
    src = IdleSource()
    tr = Tracking(src, loop.channel, loop.events, state_dir=None)
    tr.loop.arm(det(1.0))
    for rec in (None, False):  # no recorder (video), or outside a recording (brush test)
        src.recording = rec
        tr._follow_recording()
        assert tr.loop.state == "ramp"
    src.recording = True
    tr._follow_recording()
    assert tr.loop.state == "ramp"
    src.recording = False  # the recording ended
    tr._follow_recording()
    assert tr.loop.state == "off" and tr.loop.reason == "recording ended"
    assert setpoint(loop) == 0
    tr.loop.arm(det(1.0))  # armed again, between recordings: runs until disarmed
    tr._follow_recording()
    assert tr.loop.state == "ramp"


def test_ball_covered_at_the_fly_holds_the_contact(loop):
    # 2026-10-22: 11 of 48 releases were a ball last seen at contact, then
    # out of sight (the fly covering it): the air came on 5.2 s later while the
    # fly still had it. Covered at the fly, it holds for occluded_hold_s.
    loop.params.update(occluded_hold_s=20)
    loop.arm(det(0.0))
    t = time.monotonic()
    seen(loop, 0.05, t + 0.5)  # at the fly
    for k in range(1, 40):  # out of sight for 19.5 s
        loop.update(det(None), t + 0.5 + 0.5 * k)
    assert loop.state == "contact" and setpoint(loop) == 0.0 and loop.vanished == 0
    assert loop.status()["missing_limit_s"] == 20
    # A ball that really fell off is still delivered again.
    loop.update(det(None), t + 21.0)
    assert loop.vanished == 1
    loop.update(det(None), t + 22.1)
    assert loop.state == "ramp" and setpoint(loop) == 0.6


def test_a_ball_covered_mid_ramp_still_holds_only_briefly(loop):
    # Air on: the long hold would blow at a ball it cannot see for 20 s.
    loop.params.update(occluded_hold_s=20)
    loop.arm(det(1.0, edge=True))
    t = time.monotonic()
    seen(loop, 0.1, t + 1.0)  # arrived, contact not held long enough yet
    loop.update(det(None), t + 1.1)
    assert loop.status()["missing_limit_s"] == loop.params.missing_hold_s
    loop.update(det(None), t + 3.2)
    assert loop.vanished == 1


@pytest.mark.parametrize("distance", [0.25, 0.9])
def test_arming_near_the_fly_is_contact(distance):
    # 2026-10-22: 8 of 29 takes started with a ball 0.24-0.44 diameters off the
    # contact point: a ramp, contact again within 0.8 s, a short air burst.
    ch = Channel(fake_mfc("air", 1), EventLog(None))
    loop = ClosedLoop(ch, ch.events, LoopParams(release_dist=1.0))
    loop.arm(det(distance))
    assert loop.state == "contact" and setpoint(loop) == 0.0
    loop2 = ClosedLoop(ch, ch.events, LoopParams(release_dist=1.0))
    loop2.arm(det(1.5))  # out of reach: deliver it
    assert loop2.state == "ramp"


def test_resuming_near_the_fly_is_contact(loop):
    loop.arm(det(1.0))
    t = time.monotonic()
    loop.update(det(1.0), t + 0.5)
    loop.tick(t + 1.6)
    assert loop.state == "paused"
    loop.update(det(0.25), t + 3)  # within release_dist (0.3)
    assert loop.state == "contact" and setpoint(loop) == 0.0
