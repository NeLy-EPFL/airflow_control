"""A simulated ball for ``gui --sim --ball``: frames like the side camera's,
with the ball pushed by the simulated air flow."""

from __future__ import annotations

import time
from collections.abc import Callable

import numpy as np

from airflow_control.frames import Frame, FrameSource


class SimBallSource(FrameSource):
    """A synthetic side view: a ball on rails, blown toward a platform on the right.

    The ball moves right when the flow exceeds a per-run threshold (it varies
    from push to push, like a real ball), rolls back when it doesn't, stops at
    the platform, and is pushed back by the "fly" a few seconds after the air
    goes off.
    """

    kind = "sim"
    W, H, R = 480, 290, 48  # frame size, ball radius (px)
    PLATFORM_X0 = 330  # platform left edge
    REST_X = 10  # ball center when at rest: mostly out of view on the left

    def __init__(self, flow: Callable[[], float], *, fps: float = 30.0, seed: int = 0):
        super().__init__()
        self.flow = flow
        self.out_fps = fps
        self.rng = np.random.default_rng(seed)
        self.x = float(self.REST_X)
        self.needed = 1.0  # flow needed to roll the ball
        self._push_at: float | None = None
        self._background = self._make_background()
        self._ball, self._disk = self._make_ball()

    def describe(self) -> str:
        return "simulated ball"

    @classmethod
    def ball_box(cls, x: float) -> tuple[int, int, int, int]:
        y = cls.H - 75 - cls.R
        return (round(x - cls.R), round(x + cls.R), y - cls.R, y + cls.R)

    @classmethod
    def platform_box(cls) -> tuple[int, int, int, int]:
        return (cls.PLATFORM_X0, cls.PLATFORM_X0 + 100, cls.H - 90, cls.H - 10)

    @classmethod
    def contact_x(cls) -> float:
        return cls.PLATFORM_X0 - cls.R

    def _make_background(self) -> np.ndarray:
        rng = np.random.default_rng(1)
        f = rng.normal(20, 4, (self.H, self.W))
        f[self.H - 75 : self.H - 70, :] = 170  # rail
        f[self.H - 50 : self.H - 46, :] = 140
        x0, y0 = self.PLATFORM_X0, self.platform_box()[2]
        block = 60 + 25 * rng.normal(0, 1, (self.H - y0, self.W - x0))
        f[y0:, x0:] = block
        f[y0 : y0 + 4, x0:] = 120  # platform top edge
        return f

    def _make_ball(self):
        rng = np.random.default_rng(2)
        r = self.R
        yy, xx = np.mgrid[-r:r, -r:r]
        disk = xx**2 + yy**2 <= r**2
        texture = 120 + 60 * rng.normal(0, 1, disk.shape)
        texture[(yy > -10) & (yy < 4)] = 230  # highlight band
        return np.where(disk, texture, 0.0), disk

    def render(self) -> np.ndarray:
        f = self._background.copy()
        x0, _, y0, _ = self.ball_box(self.x)
        r2 = 2 * self.R
        # paste with clipping: the ball may be cut by the frame edge
        sx0, sy0 = max(0, -x0), max(0, -y0)
        dx0, dy0 = max(0, x0), max(0, y0)
        dx1, dy1 = min(self.W, x0 + r2), min(self.H, y0 + r2)
        if dx1 > dx0 and dy1 > dy0:
            patch = self._ball[sy0 : sy0 + dy1 - dy0, sx0 : sx0 + dx1 - dx0]
            mask = self._disk[sy0 : sy0 + dy1 - dy0, sx0 : sx0 + dx1 - dx0]
            region = f[dy0:dy1, dx0:dx1]
            region[mask] = patch[mask]
        f += self.rng.normal(0, 3, f.shape)
        return np.clip(f, 0, 255).astype(np.uint8)

    def step(self, dt: float) -> None:
        flow = self.flow()
        contact = self.contact_x()
        if self.x >= contact - 0.5:
            self.x = contact
            if flow < 0.2:  # air off at the platform: the fly pushes the ball away
                now = time.monotonic()
                if self._push_at is None:
                    self._push_at = now + self.rng.uniform(2, 5)
                elif now >= self._push_at:
                    self.x = contact - self.rng.uniform(80, 250)
                    self.needed = float(self.rng.uniform(0.7, 1.4))
                    self._push_at = None
            return
        self._push_at = None
        if flow > self.needed:
            self.x = min(contact, self.x + 150 * (flow - self.needed + 0.2) * dt)
        else:
            self.x = max(float(self.REST_X), self.x - 40 * dt)

    def _run(self) -> None:
        self.connected = True
        period = 1 / self.out_fps
        seq = 0
        try:
            while not self._stop.wait(period):
                self.step(period)
                seq += 1
                self._emit(Frame(image=self.render(), t=time.time(), seq=seq))
        finally:
            self.connected = False


def calibrate_sim(tracker) -> None:
    """Calibrate *tracker* (a :class:`~airflow_control.ball_delivery.track.Tracker`) for
    :class:`SimBallSource`, as an operator would: ball template at contact,
    a second one at the rest position (mostly out of view), platform, contact."""
    sim = SimBallSource(lambda: 0.0)
    sim.x = sim.contact_x()
    at_contact = sim.render()
    tracker.add_ball_template(at_contact, sim.ball_box(sim.x))
    tracker.set_platform(at_contact, sim.platform_box())
    sim.x = float(sim.REST_X)
    tracker.add_ball_template(sim.render(), sim.ball_box(sim.x))
    tracker.set_contact(tracker.process(at_contact, t=time.time()))
