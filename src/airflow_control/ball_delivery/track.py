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
  search would sometimes match the ball's dark lower half instead). It may be
  partly out of view: its box may extend past the image edge, and it is matched
  on the pixels in view only (masked NCC), both when its template is cut and
  when it is searched for;
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
# At least this fraction of the platform box must be in view (the rest may lie
# past the image edge), and of its template's known pixels to match on.
MIN_PLATFORM_INSIDE = 0.25  # the GUI keeps the box center in view


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


def cut_masked(
    frame, box: tuple[int, int, int, int], ds: int = DEFAULT_DOWNSAMPLE
) -> tuple[np.ndarray, np.ndarray]:
    """Template and mask (True where the box was in the frame) for *box*, which
    may extend past the frame edge; to match with ``masked_ncc_map``."""
    x0, x1, y0, y1 = (int(v) // ds for v in box)
    img = to_gray_downsampled(frame, ds)
    h, w = img.shape
    t = np.zeros((y1 - y0, x1 - x0), np.float32)
    mask = np.zeros(t.shape, bool)
    ix0, ix1, iy0, iy1 = max(x0, 0), min(x1, w), max(y0, 0), min(y1, h)
    if ix1 > ix0 and iy1 > iy0:
        t[iy0 - y0 : iy1 - y0, ix0 - x0 : ix1 - x0] = img[iy0:iy1, ix0:ix1]
        mask[iy0 - y0 : iy1 - y0, ix0 - x0 : ix1 - x0] = True
    return t, mask


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


def _corr(image: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    return fftconvolve(image, kernel[::-1, ::-1], mode="valid")


def masked_ncc_map(
    image: np.ndarray, valid: np.ndarray, template: np.ndarray, tmask: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """NCC like ``ncc_map``, but over the pixels known in both: *valid* marks the
    image's real pixels (not padding past the frame edge), *tmask* the
    template's (the part of its box that was in view when it was cut). Returns
    (NCC, number of pixels it was computed on), per template position."""
    f = np.where(valid, image, 0).astype(np.float64)
    m = valid.astype(np.float64)
    tm = tmask.astype(np.float64)
    t = np.where(tmask, template, 0).astype(np.float64)
    n = np.round(_corr(m, tm))
    nn = np.maximum(n, 1)
    sf, st = _corr(f, tm), _corr(m, t)
    cov = _corr(f, t) - sf * st / nn
    vf = _corr(f * f, tm) - sf**2 / nn
    vt = _corr(m, t * t) - st**2 / nn
    tvar = float(t[tmask].var()) if tmask.any() else 0.0
    vf = np.maximum(vf, _MIN_VARIANCE_FRACTION * tvar * nn)
    vt = np.maximum(vt, 1e-6 * nn)
    return cov / np.sqrt(vf * vt), n


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
    # the platform template's pixels that were in view when it was cut
    platform_mask: np.ndarray | None = None
    platform_box: Box | None = None  # may extend past the image edge
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
            arrays["platform_mask"] = self.platform_mask
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
            mask = z["platform_mask"] if "platform_mask" in z.files else None

        if platform is not None and mask is None:  # cut before masks: fully in view
            mask = np.ones(platform.shape, bool)

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
            platform_mask=mask,
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

    def _check_in_view(self, box: Box) -> None:
        """Enough of the platform box in view (the rest may lie past the edge)."""
        h, w = self.cal.frame_shape
        x0, x1, y0, y1 = box
        inside = max(0, min(x1, w) - max(x0, 0)) * max(0, min(y1, h) - max(y0, 0))
        if inside < MIN_PLATFORM_INSIDE * (x1 - x0) * (y1 - y0):
            raise ValueError(
                f"at least {MIN_PLATFORM_INSIDE:.0%} of the platform box must be in the image"
            )

    def set_platform(self, frame: np.ndarray, box) -> None:
        """Cut the platform template. A contact position already set keeps its
        place in the image."""
        self._check_shape(frame)
        box = self._norm_box(box)
        if box[1] - box[0] < 4 * self.cal.ds or box[3] - box[2] < 4 * self.cal.ds:
            raise ValueError("box too small")
        self._check_in_view(box)
        contact = self.contact_position()
        self.cal.platform, self.cal.platform_mask = cut_masked(frame, box, self.cal.ds)
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
        self._check_in_view(box)
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
    def _locate_platform(self, img: np.ndarray):
        """(x, y, score) of the platform's best match within platform_margin of its
        box, in frame px. *img*: the downsampled frame. Positions past the image
        edge are searched too, matching on the part in view."""
        cal = self.cal
        ds, t, tmask = cal.ds, cal.platform, cal.platform_mask
        th, tw = t.shape
        x0, x1, y0, y1 = cal.platform_box
        m = int(self.platform_margin * max(x1 - x0, y1 - y0)) // ds + 1
        wx, wy = x0 // ds - m, y0 // ds - m  # search window's top-left, in cells
        win = np.zeros((th + 2 * m, tw + 2 * m), np.float32)
        valid = np.zeros(win.shape, bool)
        h, w = img.shape
        ix0, ix1 = max(wx, 0), min(wx + win.shape[1], w)
        iy0, iy1 = max(wy, 0), min(wy + win.shape[0], h)
        if ix1 > ix0 and iy1 > iy0:
            win[iy0 - wy : iy1 - wy, ix0 - wx : ix1 - wx] = img[iy0:iy1, ix0:ix1]
            valid[iy0 - wy : iy1 - wy, ix0 - wx : ix1 - wx] = True
        ncc, n = masked_ncc_map(win, valid, t, tmask)
        ncc = np.where(n >= MIN_PLATFORM_INSIDE * tmask.sum(), ncc, -np.inf)
        y, x = np.unravel_index(int(np.argmax(ncc)), ncc.shape)
        score = float(ncc[y, x])
        if not np.isfinite(score):  # nothing of it in view
            return None
        return (wx + x + tw / 2) * ds, (wy + y + th / 2) * ds, score

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
            found = self._locate_platform(to_gray_downsampled(frame, cal.ds))
            px, py, ps = found or (None, None, 0.0)
            if found and (ps >= self.platform_threshold or self._platform_pos is None):
                self._platform_pos = (px, py)
            elif self._platform_pos is None:
                self._platform_pos = _center(cal.platform_box)
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
