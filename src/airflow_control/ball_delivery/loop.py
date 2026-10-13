"""Closed-loop ball delivery: blow the ball to the platform, let the fly push it.

States (``ClosedLoop.state``)::

    off      disarmed; the loop does not touch the MFC
    ramp     air on: start at start_flow, +step_flow every step_interval_s up to
             max_flow, until the ball is at contact for contact_hold_s
               -> air off, contact
             at max_flow for max_at_max_s without contact
               -> air off, rest
    contact  air off, the fly has the ball. Once the ball has been away from
             contact by more than release_dist (or out of sight) for
             release_hold_s -> ramp again, from start_flow
    rest     air off for rest_s after a ramp that got no contact (ball stuck,
             fallen off, or not tracked), then ramp again from start_flow
    paused   no camera frame for frame_timeout_s (camera not triggered, octacam
             closed): air off; resumes when frames come back

Once armed, the loop never stops by itself: delivering the ball to the animal
matters more than saving air when something looks wrong. Only a disarm, a
manual action on its MFC (setpoint, puff, stop) or the GUI exiting stop it.

A ball normally leaves the view through the edge it comes from (rolling back
down the track). One that vanishes elsewhere (the animal covering it, or a
fall) holds the loop for up to missing_hold_s: the ramp doesn't step and a
contact isn't released. After that it is treated like a ball that rolled back.
A ball counts as on the track only after MIN_SEEN_FRAMES sightings in a row
(a lone match doesn't place it).

Distances are in ball diameters, measured from the ball center to the contact
position (see :mod:`airflow_control.ball_delivery.track`). Any manual action on the
loop's MFC (setpoint, puff, stop) disarms the loop.
"""

from __future__ import annotations

import collections
import dataclasses
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from airflow_control.ball_delivery.track import Calibration, Detection, Tracker
from airflow_control.controller import Channel, EventLog
from airflow_control.frames import Frame, FrameSource

log = logging.getLogger("airflow_control")


# Sightings in a row needed before a vanishing ball counts as gone from the track.
MIN_SEEN_FRAMES = 3


@dataclass
class LoopParams:
    start_flow: float = 0.6  # first flow of each ramp
    step_flow: float = 0.1  # ramp increment
    step_interval_s: float = 2.0  # time at each ramp level
    max_flow: float = 2.0  # ramp ceiling (also limited by the MFC cap)
    max_at_max_s: float = 10.0  # at max_flow this long without contact -> rest
    rest_s: float = 10.0  # air off this long, then ramp again
    contact_dist: float = 0.15  # ball diameters: closer than this = contact
    contact_hold_s: float = 0.2  # ... for this long -> air off
    release_dist: float = 0.3  # ball diameters: farther than this (or gone) = pushed away
    release_hold_s: float = 1.0  # ... for this long -> ramp again
    frame_timeout_s: float = 1.0  # no frame for this long -> paused, air off
    missing_hold_s: float = 2.0  # vanished off the track: hold the loop at most this long
    threshold: float = 0.6  # ball match score counted as visible

    def update(self, **values) -> None:
        names = {f.name for f in dataclasses.fields(self)}
        for k, v in values.items():
            if k not in names:
                raise ValueError(f"unknown loop parameter {k!r}")
            v = float(v)
            if v < 0 or v != v:
                raise ValueError(f"{k} must be >= 0")
            setattr(self, k, v)
        if self.step_interval_s <= 0:
            raise ValueError("step_interval_s must be > 0")
        if self.release_dist < self.contact_dist:
            raise ValueError("release_dist must be >= contact_dist")
        if self.start_flow > self.max_flow:
            raise ValueError("start_flow must be <= max_flow")

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


class ClosedLoop:
    """The state machine. Feed it detections (``update``) and clock ticks (``tick``)."""

    def __init__(self, channel: Channel, events: EventLog, params: LoopParams | None = None):
        self.channel = channel
        self.events = events
        self.params = params or LoopParams()
        self.state = "off"
        self.reason: str | None = None  # why the last off/rest/paused
        self.flow = 0.0  # what the loop last asked for
        self.cycles = 0  # completed ramp -> contact cycles
        self.last_contact_flow: float | None = None
        self.vanished = 0  # times the ball vanished off the track (fell off? covered?)
        self._lock = threading.RLock()
        self._t_state = time.monotonic()
        self._t_level = 0.0
        self._t_at_max: float | None = None
        self._contact_since: float | None = None
        self._away_since: float | None = None
        self._last_frame: float | None = None
        # where the ball was last seen: at its entry edge (True), elsewhere
        # (False), not seen since arming/resuming (None); and since when it is
        # missing after vanishing elsewhere
        self._last_seen_edge: bool | None = None
        self._seen_run = 0  # consecutive frames with the ball visible
        self._missing_since: float | None = None

    @property
    def armed(self) -> bool:
        return self.state != "off"

    def _forget_ball(self) -> None:
        self._last_seen_edge = None
        self._seen_run = 0
        self._missing_since = None

    @property
    def max_flow(self) -> float:
        return min(self.params.max_flow, self.channel.mfc.cap)

    # --- MFC writes ---------------------------------------------------------
    def _set(self, flow: float, why: str) -> None:
        flow = round(min(flow, self.max_flow), 4)
        self.channel.set_continuous(flow)  # logged by the channel as "continuous"
        self.flow = flow
        self.events.write(self.channel.name, f"loop_{why}", setpoint=flow, state=self.state)

    def _goto(self, state: str, now: float, reason: str | None = None) -> None:
        self.state = state
        self.reason = reason
        self._t_state = now
        self._contact_since = self._away_since = None

    def _start_ramp(self, now: float, why: str) -> None:
        self._goto("ramp", now)
        self._t_level = now
        self._t_at_max = now if self.params.start_flow >= self.max_flow else None
        self._set(self.params.start_flow, why)

    def _air_off(self, state: str, now: float, why: str, reason: str | None = None) -> None:
        self._goto(state, now, reason)
        try:
            self._set(0.0, why)
        except Exception:
            log.exception("closed loop: failed to turn the air off")
            self.channel._safe_stop(f"loop {why}")
            self.flow = 0.0

    # --- control ----------------------------------------------------------
    def arm(self, det: Detection | None) -> None:
        with self._lock:
            if self.armed:
                return
            now = time.monotonic()
            self.cycles = self.vanished = 0
            self._forget_ball()
            self._last_frame = now if det is not None else None
            self.events.write(self.channel.name, "loop_arm", params=self.params.as_dict())
            if det is not None and self._at_contact(det):
                self._air_off("contact", now, "contact")
            else:
                self._start_ramp(now, "ramp_start")

    def disarm(self, reason: str = "user", *, air_off: bool = True) -> None:
        """Back to off. With ``air_off`` the MFC is set to 0 (unless a manual
        action already wrote a new setpoint, then pass air_off=False)."""
        with self._lock:
            if self.state == "off":
                return
            was = self.state
            self._goto("off", time.monotonic(), reason)
            if air_off and was == "ramp":
                self.channel._safe_stop(f"loop disarmed ({reason})")
                self.flow = 0.0
            self.events.write(self.channel.name, "loop_disarm", reason=reason, was=was)

    def _at_contact(self, det: Detection) -> bool:
        return det.distance is not None and det.distance <= self.params.contact_dist

    def _away(self, det: Detection) -> bool:
        if not det.visible:  # gone back down the track, unless it is missing
            return self._missing_since is None
        return det.distance is None or det.distance > self.params.release_dist

    def _track_ball(self, det: Detection, now: float) -> None:
        """Follow where the ball leaves the view."""
        self._seen_run = self._seen_run + 1 if det.visible else 0
        if det.at_entry_edge:  # seen, or its trail reached, the entry edge
            self._last_seen_edge = True
            self._missing_since = None
        elif det.visible:
            if self._seen_run >= MIN_SEEN_FRAMES:  # placed on the track
                self._last_seen_edge = False
            self._missing_since = None
        elif self._last_seen_edge is False:  # vanished away from the entry edge
            if self._missing_since is None:
                self._missing_since = now
            elif now - self._missing_since >= self.params.missing_hold_s:
                # stop waiting: carry on as if it had rolled back down the track
                self.vanished += 1
                self.events.write(self.channel.name, "loop_ball_vanished",
                                  state=self.state, flow=self.flow)  # fmt: skip
                self._forget_ball()

    def update(self, det: Detection, now: float | None = None) -> None:
        """One tracked frame."""
        now = time.monotonic() if now is None else now
        p = self.params
        with self._lock:
            self._last_frame = now
            if not self.armed:
                return
            if self.state == "paused":
                self._forget_ball()
                if self._at_contact(det):
                    self._goto("contact", now)
                else:
                    self._start_ramp(now, "resume")
                return

            self._track_ball(det, now)
            contact = self._at_contact(det)
            away = self._away(det)
            self._contact_since = (self._contact_since or now) if contact else None
            self._away_since = (self._away_since or now) if away else None

            if self.state == "ramp":
                if (
                    self._contact_since is not None
                    and now - self._contact_since >= p.contact_hold_s
                ):
                    self.last_contact_flow = self.flow
                    self.cycles += 1
                    self._air_off("contact", now, "contact")
                    return
                self._step_ramp(now)
            elif self.state == "contact":
                if self._away_since is not None and now - self._away_since >= p.release_hold_s:
                    self._start_ramp(now, "released")
            elif self.state == "rest":
                if (
                    self._contact_since is not None
                    and now - self._contact_since >= p.contact_hold_s
                ):
                    self._goto("contact", now)  # got there after all
                    self.events.write(self.channel.name, "loop_contact", setpoint=0.0)
                else:
                    self._end_rest(now)

    def _end_rest(self, now: float) -> None:
        if now - self._t_state >= self.params.rest_s:
            self._forget_ball()
            self._start_ramp(now, "retry")

    def _step_ramp(self, now: float) -> None:
        p = self.params
        if self._missing_since is not None:  # hold while the ball is missing
            return
        if self.flow >= self.max_flow - 1e-9:
            if self._t_at_max is None:
                self._t_at_max = now
            if now - self._t_at_max >= p.max_at_max_s:
                self._air_off(
                    "rest", now, "rest",
                    f"no contact after {p.max_at_max_s:g} s at {self.max_flow:g}",
                )  # fmt: skip
            return
        if now - self._t_level >= p.step_interval_s:
            self._t_level = now
            self._set(self.flow + p.step_flow, "step")

    def tick(self, now: float | None = None) -> None:
        """Clock tick without a frame: the frame watchdog."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if self.state == "rest":  # air is off: no frames needed to end it
                self._end_rest(now)
                return
            if self.state not in ("ramp", "contact"):
                return
            last = self._last_frame
            if last is None or now - last >= self.params.frame_timeout_s:
                if self.state == "ramp":
                    self._air_off("paused", now, "paused", "no camera frames")
                else:  # air is already off
                    self._goto("paused", now, "no camera frames")
                    self.events.write(self.channel.name, "loop_paused", setpoint=0.0)
            elif self.state == "ramp":
                self._step_ramp(now)  # keep ramping between frames

    def status(self) -> dict:
        now = time.monotonic()
        return {
            "state": self.state,
            "armed": self.armed,
            "reason": self.reason,
            "flow": self.flow,
            "max_flow": self.max_flow,
            "cycles": self.cycles,
            "last_contact_flow": self.last_contact_flow,
            "vanished": self.vanished,
            "missing_s": None
            if self._missing_since is None
            else round(now - self._missing_since, 1),
            "in_state_s": round(now - self._t_state, 1),
            # how long the current contact / pushed-away condition has held (armed only)
            "contact_s": None
            if self._contact_since is None
            else round(now - self._contact_since, 1),
            "away_s": None if self._away_since is None else round(now - self._away_since, 1),
            "last_seen_edge": self._last_seen_edge,
            "channel": self.channel.name,
            "params": self.params.as_dict(),
        }


class TrackLog:
    """Per-frame detections as JSONL (one line per tracked frame)."""

    def __init__(self, log_dir: Path | None):
        self.path: Path | None = None
        self._f = None
        self._lock = threading.Lock()
        if log_dir is not None:
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.path = log_dir / f"track_{stamp}.jsonl"

    def write(self, rec: dict) -> None:
        if self.path is None:
            return
        with self._lock:
            if self._f is None:  # created on the first tracked frame only
                self._f = open(self.path, "a", buffering=1 << 16)
            self._f.write(json.dumps(rec) + "\n")

    def close(self) -> None:
        with self._lock:
            if self._f:
                self._f.close()
                self._f = None


class Tracking:
    """Frame source -> tracker -> closed loop, plus calibration persistence."""

    def __init__(
        self,
        source: FrameSource,
        channel: Channel,
        events: EventLog,
        *,
        state_dir: Path | None,
        log_dir: Path | None = None,
        params: LoopParams | None = None,
        downsample: int = 4,
    ):
        self.source = source
        self.events = events
        self.state_dir = state_dir
        self.loop = ClosedLoop(channel, events, params)
        self.tracker = Tracker(Calibration(ds=downsample), threshold=self.loop.params.threshold)
        self.track_log = TrackLog(log_dir)
        self.frame: Frame | None = None  # latest frame, for calibration
        self.detection: Detection | None = None
        # latest (frame, its detection) pair, swapped atomically for the GUI view
        self.latest: tuple[Frame, Detection | None] | None = None
        self.track_ms: float | None = None
        self.error: str | None = None
        # (t, distance, score, visible) for the GUI plot
        self.history: collections.deque[tuple] = collections.deque(maxlen=1800)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ticker: threading.Thread | None = None
        self._load()

    # --- persistence ------------------------------------------------------
    @property
    def _cal_path(self) -> Path | None:
        return self.state_dir / "calibration.npz" if self.state_dir else None

    @property
    def _params_path(self) -> Path | None:
        return self.state_dir / "loop_params.json" if self.state_dir else None

    def _load(self) -> None:
        if self._cal_path and self._cal_path.exists():
            try:
                self.tracker.cal = Calibration.load(self._cal_path)
            except Exception as e:
                self.error = f"could not load {self._cal_path}: {e}"
        if self._params_path and self._params_path.exists():
            try:
                saved = json.loads(self._params_path.read_text())
                known = {f.name for f in dataclasses.fields(LoopParams)}
                self.loop.params.update(**{k: v for k, v in saved.items() if k in known})
                self.tracker.threshold = self.loop.params.threshold
            except Exception as e:
                self.error = f"could not load {self._params_path}: {e}"

    def _save(self) -> None:
        if self.state_dir is None:
            return
        self.tracker.cal.save(self._cal_path)
        self._params_path.write_text(json.dumps(self.loop.params.as_dict(), indent=2))

    # --- lifecycle --------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self.source.start(self._on_frame)
        self._ticker = threading.Thread(target=self._tick_loop, name="loop-tick", daemon=True)
        self._ticker.start()

    def close(self) -> None:
        self._stop.set()
        self.loop.disarm("shutdown")
        self.source.stop()
        if self._ticker:
            self._ticker.join(timeout=2)
            self._ticker = None
        self.track_log.close()

    def _tick_loop(self) -> None:
        while not self._stop.wait(0.1):
            try:
                self.loop.tick()
            except Exception:
                log.exception("closed loop tick failed")

    def _on_frame(self, frame: Frame) -> None:
        with self._lock:
            self.frame = frame
            t0 = time.perf_counter()
            try:
                det = self.tracker.process(frame.image, t=frame.t, seq=frame.seq)
                self.error = None
            except ValueError as e:
                det, self.error = None, str(e)
            self.track_ms = (time.perf_counter() - t0) * 1e3
            self.detection = det
            self.latest = (frame, det)
        if det is None:
            return
        self.history.append(
            (round(det.t, 3), None if det.distance is None else round(det.distance, 3),
             round(det.ball_score, 3), det.visible)
        )  # fmt: skip
        if det.contact_x is not None:  # calibrated: the loop can use it
            self.loop.update(det)
        self.track_log.write(
            {**det.as_dict(), "ts_ns": frame.source_ts_ns, "rec": frame.recording,
             "state": self.loop.state, "flow": self.loop.flow}
        )  # fmt: skip

    # --- operator actions (from the GUI) ----------------------------------
    def _current(self) -> Frame:
        if self.frame is None:
            raise ValueError("no camera frame yet")
        return self.frame

    def set_box(self, kind: str, box) -> None:
        if self.loop.armed:
            raise ValueError("disarm the closed loop before changing the calibration")
        with self._lock:
            frame = self._current()
            if kind == "ball":  # replaces all ball templates (the contact stays)
                self.tracker.cal.ball.clear()
                self.tracker.cal.ball_boxes.clear()
                self.tracker.add_ball_template(frame.image, box)
            elif kind == "ball_extra":
                if not self.tracker.cal.ball:
                    raise ValueError("set the main ball template first")
                self.tracker.add_ball_template(frame.image, box)
            elif kind == "platform":
                self.tracker.set_platform(frame.image, box)
            else:
                raise ValueError(f"unknown box kind {kind!r}")
            self.detection = self.tracker.process(frame.image, t=frame.t, seq=frame.seq)
            self.latest = (frame, self.detection)
        self.events.write(self.loop.channel.name, "calibration", kind=kind, box=list(box))
        self._save()

    def set_contact(self, at: tuple[float, float] | None = None) -> None:
        """Contact position: *at* (image px), or else the ball's current position.
        Without a ball in view and no contact yet, a first guess next to the
        platform, to be dragged into place. Allowed while armed."""
        tr = self.tracker
        with self._lock:
            frame = self._current()
            if at is None:
                det = tr.process(frame.image, t=frame.t, seq=frame.seq)
                if det is None:
                    raise ValueError("set the ball template first")
                if det.visible:
                    at = (det.ball_x, det.ball_y)
                elif tr.cal.contact is not None:
                    raise ValueError("the ball is not in view: drag the dashed circle instead")
                elif tr.cal.platform_box is not None:
                    at = self._contact_guess()
            if at is None:
                raise ValueError("mark the platform first")
            tr.set_contact_at(*at)
            self.detection = tr.process(frame.image, t=frame.t, seq=frame.seq)
            self.latest = (frame, self.detection)
        self.events.write(self.loop.channel.name, "calibration", kind="contact",
                          contact=[round(v, 1) for v in tr.cal.contact])  # fmt: skip
        self._save()

    def _contact_guess(self) -> tuple[float, float]:
        """Ball touching the platform's side that faces where the ball was marked."""
        cal = self.tracker.cal
        x0, x1, _, _ = cal.platform_box
        bx0, bx1, by0, by1 = cal.ball_boxes[0]
        r = cal.ball_diameter / 2
        x = x0 - r if (bx0 + bx1) / 2 < (x0 + x1) / 2 else x1 + r
        return x, (by0 + by1) / 2

    def move_platform(self, x: float, y: float) -> None:
        """The platform is now around (x, y) (new animal, camera nudged). Allowed
        while armed: the templates stay, only positions change."""
        with self._lock:
            frame = self._current()
            self.tracker.move_platform(x, y)
            self.detection = self.tracker.process(frame.image, t=frame.t, seq=frame.seq)
            self.latest = (frame, self.detection)
        self.events.write(self.loop.channel.name, "calibration", kind="platform_move",
                          box=list(self.tracker.cal.platform_box))  # fmt: skip
        self._save()

    def clear_calibration(self) -> None:
        if self.loop.armed:
            raise ValueError("disarm the closed loop before changing the calibration")
        with self._lock:
            self.tracker.clear()
            self.detection = None
            if self.frame is not None:
                self.latest = (self.frame, None)
        self._save()

    def set_params(self, values: dict) -> None:
        p = LoopParams(**self.loop.params.as_dict())
        p.update(**values)  # validate on a copy
        self.loop.params.update(**values)
        self.tracker.threshold = self.loop.params.threshold
        self.events.write(self.loop.channel.name, "loop_params", **values)
        self._save()

    def arm(self) -> None:
        missing = self.tracker.cal.missing()
        if missing:
            raise ValueError(f"calibration incomplete: mark the {', '.join(missing)}")
        if self.frame is None or time.time() - self.frame.t > self.loop.params.frame_timeout_s:
            raise ValueError("no live camera frames")
        self.loop.arm(self.detection)

    def status(self) -> dict:
        det, frame = self.detection, self.frame
        return {
            "source": self.source.status(),
            "frame": None
            if frame is None
            else {
                "seq": frame.seq,
                "t": frame.t,
                "w": frame.image.shape[1],
                "h": frame.image.shape[0],
            },
            "detection": det.as_dict() if det else None,
            "calibration": self.tracker.cal.as_dict(),
            "platform_threshold": self.tracker.platform_threshold,
            "loop": self.loop.status(),
            "track_ms": round(self.track_ms, 1) if self.track_ms else None,
            "track_log": str(self.track_log.path) if self.track_log.path else None,
            "error": self.error,
        }
