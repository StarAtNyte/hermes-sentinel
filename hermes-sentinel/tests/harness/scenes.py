"""Deterministic synthetic scenes for testing the change gate and judge without cameras.

Every scenario is generated in memory with numpy from a seed. The same
``(scenario, seed, kwargs)`` always yields byte-identical frames, so tests can
assert exact behaviour in CI.

Each frame is built in two layers:

1. *Content*: what is actually in front of the camera (background, a mug,
   text on a screen). Ground-truth labels come from diffs of this layer only.
2. *Nuisance*: things a good gate must ignore (sensor noise, lighting flicker,
   small or sub-pixel camera shake and rotation, slow exposure drift, codec block
   artifacts, low-light noise, an IR night-mode switch, moving shadows and
   headlight sweeps). Applied after the labels are computed, so it never
   produces a "real change". Combined scenes (``COMBINED_SCENARIOS``) put a real
   event on top of a nuisance.

Frames are ``HxWx3`` ``uint8`` arrays in BGR order (OpenCV convention), which
is what real sources will hand to the gate.

Usage::

    scene = build_scene("object_appears", seed=7)
    scene.frames[scene.first_change_index]   # the frame where the mug shows up
    scene.labels[i].changed, scene.labels[i].bbox
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

BBox = tuple[int, int, int, int]  # (x, y, w, h) in pixels, frame coordinates

CAMERA_SIZE = (160, 120)  # (width, height)
SCREEN_SIZE = (320, 180)
DEFAULT_FPS = 10.0
SENSOR_NOISE_SIGMA = 2.0

MUG_COLOR = (40, 40, 200)  # BGR red
MUG_HANDLE_COLOR = (230, 230, 230)


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class FrameLabel:
    """Ground truth for one frame.

    ``changed`` is True when the *content* differs from the previous frame (the
    onset of a real event). ``bbox`` is the tight box around the changed pixels
    (None when unchanged). ``present`` is the bbox of the tracked object or text
    field when it is visible, whether or not it changed on this frame.
    """

    index: int
    changed: bool
    bbox: BBox | None = None
    present: BBox | None = None
    note: str = ""


@dataclass
class Scene:
    """A generated frame sequence plus its ground truth."""

    name: str
    seed: int
    fps: float
    frames: list[np.ndarray]
    labels: list[FrameLabel]
    should_trigger: bool
    description: str
    disconnect_after: int | None = None
    meta: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.frames)

    @property
    def size(self) -> tuple[int, int]:
        h, w = self.frames[0].shape[:2]
        return (w, h)

    @property
    def change_indices(self) -> list[int]:
        return [lab.index for lab in self.labels if lab.changed]

    @property
    def first_change_index(self) -> int | None:
        idx = self.change_indices
        return idx[0] if idx else None


# ------------------------------------------------------------------ primitives


def _background(rng: np.random.Generator, width: int, height: int) -> np.ndarray:
    """A plausible indoor scene: gradient wall, a table, a few objects."""
    ys = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
    xs = np.linspace(0.0, 1.0, width, dtype=np.float32)[None, :]
    base = rng.uniform(90, 140, size=3).astype(np.float32)
    img = np.empty((height, width, 3), dtype=np.float32)
    for c in range(3):
        img[..., c] = base[c] + 30.0 * ys + 15.0 * xs
    # Table top across the lower third.
    table_y = int(height * 0.62)
    img[table_y:, :] = rng.uniform(50, 90, size=3)
    img[table_y : table_y + 2, :] = 30.0
    # A few static props (books, a lamp base) so the scene is not flat.
    for _ in range(4):
        w = int(rng.integers(width // 12, width // 5))
        h = int(rng.integers(height // 10, height // 4))
        x = int(rng.integers(0, width - w))
        y = int(rng.integers(0, max(1, table_y - h)))
        img[y : y + h, x : x + w] = rng.uniform(20, 235, size=3)
    # Low-amplitude fixed texture (same on every frame).
    img += rng.normal(0.0, 3.0, size=img.shape).astype(np.float32)
    return img


def mug_bbox(width: int, height: int) -> BBox:
    """Where the mug sits on the table (body + handle)."""
    body_w = max(8, width // 10)
    body_h = max(10, height // 6)
    handle_r = max(3, body_h // 3)
    x = int(width * 0.55)
    y = int(height * 0.62) - body_h
    return (x, y, body_w + handle_r, body_h)


def _draw_mug(img: np.ndarray) -> BBox:
    height, width = img.shape[:2]
    x, y, w, h = mug_bbox(width, height)
    handle_r = max(3, h // 3)
    body_w = w - handle_r
    img[y : y + h, x : x + body_w] = MUG_COLOR
    # Handle: annulus centred on the right edge of the body, right half only.
    cy, cx = y + h // 2, x + body_w
    yy, xx = np.mgrid[y : y + h, x + body_w : x + w]
    d2 = (yy - cy) ** 2 + (xx - cx + 0.5) ** 2
    ring = (d2 <= handle_r**2) & (d2 >= (handle_r - 2) ** 2)
    region = img[y : y + h, x + body_w : x + w]
    region[ring] = MUG_HANDLE_COLOR
    return (x, y, w, h)


# 5x7 bitmap font: deterministic everywhere (no OpenCV/freetype version drift).
_GLYPHS: dict[str, tuple[str, ...]] = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    ":": ("00000", "01100", "01100", "00000", "01100", "01100", "00000"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    " ": ("00000",) * 7,
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "a": ("00000", "00000", "01110", "00001", "01111", "10001", "01111"),
    "g": ("00000", "01111", "10001", "10001", "01111", "00001", "01110"),
    "i": ("00100", "00000", "01100", "00100", "00100", "00100", "01110"),
    "l": ("01100", "00100", "00100", "00100", "00100", "00100", "01110"),
    "n": ("00000", "00000", "10110", "11001", "10001", "10001", "10001"),
    "o": ("00000", "00000", "01110", "10001", "10001", "10001", "01110"),
    "r": ("00000", "00000", "10110", "11001", "10000", "10000", "10000"),
    "s": ("00000", "00000", "01111", "10000", "01110", "00001", "11110"),
}
GLYPH_W, GLYPH_H, GLYPH_GAP = 5, 7, 1


def text_extent(text: str, scale: int) -> tuple[int, int]:
    """(width, height) in pixels of ``text`` rendered with ``draw_text``."""
    return (len(text) * (GLYPH_W + GLYPH_GAP) * scale, GLYPH_H * scale)


def draw_text(
    img: np.ndarray, text: str, x: int, y: int, scale: int, color: tuple[int, int, int]
) -> BBox:
    """Render ``text`` with the bitmap font; returns the cell bbox."""
    cell = (GLYPH_W + GLYPH_GAP) * scale
    for i, ch in enumerate(text):
        rows = _GLYPHS[ch]
        bitmap = np.array([[c == "1" for c in row] for row in rows], dtype=bool)
        mask = np.kron(bitmap, np.ones((scale, scale), dtype=bool))
        x0 = x + i * cell
        region = img[y : y + mask.shape[0], x0 : x0 + mask.shape[1]]
        region[mask] = color
    w, h = text_extent(text, scale)
    return (x, y, w, h)


def diff_bbox(a: np.ndarray, b: np.ndarray, atol: float = 0.0) -> BBox | None:
    """Tight bbox of pixels where ``a`` and ``b`` differ by more than ``atol``."""
    d = np.abs(a.astype(np.float32) - b.astype(np.float32))
    if d.ndim == 3:
        d = d.max(axis=2)
    ys, xs = np.nonzero(d > atol)
    if ys.size == 0:
        return None
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    return (x0, y0, x1 - x0 + 1, y1 - y0 + 1)


def _to_uint8(img: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(img), 0, 255).astype(np.uint8)


def _shift(img: np.ndarray, dx: int, dy: int) -> np.ndarray:
    """Translate by (dx, dy) pixels, replicating edges (small camera shake)."""
    if dx == 0 and dy == 0:
        return img
    m = max(abs(dx), abs(dy))
    padded = np.pad(img, ((m, m), (m, m), (0, 0)), mode="edge")
    h, w = img.shape[:2]
    return padded[m - dy : m - dy + h, m - dx : m - dx + w]


# --------------------------------------------------------------- assembly


Nuisance = Callable[[int, np.ndarray, np.random.Generator], np.ndarray]


def _assemble(
    *,
    name: str,
    seed: int,
    fps: float,
    content: list[np.ndarray],
    present: list[BBox | None],
    rng: np.random.Generator,
    should_trigger: bool,
    description: str,
    nuisance: Nuisance | None = None,
    noise_sigma: float = SENSOR_NOISE_SIGMA,
    notes: dict[int, str] | None = None,
    disconnect_after: int | None = None,
    meta: dict | None = None,
) -> Scene:
    """Label from content diffs, then apply nuisance + sensor noise."""
    notes = notes or {}
    labels: list[FrameLabel] = []
    frames: list[np.ndarray] = []
    prev_u8: np.ndarray | None = None
    for i, img in enumerate(content):
        cur_u8 = _to_uint8(img)
        bbox = None if prev_u8 is None else diff_bbox(prev_u8, cur_u8)
        labels.append(
            FrameLabel(
                index=i,
                changed=bbox is not None,
                bbox=bbox,
                present=present[i],
                note=notes.get(i, ""),
            )
        )
        prev_u8 = cur_u8

        out = img.copy()
        if nuisance is not None:
            out = nuisance(i, out, rng)
        if noise_sigma > 0:
            out = out + rng.normal(0.0, noise_sigma, size=out.shape).astype(np.float32)
        frames.append(_to_uint8(out))
    return Scene(
        name=name,
        seed=seed,
        fps=fps,
        frames=frames,
        labels=labels,
        should_trigger=should_trigger,
        description=description,
        disconnect_after=disconnect_after,
        meta=meta or {},
    )


# --------------------------------------------------------------- scenarios


def static_scene(
    seed: int = 0, n_frames: int = 30, size: tuple[int, int] = CAMERA_SIZE, fps: float = DEFAULT_FPS
) -> Scene:
    rng = np.random.default_rng(seed)
    bg = _background(rng, *size)
    return _assemble(
        name="static_scene",
        seed=seed,
        fps=fps,
        content=[bg] * n_frames,
        present=[None] * n_frames,
        rng=rng,
        should_trigger=False,
        description="Nothing changes; only sensor noise.",
    )


def object_appears(
    seed: int = 0,
    n_frames: int = 30,
    appear_at: int = 10,
    size: tuple[int, int] = CAMERA_SIZE,
    fps: float = DEFAULT_FPS,
) -> Scene:
    if not 0 < appear_at < n_frames:
        raise ValueError("appear_at must be inside the sequence (and not frame 0)")
    rng = np.random.default_rng(seed)
    bg = _background(rng, *size)
    with_mug = bg.copy()
    box = _draw_mug(with_mug)
    content = [bg if i < appear_at else with_mug for i in range(n_frames)]
    present = [None if i < appear_at else box for i in range(n_frames)]
    return _assemble(
        name="object_appears",
        seed=seed,
        fps=fps,
        content=content,
        present=present,
        rng=rng,
        should_trigger=True,
        description=f"A red mug appears on the table at frame {appear_at}.",
        notes={appear_at: "mug appears"},
        meta={"event_frame": appear_at, "object": "mug", "object_bbox": box},
    )


def object_removed(
    seed: int = 0,
    n_frames: int = 30,
    remove_at: int = 10,
    size: tuple[int, int] = CAMERA_SIZE,
    fps: float = DEFAULT_FPS,
) -> Scene:
    if not 0 < remove_at < n_frames:
        raise ValueError("remove_at must be inside the sequence (and not frame 0)")
    rng = np.random.default_rng(seed)
    bg = _background(rng, *size)
    with_mug = bg.copy()
    box = _draw_mug(with_mug)
    content = [with_mug if i < remove_at else bg for i in range(n_frames)]
    present = [box if i < remove_at else None for i in range(n_frames)]
    return _assemble(
        name="object_removed",
        seed=seed,
        fps=fps,
        content=content,
        present=present,
        rng=rng,
        should_trigger=True,
        description=f"The red mug is taken off the table at frame {remove_at}.",
        notes={remove_at: "mug removed"},
        meta={"event_frame": remove_at, "object": "mug", "object_bbox": box},
    )


def lighting_flicker(
    seed: int = 0,
    n_frames: int = 30,
    amplitude: float = 0.15,
    size: tuple[int, int] = CAMERA_SIZE,
    fps: float = DEFAULT_FPS,
) -> Scene:
    """Global brightness gain jumps each frame by up to +-amplitude. Must NOT trigger."""
    rng = np.random.default_rng(seed)
    bg = _background(rng, *size)
    gains = 1.0 + rng.uniform(-amplitude, amplitude, size=n_frames)

    def nuisance(i: int, img: np.ndarray, _rng: np.random.Generator) -> np.ndarray:
        return img * np.float32(gains[i])

    return _assemble(
        name="lighting_flicker",
        seed=seed,
        fps=fps,
        content=[bg] * n_frames,
        present=[None] * n_frames,
        rng=rng,
        nuisance=nuisance,
        should_trigger=False,
        description="Lights flicker (global brightness noise); nothing moves.",
        meta={"gains": gains.tolist()},
    )


def camera_shake(
    seed: int = 0,
    n_frames: int = 30,
    max_shift: int = 2,
    size: tuple[int, int] = CAMERA_SIZE,
    fps: float = DEFAULT_FPS,
) -> Scene:
    """Whole frame jitters by up to max_shift px. Must NOT trigger."""
    rng = np.random.default_rng(seed)
    bg = _background(rng, *size)
    shifts = rng.integers(-max_shift, max_shift + 1, size=(n_frames, 2))

    def nuisance(i: int, img: np.ndarray, _rng: np.random.Generator) -> np.ndarray:
        return _shift(img, int(shifts[i, 0]), int(shifts[i, 1]))

    return _assemble(
        name="camera_shake",
        seed=seed,
        fps=fps,
        content=[bg] * n_frames,
        present=[None] * n_frames,
        rng=rng,
        nuisance=nuisance,
        should_trigger=False,
        description=f"Camera shakes by up to {max_shift}px; scene content is static.",
        meta={"shifts": shifts.tolist()},
    )


def slow_drift(
    seed: int = 0,
    n_frames: int = 60,
    total_delta: float = 40.0,
    size: tuple[int, int] = CAMERA_SIZE,
    fps: float = DEFAULT_FPS,
) -> Scene:
    """Exposure brightens linearly by total_delta over the clip. Must NOT trigger."""
    rng = np.random.default_rng(seed)
    bg = _background(rng, *size)
    step = total_delta / max(1, n_frames - 1)

    def nuisance(i: int, img: np.ndarray, _rng: np.random.Generator) -> np.ndarray:
        return img + np.float32(step * i)

    return _assemble(
        name="slow_drift",
        seed=seed,
        fps=fps,
        content=[bg] * n_frames,
        present=[None] * n_frames,
        rng=rng,
        nuisance=nuisance,
        should_trigger=False,
        description=f"Brightness drifts slowly (+{total_delta:g} levels overall).",
        meta={"step_per_frame": step},
    )


SCREEN_PREFIX = "Training loss: "


def screen_text_change(
    seed: int = 0,
    n_frames: int = 30,
    change_every: int = 10,
    size: tuple[int, int] = SCREEN_SIZE,
    scale: int = 2,
    fps: float = DEFAULT_FPS,
) -> Scene:
    """A terminal-like screen showing "Training loss: x" where x updates periodically.

    Screens have no sensor noise, so ``noise_sigma`` is 0. Changes happen at
    frames ``change_every, 2*change_every, ...``.
    """
    rng = np.random.default_rng(seed)
    width, height = size
    full_w, full_h = text_extent(SCREEN_PREFIX + "0.000", scale)
    if full_w + 8 > width or full_h + 8 > height:
        raise ValueError("screen too small for the text at this scale")
    screen = np.empty((height, width, 3), dtype=np.float32)
    screen[...] = (30, 24, 20)  # dark terminal background (BGR)
    screen[: height // 8] = (70, 60, 55)  # title bar
    x0 = 8
    y0 = height // 2 - full_h // 2
    prefix_w, _ = text_extent(SCREEN_PREFIX, scale)

    n_values = (n_frames - 1) // change_every + 1
    loss = 2.0 + rng.uniform(0.0, 0.5)
    values: list[str] = []
    for _ in range(n_values):
        values.append(f"{loss:.3f}")
        loss = max(0.001, loss * rng.uniform(0.6, 0.9))

    content: list[np.ndarray] = []
    present: list[BBox | None] = []
    notes: dict[int, str] = {}
    shown: list[str] = []
    for i in range(n_frames):
        v = values[i // change_every]
        img = screen.copy()
        draw_text(img, SCREEN_PREFIX, x0, y0, scale, (200, 200, 200))
        field_box = draw_text(img, v, x0 + prefix_w, y0, scale, (120, 255, 120))
        content.append(img)
        present.append(field_box)
        shown.append(v)
        if i > 0 and shown[i - 1] != v:
            notes[i] = f"loss {shown[i - 1]} -> {v}"

    return _assemble(
        name="screen_text_change",
        seed=seed,
        fps=fps,
        content=content,
        present=present,
        rng=rng,
        noise_sigma=0.0,
        should_trigger=True,
        description=f'A screen shows "Training loss: x"; x changes every {change_every} frames.',
        notes=notes,
        meta={"values": shown, "prefix": SCREEN_PREFIX, "change_every": change_every},
    )


def disconnect(
    seed: int = 0,
    n_frames: int = 12,
    size: tuple[int, int] = CAMERA_SIZE,
    fps: float = DEFAULT_FPS,
) -> Scene:
    """A static camera that drops after ``n_frames``; FakeSource raises on the next read."""
    scene = static_scene(seed=seed, n_frames=n_frames, size=size, fps=fps)
    scene.name = "disconnect"
    scene.disconnect_after = n_frames
    scene.should_trigger = False
    scene.description = f"Camera delivers {n_frames} frames, then disconnects."
    return scene


# ------------------------------------------------- photometric / codec helpers

# Standard JPEG quantisation tables (ITU-T T.81 Annex K), luma and chroma.
_JPEG_LUMA_Q = np.array(
    [
        [16, 11, 10, 16, 24, 40, 51, 61],
        [12, 12, 14, 19, 26, 58, 60, 55],
        [14, 13, 16, 24, 40, 57, 69, 56],
        [14, 17, 22, 29, 51, 87, 80, 62],
        [18, 22, 37, 56, 68, 109, 103, 77],
        [24, 35, 55, 64, 81, 104, 113, 92],
        [49, 64, 78, 87, 103, 121, 120, 101],
        [72, 92, 95, 98, 112, 100, 103, 99],
    ],
    dtype=np.float64,
)
_JPEG_CHROMA_Q = np.full((8, 8), 99.0)
_JPEG_CHROMA_Q[:4, :4] = [[17, 18, 24, 47], [18, 21, 26, 66], [24, 26, 56, 99], [47, 66, 99, 99]]


def _dct_matrix(n: int = 8) -> np.ndarray:
    """Orthonormal DCT-II basis (rows = frequencies)."""
    k = np.arange(n)[:, None]
    i = np.arange(n)[None, :]
    m = np.cos(np.pi * (2 * i + 1) * k / (2 * n)) * np.sqrt(2.0 / n)
    m[0, :] = np.sqrt(1.0 / n)
    return m


_DCT8 = _dct_matrix(8)


def _quality_table(base: np.ndarray, quality: int) -> np.ndarray:
    """libjpeg quality scaling of a base table (quality 1..100)."""
    q = int(np.clip(quality, 1, 100))
    scale = 5000 / q if q < 50 else 200 - 2 * q
    return np.clip(np.floor((base * scale + 50) / 100), 1, 255)


def jpeg_like(img: np.ndarray, quality: int) -> np.ndarray:
    """Lossy JPEG-style round trip in pure numpy: BGR -> YCbCr, 8x8 block DCT, quantise with the
    standard tables at ``quality``, dequantise, inverse DCT, back to BGR. Returns float32 (not
    clipped) so later nuisance/noise layers compose. Deterministic; no codec library involved."""
    src = np.clip(img.astype(np.float64), 0, 255)
    h, w = src.shape[:2]
    ph, pw = (-h) % 8, (-w) % 8
    src = np.pad(src, ((0, ph), (0, pw), (0, 0)), mode="edge")
    b, g, r = src[..., 0], src[..., 1], src[..., 2]
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b
    out_planes = []
    for plane, base in ((y, _JPEG_LUMA_Q), (cb, _JPEG_CHROMA_Q), (cr, _JPEG_CHROMA_Q)):
        table = _quality_table(base, quality)
        hh, ww = plane.shape
        blocks = (plane - 128.0).reshape(hh // 8, 8, ww // 8, 8).transpose(0, 2, 1, 3)
        coeffs = _DCT8 @ blocks @ _DCT8.T
        coeffs = np.round(coeffs / table) * table
        rec = _DCT8.T @ coeffs @ _DCT8
        out_planes.append(rec.transpose(0, 2, 1, 3).reshape(hh, ww) + 128.0)
    y, cb, cr = out_planes
    r = y + 1.402 * (cr - 128)
    g = y - 0.344136 * (cb - 128) - 0.714136 * (cr - 128)
    b = y + 1.772 * (cb - 128)
    out = np.stack([b, g, r], axis=-1)[:h, :w]
    return out.astype(np.float32)


def _gaussian_blob(
    width: int, height: int, cx: float, cy: float, rx: float, ry: float
) -> np.ndarray:
    """Soft elliptical mask in [0, 1] (1 at the centre), float32, shape (h, w)."""
    ys = np.arange(height, dtype=np.float32)[:, None]
    xs = np.arange(width, dtype=np.float32)[None, :]
    d2 = ((xs - cx) / max(rx, 1e-3)) ** 2 + ((ys - cy) / max(ry, 1e-3)) ** 2
    return np.exp(-0.5 * d2).astype(np.float32)


def _mask_bbox(mask: np.ndarray, threshold: float) -> BBox | None:
    ys, xs = np.nonzero(mask > threshold)
    if ys.size == 0:
        return None
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    return (x0, y0, x1 - x0 + 1, y1 - y0 + 1)


def warp_affine(img: np.ndarray, dx: float, dy: float, angle_deg: float) -> np.ndarray:
    """Rotate by ``angle_deg`` about the frame centre then translate by (dx, dy) sub-pixel
    amounts; bilinear sampling with edge replication. Pure numpy, deterministic."""
    h, w = img.shape[:2]
    theta = np.deg2rad(angle_deg)
    cos_t, sin_t = np.cos(theta), np.sin(theta)
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float64)
    # Inverse map: output pixel -> source pixel.
    ox, oy = xs - cx - dx, ys - cy - dy
    sx = cos_t * ox + sin_t * oy + cx
    sy = -sin_t * ox + cos_t * oy + cy
    sx = np.clip(sx, 0, w - 1)
    sy = np.clip(sy, 0, h - 1)
    x0 = np.floor(sx).astype(np.intp)
    y0 = np.floor(sy).astype(np.intp)
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    fx = (sx - x0)[..., None]
    fy = (sy - y0)[..., None]
    src = img.astype(np.float64)
    top = src[y0, x0] * (1 - fx) + src[y0, x1] * fx
    bottom = src[y1, x0] * (1 - fx) + src[y1, x1] * fx
    return (top * (1 - fy) + bottom * fy).astype(np.float32)


def _sweep_centres(n_frames: int, start: float, end: float) -> np.ndarray:
    return np.linspace(start, end, n_frames, dtype=np.float64)


# ------------------------------------------------------ nuisance scenarios (2)


def compression_artifacts(
    seed: int = 0,
    n_frames: int = 30,
    quality_range: tuple[int, int] = (15, 45),
    size: tuple[int, int] = CAMERA_SIZE,
    fps: float = DEFAULT_FPS,
) -> Scene:
    """Low-bitrate stream: every frame is re-encoded (8x8 block DCT quantisation) at a quality
    that varies frame to frame, so block artifacts shimmer. Must NOT trigger."""
    lo, hi = quality_range
    if not 1 <= lo <= hi <= 100:
        raise ValueError("quality_range must satisfy 1 <= lo <= hi <= 100")
    rng = np.random.default_rng(seed)
    bg = _background(rng, *size)
    qualities = rng.integers(lo, hi + 1, size=n_frames)

    def nuisance(i: int, img: np.ndarray, frame_rng: np.random.Generator) -> np.ndarray:
        # Sensor noise happens before the encoder; the codec then quantises it.
        noisy = img + frame_rng.normal(0.0, SENSOR_NOISE_SIGMA, size=img.shape).astype(np.float32)
        return jpeg_like(noisy, int(qualities[i]))

    return _assemble(
        name="compression_artifacts",
        seed=seed,
        fps=fps,
        content=[bg] * n_frames,
        present=[None] * n_frames,
        rng=rng,
        nuisance=nuisance,
        noise_sigma=0.0,
        should_trigger=False,
        description="Static scene through a low-quality JPEG-like codec; block artifacts vary.",
        meta={"qualities": qualities.tolist(), "block": 8},
    )


def heavy_low_light_noise(
    seed: int = 0,
    n_frames: int = 30,
    gain: float = 0.22,
    noise_sigma: float = 11.0,
    size: tuple[int, int] = CAMERA_SIZE,
    fps: float = DEFAULT_FPS,
) -> Scene:
    """Dark room, sensor gain cranked up: dim image buried in heavy (shot + read) noise.
    Must NOT trigger."""
    if not 0 < gain <= 1:
        raise ValueError("gain must be in (0, 1]")
    rng = np.random.default_rng(seed)
    bg = _background(rng, *size)

    def nuisance(i: int, img: np.ndarray, frame_rng: np.random.Generator) -> np.ndarray:
        dim = img * np.float32(gain)
        # Shot noise grows with signal (variance ~ level); read noise added by _assemble.
        shot = frame_rng.normal(0.0, 1.0, size=dim.shape).astype(np.float32)
        return dim + shot * np.sqrt(np.maximum(dim, 0.0)) * np.float32(0.8)

    return _assemble(
        name="heavy_low_light_noise",
        seed=seed,
        fps=fps,
        content=[bg] * n_frames,
        present=[None] * n_frames,
        rng=rng,
        nuisance=nuisance,
        noise_sigma=noise_sigma,
        should_trigger=False,
        description=f"Low light (gain {gain:g}) with heavy sensor noise (sigma {noise_sigma:g}).",
        meta={"gain": gain, "noise_sigma": noise_sigma},
    )


def ir_night_switch(
    seed: int = 0,
    n_frames: int = 30,
    switch_at: int = 12,
    size: tuple[int, int] = CAMERA_SIZE,
    fps: float = DEFAULT_FPS,
) -> Scene:
    """The camera flips to IR night mode at ``switch_at``: colour disappears, the image turns
    grey with a different brightness/contrast curve. Nothing in the scene changed. Must NOT
    trigger (a gate should re-baseline instead)."""
    if not 0 < switch_at < n_frames:
        raise ValueError("switch_at must be inside the sequence (and not frame 0)")
    rng = np.random.default_rng(seed)
    bg = _background(rng, *size)

    def nuisance(i: int, img: np.ndarray, _rng: np.random.Generator) -> np.ndarray:
        if i < switch_at:
            return img
        # IR response: roughly luminance, lifted and contrast-compressed by the IR illuminator.
        gray = 0.299 * img[..., 2] + 0.587 * img[..., 1] + 0.114 * img[..., 0]
        ir = 40.0 + 0.75 * gray
        return np.repeat(ir[..., None], 3, axis=2).astype(np.float32)

    return _assemble(
        name="ir_night_switch",
        seed=seed,
        fps=fps,
        content=[bg] * n_frames,
        present=[None] * n_frames,
        rng=rng,
        nuisance=nuisance,
        should_trigger=False,
        description=f"Camera switches to IR night mode (greyscale) at frame {switch_at}.",
        notes={switch_at: "IR mode on"},
        meta={"switch_frame": switch_at},
    )


def local_shadow_pass(
    seed: int = 0,
    n_frames: int = 30,
    darkness: float = 0.55,
    size: tuple[int, int] = CAMERA_SIZE,
    fps: float = DEFAULT_FPS,
) -> Scene:
    """A soft shadow (someone walking past a window off-camera) slides across the scene.
    Local lighting change only. Must NOT trigger."""
    if not 0 < darkness < 1:
        raise ValueError("darkness must be in (0, 1)")
    rng = np.random.default_rng(seed)
    width, height = size
    bg = _background(rng, width, height)
    centres = _sweep_centres(n_frames, -0.2 * width, 1.2 * width)
    cy = height * float(rng.uniform(0.35, 0.65))
    rx, ry = width * 0.12, height * 0.45
    boxes: list[BBox | None] = []

    def nuisance(i: int, img: np.ndarray, _rng: np.random.Generator) -> np.ndarray:
        blob = _gaussian_blob(width, height, float(centres[i]), cy, rx, ry)
        return img * (1.0 - np.float32(darkness) * blob)[..., None]

    for i in range(n_frames):
        blob = _gaussian_blob(width, height, float(centres[i]), cy, rx, ry)
        boxes.append(_mask_bbox(blob * darkness, 0.05))

    return _assemble(
        name="local_shadow_pass",
        seed=seed,
        fps=fps,
        content=[bg] * n_frames,
        present=[None] * n_frames,
        rng=rng,
        nuisance=nuisance,
        should_trigger=False,
        description="A soft moving shadow passes across the scene; nothing physical changes.",
        meta={"shadow_bboxes": boxes, "darkness": darkness},
    )


def headlight_sweep(
    seed: int = 0,
    n_frames: int = 30,
    intensity: float = 110.0,
    size: tuple[int, int] = CAMERA_SIZE,
    fps: float = DEFAULT_FPS,
) -> Scene:
    """Car headlights sweep across the wall at night: a bright moving cone over a dim scene.
    Local lighting change only. Must NOT trigger."""
    rng = np.random.default_rng(seed)
    width, height = size
    bg = _background(rng, width, height) * np.float32(0.45)  # night
    centres = _sweep_centres(n_frames, 1.3 * width, -0.3 * width)  # right to left
    cy = height * float(rng.uniform(0.25, 0.45))
    rx, ry = width * 0.18, height * 0.3
    boxes: list[BBox | None] = []

    def nuisance(i: int, img: np.ndarray, _rng: np.random.Generator) -> np.ndarray:
        blob = _gaussian_blob(width, height, float(centres[i]), cy, rx, ry)
        # Light adds energy and slightly warms the colour (more R/G than B).
        tint = np.array([0.85, 1.0, 1.0], dtype=np.float32)
        return img + np.float32(intensity) * blob[..., None] * tint

    for i in range(n_frames):
        blob = _gaussian_blob(width, height, float(centres[i]), cy, rx, ry)
        boxes.append(_mask_bbox(blob * intensity, 5.0))

    return _assemble(
        name="headlight_sweep",
        seed=seed,
        fps=fps,
        content=[bg] * n_frames,
        present=[None] * n_frames,
        rng=rng,
        nuisance=nuisance,
        should_trigger=False,
        description="Headlights sweep across a dim room; nothing physical changes.",
        meta={"light_bboxes": boxes, "intensity": intensity},
    )


def subpixel_shake_rotation(
    seed: int = 0,
    n_frames: int = 30,
    max_shift: float = 0.8,
    max_angle_deg: float = 0.6,
    size: tuple[int, int] = CAMERA_SIZE,
    fps: float = DEFAULT_FPS,
) -> Scene:
    """Wind-buffeted mount: sub-pixel translation plus a small rotation every frame (bilinear
    resampling softens edges differently each frame). Must NOT trigger."""
    rng = np.random.default_rng(seed)
    bg = _background(rng, *size)
    shifts = rng.uniform(-max_shift, max_shift, size=(n_frames, 2))
    angles = rng.uniform(-max_angle_deg, max_angle_deg, size=n_frames)

    def nuisance(i: int, img: np.ndarray, _rng: np.random.Generator) -> np.ndarray:
        return warp_affine(img, float(shifts[i, 0]), float(shifts[i, 1]), float(angles[i]))

    return _assemble(
        name="subpixel_shake_rotation",
        seed=seed,
        fps=fps,
        content=[bg] * n_frames,
        present=[None] * n_frames,
        rng=rng,
        nuisance=nuisance,
        should_trigger=False,
        description=(
            f"Sub-pixel shake (<= {max_shift:g}px) and rotation (<= {max_angle_deg:g} deg)."
        ),
        meta={"shifts": shifts.tolist(), "angles_deg": angles.tolist()},
    )


# ------------------------------------------------- nuisance + real event (combined)


def _mug_content(
    bg: np.ndarray, n_frames: int, event_at: int, appears: bool
) -> tuple[list[np.ndarray], list[BBox | None], BBox]:
    with_mug = bg.copy()
    box = _draw_mug(with_mug)
    before, after = (bg, with_mug) if appears else (with_mug, bg)
    content = [before if i < event_at else after for i in range(n_frames)]
    visible = [(i >= event_at) == appears for i in range(n_frames)]
    present = [box if v else None for v in visible]
    return content, present, box


def flicker_with_mug(
    seed: int = 0,
    n_frames: int = 30,
    remove_at: int = 15,
    amplitude: float = 0.15,
    size: tuple[int, int] = CAMERA_SIZE,
    fps: float = DEFAULT_FPS,
) -> Scene:
    """Lights flicker the whole time AND the mug is taken at ``remove_at``. Must trigger once,
    on the real event only."""
    if not 0 < remove_at < n_frames:
        raise ValueError("remove_at must be inside the sequence (and not frame 0)")
    rng = np.random.default_rng(seed)
    bg = _background(rng, *size)
    content, present, box = _mug_content(bg, n_frames, remove_at, appears=False)
    gains = 1.0 + rng.uniform(-amplitude, amplitude, size=n_frames)

    def nuisance(i: int, img: np.ndarray, _rng: np.random.Generator) -> np.ndarray:
        return img * np.float32(gains[i])

    return _assemble(
        name="flicker_with_mug",
        seed=seed,
        fps=fps,
        content=content,
        present=present,
        rng=rng,
        nuisance=nuisance,
        should_trigger=True,
        description=f"Lighting flickers throughout; the mug is taken at frame {remove_at}.",
        notes={remove_at: "mug removed (during flicker)"},
        meta={"event_frame": remove_at, "object": "mug", "object_bbox": box,
              "gains": gains.tolist(), "nuisance": "lighting_flicker"},
    )


def shake_with_mug(
    seed: int = 0,
    n_frames: int = 30,
    appear_at: int = 15,
    max_shift: int = 2,
    size: tuple[int, int] = CAMERA_SIZE,
    fps: float = DEFAULT_FPS,
) -> Scene:
    """The camera shakes the whole time AND a mug appears at ``appear_at``. Must trigger once.
    Labels are in content coordinates; the rendered mug may sit up to ``max_shift`` px off."""
    if not 0 < appear_at < n_frames:
        raise ValueError("appear_at must be inside the sequence (and not frame 0)")
    rng = np.random.default_rng(seed)
    bg = _background(rng, *size)
    content, present, box = _mug_content(bg, n_frames, appear_at, appears=True)
    shifts = rng.integers(-max_shift, max_shift + 1, size=(n_frames, 2))

    def nuisance(i: int, img: np.ndarray, _rng: np.random.Generator) -> np.ndarray:
        return _shift(img, int(shifts[i, 0]), int(shifts[i, 1]))

    return _assemble(
        name="shake_with_mug",
        seed=seed,
        fps=fps,
        content=content,
        present=present,
        rng=rng,
        nuisance=nuisance,
        should_trigger=True,
        description=f"Camera shakes throughout; a mug appears at frame {appear_at}.",
        notes={appear_at: "mug appears (during shake)"},
        meta={"event_frame": appear_at, "object": "mug", "object_bbox": box,
              "shifts": shifts.tolist(), "max_shift": max_shift, "nuisance": "camera_shake"},
    )


SCENARIOS: dict[str, Callable[..., Scene]] = {
    "static_scene": static_scene,
    "object_appears": object_appears,
    "object_removed": object_removed,
    "lighting_flicker": lighting_flicker,
    "camera_shake": camera_shake,
    "slow_drift": slow_drift,
    "screen_text_change": screen_text_change,
    "disconnect": disconnect,
    "compression_artifacts": compression_artifacts,
    "heavy_low_light_noise": heavy_low_light_noise,
    "ir_night_switch": ir_night_switch,
    "local_shadow_pass": local_shadow_pass,
    "headlight_sweep": headlight_sweep,
    "subpixel_shake_rotation": subpixel_shake_rotation,
    "flicker_with_mug": flicker_with_mug,
    "shake_with_mug": shake_with_mug,
}

# Scenes whose frames change but whose content never does: a good gate stays quiet.
NUISANCE_SCENARIOS = (
    "static_scene",
    "lighting_flicker",
    "camera_shake",
    "slow_drift",
    "compression_artifacts",
    "heavy_low_light_noise",
    "ir_night_switch",
    "local_shadow_pass",
    "headlight_sweep",
    "subpixel_shake_rotation",
)
# Scenes with a real content change (``should_trigger``), labelled per frame.
EVENT_SCENARIOS = (
    "object_appears",
    "object_removed",
    "screen_text_change",
    "flicker_with_mug",
    "shake_with_mug",
)
# Event scenes that also carry a nuisance: the gate must fire on the event, not the nuisance.
COMBINED_SCENARIOS = ("flicker_with_mug", "shake_with_mug")


def build_scene(name: str, seed: int = 0, **kwargs) -> Scene:
    """Build a scenario by name. Raises KeyError for unknown names."""
    try:
        factory = SCENARIOS[name]
    except KeyError:
        raise KeyError(f"unknown scenario {name!r}; known: {sorted(SCENARIOS)}") from None
    return factory(seed=seed, **kwargs)
