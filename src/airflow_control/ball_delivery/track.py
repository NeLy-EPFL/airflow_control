"""Ball and platform tracking in a side-view behavior camera, by template matching.

The matching code (``to_gray_downsampled``, ``make_template``, ``ncc_map``,
``locate``) comes from scapepp's ``balltrack`` module
(https://github.com/NeLy-EPFL/scapepp, ``twop`` branch), unchanged apart from
formatting. A sphere looks the same however it rolls, so a template of the
ball cut from one frame matches it anywhere along its track. Per frame,
normalized cross-correlation (NCC) of the template over the downsampled frame
gives the ball position (template center at the best match) and a score in
[-1, 1]: the ball counts as visible when the score exceeds a threshold.

Frames are padded so a ball partly out of view still matches; a ball mostly
out of view (waiting at the edge of the image) needs a template of its own,
cut at that position (boxes may extend past the frame edge).

On top of that, :class:`Tracker` adds what the closed loop needs:

- the **platform** the fly stands on, located with its own template but only
  within a small window around where it was marked (it does not move; a global
  search would sometimes match the ball's dark lower half instead);
- the **contact position**: where the ball center is when the ball touches the
  platform. The ball runs on fixed rails while the platform can be moved, so the
  contact follows the platform along the track (x) and stays at the track's
  height (y); the ball's distance to it is measured along the track, in ball
  diameters;
- whether the ball is at the image edge it comes from: the only place it can
  leave the view other than by falling off the track.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.signal import fftconvolve

DEFAULT_DOWNSAMPLE = 4
# A match must overlap the real frame by at least this fraction of the template's
# area; otherwise frames without a ball put their best match in a corner with
# only a sliver of template on real pixels.
MIN_INSIDE_FRACTION = 0.25
# Windows with less than this fraction of the template's own intensity variance
# (e.g. flat black background) can't be the ball; without this floor NCC's
# denominator is ~0 there and produces nonsense scores >> 1.
_MIN_VARIANCE_FRACTION = 0.05
# A ball whose box comes within this many diameters of the image edge on its
# entry side counts as at that edge (see Detection.at_entry_edge).
EDGE_MARGIN = 0.25
# A ball leaving the view fast is only matched weakly on its way out (motion
# blur, partly out of view). For TRAIL_S after the last sighting, a match down to
# TRAIL_SCORE_DROP below the threshold still counts as the ball if it continues
# its path along the track: at most TRAIL_STEP_X diameters along it and
# TRAIL_STEP_Y across it from the previous position.
TRAIL_S = 0.3
TRAIL_SCORE_DROP = 0.15
TRAIL_STEP_X = 1.0
TRAIL_STEP_Y = 0.25


def to_gray_downsampled(frame, ds: int = DEFAULT_DOWNSAMPLE) -> np.ndarray:
    f = np.asarray(frame)
    if f.ndim == 3:
        f = f[..., 0]
    f = f.astype(np.float32)
    h, w = f.shape
    return f[: h - h % ds, : w - w % ds].reshape(h // ds, ds, w // ds, ds).mean((1, 3))


def make_template(
    frame, box: tuple[int, int, int, int], ds: int = DEFAULT_DOWNSAMPLE
) -> np.ndarray:
    """*box* = (x0, x1, y0, y1) in frame px. The box may extend past the frame
    edge: outside pixels are filled with the frame's median, as ``locate`` pads
    frames, so a template of a ball partly out of view matches consistently."""
    x0, x1, y0, y1 = (int(v) // ds for v in box)
    img = to_gray_downsampled(frame, ds)
    h, w = img.shape
    pad = max(0, -x0, -y0, x1 - w, y1 - h)
    if pad:
        img = np.pad(img, pad, mode="constant", constant_values=float(np.median(img)))
    return img[y0 + pad : y1 + pad, x0 + pad : x1 + pad]


def ncc_map(image: np.ndarray, template: np.ndarray) -> np.ndarray:
    """NCC of *template* at every position where it fits fully inside *image*:
    result[y, x] is the match with the template's top-left at (y, x)."""
    n = template.size
    tz = template - template.mean()
    tvar = float((tz**2).sum()) / n
    num = fftconvolve(image, tz[::-1, ::-1], mode="valid")
    ones = np.ones_like(template)
    s1 = fftconvolve(image, ones, mode="valid")
    s2 = fftconvolve(image * image, ones, mode="valid")
    var = np.maximum(s2 - s1**2 / n, _MIN_VARIANCE_FRACTION * tvar * n)
    return num / (np.sqrt(tvar * n) * np.sqrt(var))


_inside_cache: dict = {}


def _inside_fraction(img_shape, pad_shape, t_shape) -> np.ndarray:
    """For every 'valid' template position in the padded frame, the fraction of
    the template's area on real (unpadded) pixels. Cached per shape."""
    key = (img_shape, pad_shape, t_shape)
    if key not in _inside_cache:
        (h, w), (py, px) = img_shape, pad_shape
        ind = np.zeros((h + 2 * py, w + 2 * px), dtype=np.float32)
        ind[py : py + h, px : px + w] = 1
        _inside_cache[key] = fftconvolve(ind, np.ones(t_shape, np.float32), mode="valid") / (
            t_shape[0] * t_shape[1]
        )
    return _inside_cache[key]


def locate(frame, templates, ds: int = DEFAULT_DOWNSAMPLE, pad: bool = True):
    """(x, y, score, template index) of the best match of any template, x/y in
    frame px. With *pad*, x/y may lie outside the frame, but the match always
    overlaps it by >= MIN_INSIDE_FRACTION."""
    img = to_gray_downsampled(frame, ds)
    best = None
    for k, t in enumerate(templates):
        py, px = t.shape if pad else (0, 0)
        work = (
            np.pad(
                img, ((py, py), (px, px)), mode="constant", constant_values=float(np.median(img))
            )
            if pad
            else img
        )
        m = ncc_map(work, t)
        if pad:
            m = np.where(
                _inside_fraction(img.shape, (py, px), t.shape) >= MIN_INSIDE_FRACTION, m, -np.inf
            )
        y, x = np.unravel_index(int(np.argmax(m)), m.shape)
        cand = ((x - px + t.shape[1] / 2) * ds, (y - py + t.shape[0] / 2) * ds, float(m[y, x]), k)
        if best is None or cand[2] > best[2]:
            best = cand
    return best


# --- closed-loop tracker -----------------------------------------------------

Box = tuple[int, int, int, int]  # (x0, x1, y0, y1) in frame px


def _center(box: Box) -> tuple[float, float]:
    return (box[0] + box[1]) / 2, (box[2] + box[3]) / 2


@dataclass
class Calibration:
    """Everything the tracker learns from the operator, for one camera view.

    Templates are stored downsampled by ``ds`` and are only valid for frames of
    ``frame_shape``.
    """

    ds: int = DEFAULT_DOWNSAMPLE
    frame_shape: tuple[int, int] | None = None  # (h, w)
    ball: list[np.ndarray] = field(default_factory=list)
    ball_boxes: list[Box] = field(default_factory=list)
    platform: np.ndarray | None = None
    platform_box: Box | None = None
    # ball center at contact: (x minus the platform center's x, y), frame px.
    # x follows the platform; y is the track's height (the rails don't move)
    contact: tuple[float, float] | None = None

    @property
    def ball_diameter(self) -> float | None:
        """Ball size in frame px: the first template's box width."""
        if not self.ball_boxes:
            return None
        b = self.ball_boxes[0]
        return float(b[1] - b[0])

    def missing(self) -> list[str]:
        """What still has to be marked before the closed loop can run."""
        out = []
        if not self.ball:
            out.append("ball template")
        if self.platform is None:
            out.append("platform")
        if self.contact is None:
            out.append("contact position")
        return out

    def as_dict(self) -> dict:
        return {
            "ds": self.ds,
            "frame_shape": self.frame_shape,
            "ball_boxes": self.ball_boxes,
            "platform_box": self.platform_box,
            "contact": self.contact,
            "ball_diameter": self.ball_diameter,
            "missing": self.missing(),
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        arrays = {f"ball{i}": t for i, t in enumerate(self.ball)}
        if self.platform is not None:
            arrays["platform"] = self.platform
        meta = {k: v for k, v in self.as_dict().items() if k not in ("ball_diameter", "missing")}
        buf = io.BytesIO()
        np.savez_compressed(buf, meta=np.array(repr(meta)), **arrays)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(buf.getvalue())
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> Calibration:
        import ast

        with np.load(path) as z:
            meta = ast.literal_eval(str(z["meta"]))
            n = sum(1 for k in z.files if k.startswith("ball"))
            ball = [z[f"ball{i}"] for i in range(n)]
            platform = z["platform"] if "platform" in z.files else None

        def box(b):
            return tuple(int(v) for v in b) if b is not None else None

        contact = meta.get("contact")
        old = meta.get("contact_offset")  # before 2026-10-13: (dx, dy) to the platform
        if contact is None and old and meta["platform_box"]:
            pb = meta["platform_box"]
            contact = (old[0], (pb[2] + pb[3]) / 2 + old[1])
        return cls(
            ds=meta["ds"],
            frame_shape=tuple(meta["frame_shape"]) if meta["frame_shape"] else None,
            ball=ball,
            ball_boxes=[box(b) for b in meta["ball_boxes"]],
            platform=platform,
            platform_box=box(meta["platform_box"]),
            contact=tuple(float(v) for v in contact) if contact else None,
        )


@dataclass(frozen=True)
class Detection:
    t: float  # frame arrival time, time.time()
    seq: int  # frame number from the source
    ball_x: float
    ball_y: float
    ball_score: float
    ball_template: int
    visible: bool
    platform_x: float | None
    platform_y: float | None
    platform_score: float | None
    contact_x: float | None
    contact_y: float | None
    distance: float | None  # ball center to contact position along the track, in ball diameters
    # the ball touches the image edge on the side it comes from (away from the
    # platform), the only place it can legitimately leave the view, or will by the
    # next frame at its current speed. Also set while the ball is not visible but
    # its trail is still followed (see TRAIL_S); None when unknown.
    at_entry_edge: bool | None = None

    def as_dict(self) -> dict:
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


class Tracker:
    """Finds the ball, the platform and the ball's distance to contact in a frame."""

    def __init__(
        self,
        calibration: Calibration | None = None,
        *,
        threshold: float = 0.6,
        platform_threshold: float = 0.5,
        platform_margin: float = 0.25,
    ):
        self.cal = calibration or Calibration()
        self.threshold = threshold  # ball visible when score >= this
        self.platform_threshold = platform_threshold  # below: keep the last good position
        self.platform_margin = platform_margin  # search window, fraction of the platform box
        self._platform_pos: tuple[float, float] | None = None
        self._trail: tuple[float, float, float, float] | None = None  # t, x, y, x speed

    # --- calibration ------------------------------------------------------
    def _check_shape(self, frame: np.ndarray) -> None:
        shape = tuple(frame.shape[:2])
        if self.cal.frame_shape is None:
            self.cal.frame_shape = shape
        elif self.cal.frame_shape != shape:
            raise ValueError(
                f"frame size {shape[1]}x{shape[0]} differs from the calibration's "
                f"{self.cal.frame_shape[1]}x{self.cal.frame_shape[0]}: clear and recalibrate"
            )

    @staticmethod
    def _norm_box(box) -> Box:
        x0, x1, y0, y1 = (int(round(v)) for v in box)
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        return x0, x1, y0, y1

    def add_ball_template(self, frame: np.ndarray, box) -> None:
        """Add a ball template. The first one also sets the ball diameter (its
        width), so draw it tight around the whole ball."""
        self._check_shape(frame)
        box = self._norm_box(box)
        if box[1] - box[0] < 4 * self.cal.ds or box[3] - box[2] < 4 * self.cal.ds:
            raise ValueError("box too small")
        self.cal.ball.append(make_template(frame, box, self.cal.ds))
        self.cal.ball_boxes.append(box)

    def _check_inside(self, box: Box) -> None:
        h, w = self.cal.frame_shape
        if box[0] < 0 or box[2] < 0 or box[1] > w or box[3] > h:
            raise ValueError("the platform box must lie inside the image")

    def set_platform(self, frame: np.ndarray, box) -> None:
        """Cut the platform template. A contact position already set keeps its
        place in the image."""
        self._check_shape(frame)
        box = self._norm_box(box)
        self._check_inside(box)
        if box[1] - box[0] < 4 * self.cal.ds or box[3] - box[2] < 4 * self.cal.ds:
            raise ValueError("box too small")
        contact = self.contact_position()
        self.cal.platform = make_template(frame, box, self.cal.ds)
        self.cal.platform_box = box
        self._platform_pos = _center(box)
        if contact is not None:
            self.set_contact_at(*contact)

    def move_platform(self, x: float, y: float) -> None:
        """The platform is now around (x, y), e.g. for a new animal: search for it
        there, keeping its template. The contact position moves along."""
        if self.cal.platform is None:
            raise ValueError("mark the platform first")
        x0, x1, y0, y1 = self.cal.platform_box
        cx, cy = _center(self.cal.platform_box)
        dx, dy = int(round(x - cx)), int(round(y - cy))
        box = (x0 + dx, x1 + dx, y0 + dy, y1 + dy)
        self._check_inside(box)
        self.cal.platform_box = box
        self._platform_pos = _center(box)

    def platform_position(self) -> tuple[float, float] | None:
        """Where the platform was last found."""
        if self.cal.platform_box is None:
            return None
        return self._platform_pos or _center(self.cal.platform_box)

    def contact_position(self) -> tuple[float, float] | None:
        """Contact position in the image, from the platform's last position."""
        pos = self.platform_position()
        if pos is None or self.cal.contact is None:
            return None
        return pos[0] + self.cal.contact[0], self.cal.contact[1]

    def set_contact_at(self, x: float, y: float) -> None:
        """Ball center when touching the platform, given in the image."""
        pos = self.platform_position()
        if pos is None:
            raise ValueError("mark the platform first")
        self.cal.contact = (float(x - pos[0]), float(y))

    def set_contact(self, det: Detection) -> None:
        """Use the ball position in *det* (ball touching the platform) as contact."""
        if not det.visible:
            raise ValueError("the ball is not detected in the current frame")
        if det.platform_x is None:
            raise ValueError("mark the platform first")
        self.set_contact_at(det.ball_x, det.ball_y)

    def clear(self) -> None:
        self.cal = Calibration(ds=self.cal.ds)
        self._platform_pos = None
        self._trail = None

    # --- tracking ---------------------------------------------------------
    def _locate_platform(self, frame: np.ndarray):
        cal = self.cal
        x0, x1, y0, y1 = cal.platform_box
        h, w = frame.shape[:2]
        m = int(self.platform_margin * max(x1 - x0, y1 - y0)) + cal.ds
        wx0, wy0 = max(0, x0 - m), max(0, y0 - m)
        sub = frame[wy0 : min(h, y1 + m), wx0 : min(w, x1 + m)]
        px, py, ps, _ = locate(sub, [cal.platform], cal.ds, pad=False)
        return px + wx0, py + wy0, ps

    def _follow(self, t, bx, by, score, visible, *, entry_left: bool, w: int) -> bool | None:
        """Update the ball's trail; whether it is at (or about to reach) the entry edge."""
        d = self.cal.ball_diameter
        trail = self._trail
        if trail is not None and t - trail[0] > TRAIL_S:
            trail = None
        if not visible:  # a weaker match continuing the trail is still the ball
            if (
                trail is None
                or score < self.threshold - TRAIL_SCORE_DROP
                or abs(bx - trail[1]) > TRAIL_STEP_X * d
                or abs(by - trail[2]) > TRAIL_STEP_Y * d
            ):
                self._trail = trail
                return None
        vx = 0.0 if trail is None else bx - trail[1]
        self._trail = (t, bx, by, vx)
        # within a quarter diameter of the edge counts, now or one frame ahead
        if entry_left:  # platform on the right: the ball comes from the left
            return bool(min(bx, bx + vx) - d / 2 <= EDGE_MARGIN * d)
        return bool(max(bx, bx + vx) + d / 2 >= w - EDGE_MARGIN * d)

    def process(self, frame: np.ndarray, *, t: float, seq: int = 0) -> Detection | None:
        """Detection for one frame, or None while no ball template is set."""
        cal = self.cal
        if not cal.ball:
            return None
        self._check_shape(frame)
        bx, by, bs, k = locate(frame, cal.ball, cal.ds)
        visible = bs >= self.threshold
        px = py = ps = cx = cy = dist = edge = None
        if cal.platform is not None:
            px, py, ps = self._locate_platform(frame)
            if ps >= self.platform_threshold or self._platform_pos is None:
                self._platform_pos = (px, py)
            px, py = float(self._platform_pos[0]), float(self._platform_pos[1])
            if cal.contact is not None:
                cx, cy = px + cal.contact[0], cal.contact[1]
                if visible:  # along the track
                    dist = abs(bx - cx) / cal.ball_diameter
                edge = self._follow(t, bx, by, bs, visible, entry_left=px >= cx, w=frame.shape[1])
        return Detection(
            t=t,
            seq=seq,
            ball_x=float(bx),
            ball_y=float(by),
            ball_score=bs,
            ball_template=k,
            visible=visible,
            platform_x=px,
            platform_y=py,
            platform_score=None if ps is None else float(ps),
            contact_x=cx,
            contact_y=cy,
            distance=dist,
            at_entry_edge=edge,
        )
