import numpy as np
import pytest

from airflow_control.ball_delivery.sim import SimBallSource, calibrate_sim
from airflow_control.ball_delivery.track import Calibration, Tracker


def frame_with_ball(x):
    sim = SimBallSource(lambda: 0.0)
    sim.x = x
    return sim.render()


@pytest.fixture
def tracker():
    tr = Tracker()
    calibrate_sim(tr)
    return tr


def test_contact_rest_and_absent(tracker):
    at = tracker.process(frame_with_ball(SimBallSource.contact_x()), t=0)
    assert at.visible and at.distance < 0.05
    # mostly out of view on the left: found by the second template
    rest = tracker.process(frame_with_ball(SimBallSource.REST_X), t=0)
    assert rest.visible and rest.ball_template == 1 and rest.distance > 2
    assert rest.at_entry_edge and not at.at_entry_edge
    gone = tracker.process(frame_with_ball(-500), t=0)
    assert not gone.visible and gone.distance is None and gone.ball_score < 0.5


def test_distance_grows_with_gap(tracker):
    cx = SimBallSource.contact_x()
    d = [tracker.process(frame_with_ball(cx - gap), t=0).distance for gap in (0, 20, 60, 120)]
    assert d == sorted(d)
    assert d[2] == pytest.approx(60 / tracker.cal.ball_diameter, abs=0.06)


def test_platform_is_stable_with_the_ball_against_it(tracker):
    xs = {
        tracker.process(frame_with_ball(x), t=0).platform_x
        for x in (SimBallSource.contact_x(), 100, -500)
    }
    assert max(xs) - min(xs) <= tracker.cal.ds


def test_calibration_roundtrip(tracker, tmp_path):
    tracker.cal.save(tmp_path / "cal.npz")
    cal = Calibration.load(tmp_path / "cal.npz")
    assert cal.as_dict() == tracker.cal.as_dict()
    assert all(np.array_equal(a, b) for a, b in zip(cal.ball, tracker.cal.ball, strict=True))
    det = Tracker(cal).process(frame_with_ball(SimBallSource.contact_x()), t=0)
    assert det.distance < 0.05


def test_calibration_steps_and_checks():
    tr = Tracker()
    f = frame_with_ball(SimBallSource.contact_x())
    assert tr.process(f, t=0) is None
    assert tr.cal.missing() == ["ball template", "platform", "contact position"]
    tr.add_ball_template(f, SimBallSource.ball_box(SimBallSource.contact_x()))
    with pytest.raises(ValueError, match="platform"):
        tr.set_contact(tr.process(f, t=0))
    tr.set_platform(f, SimBallSource.platform_box())
    tr.set_contact(tr.process(f, t=0))
    assert tr.cal.missing() == []
    contact = tr.contact_position()
    x0, x1, y0, y1 = SimBallSource.platform_box()
    tr.set_platform(f, (x0 + 20, x1, y0, y1))  # a new template keeps the contact in place
    assert tr.cal.missing() == []
    assert tr.contact_position() == pytest.approx(contact)
    with pytest.raises(ValueError, match="frame size"):
        tr.process(f[:200], t=0)
    with pytest.raises(ValueError, match="inside the image"):
        tr.set_platform(f, (-10, 50, 0, 50))
    with pytest.raises(ValueError, match="no ball|not detected"):
        tr.set_contact(tr.process(frame_with_ball(-500), t=0))


def follow(tr, t, x, score=0.9, dy=0.0):
    """Feed one ball match (x, y in diameters) to the exit trail."""
    d = tr.cal.ball_diameter
    return tr._follow(t, x * d, 100 + dy * d, score, score >= tr.threshold, entry_left=True, w=480)


def test_fast_ball_about_to_leave_counts_as_at_the_edge(tracker):
    assert follow(tracker, 0.0, 1.2) is False
    # its box is 0.35 diameters from the edge, but at this speed it is out next frame
    assert follow(tracker, 0.04, 0.85) is True


def test_weak_matches_continuing_the_trail_follow_the_ball_out(tracker):
    assert follow(tracker, 0.0, 1.5) is False
    weak = tracker.threshold - 0.1
    assert follow(tracker, 0.04, 1.4, weak) is False  # not visible, still followed
    assert follow(tracker, 0.08, 0.6, weak) is True  # reached the entry edge


def test_unrelated_weak_matches_do_not_extend_the_trail(tracker):
    weak = tracker.threshold - 0.1
    follow(tracker, 0.0, 1.5)
    assert follow(tracker, 0.04, 1.4, weak, dy=0.5) is None  # off the track (falling)
    assert follow(tracker, 0.08, 0.6, tracker.threshold - 0.3) is None  # background
    assert follow(tracker, 0.5, 0.6, weak) is None  # trail too old


class MovedPlatform(SimBallSource):
    PLATFORM_X0 = SimBallSource.PLATFORM_X0 - 60  # a new animal, mounted further left


def test_platform_moved_for_a_new_animal(tracker):
    cx = MovedPlatform.contact_x()
    f = MovedPlatform(lambda: 0.0)
    f.x = cx
    frame = f.render()
    # too far for the platform search window: the ball doesn't look at contact
    assert tracker.process(frame, t=0).distance > 0.3
    # dragged roughly onto it: found there, template kept, contact moved along
    template = tracker.cal.platform
    tracker.move_platform(_center_x(MovedPlatform.platform_box()) + 10, 250 - 5)
    det = tracker.process(frame, t=0)
    assert tracker.cal.platform is template
    assert det.platform_x == pytest.approx(_center_x(MovedPlatform.platform_box()), abs=4)
    assert det.distance < 0.1
    with pytest.raises(ValueError, match="inside the image"):
        tracker.move_platform(470, 250)


def test_contact_placed_by_hand(tracker):
    cx = SimBallSource.contact_x()
    tracker.set_contact_at(cx - 30, tracker.contact_position()[1])
    det = tracker.process(frame_with_ball(cx - 30), t=0)
    assert det.distance < 0.05


def _center_x(box):
    return (box[0] + box[1]) / 2


class RaisedPlatform(SimBallSource):
    """The platform mounted 20 px higher; the rails (and so the ball) don't move."""

    @classmethod
    def platform_box(cls):
        x0, x1, y0, y1 = SimBallSource.platform_box()
        return x0, x1, y0 - 20, y1 - 20


def test_contact_stays_at_track_height_when_the_platform_moves_up(tracker):
    _, contact_y = tracker.contact_position()
    sim = RaisedPlatform(lambda: 0.0)
    sim.x = RaisedPlatform.contact_x()
    frame = sim.render()
    x0, x1, y0, y1 = RaisedPlatform.platform_box()
    tracker.move_platform((x0 + x1) / 2, (y0 + y1) / 2)
    det = tracker.process(frame, t=0)
    assert det.platform_y == pytest.approx((y0 + y1) / 2, abs=4)
    assert det.contact_y == contact_y  # still on the rails
    assert det.distance < 0.05


def test_calibration_saved_before_track_contact_loads(tracker, tmp_path):
    import io

    meta = {k: v for k, v in tracker.cal.as_dict().items() if k not in ("ball_diameter", "missing")}
    del meta["contact"]
    pb = tracker.cal.platform_box
    meta["contact_offset"] = (-50.0, -40.0)  # relative to the platform center, both axes
    buf = io.BytesIO()
    np.savez_compressed(buf, meta=np.array(repr(meta)), ball0=tracker.cal.ball[0],
                        platform=tracker.cal.platform)  # fmt: skip
    (tmp_path / "old.npz").write_bytes(buf.getvalue())
    cal = Calibration.load(tmp_path / "old.npz")
    assert cal.contact == (-50.0, (pb[2] + pb[3]) / 2 - 40.0)
