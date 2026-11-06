"""Tests for sentinel.gate.ChangeGate: scene-level accuracy plus unit behaviour.

The scene sweep runs every scenario in ``harness.scenes`` over several seeds and both
backends (numpy always, OpenCV when installed) and checks:

* nuisance scenes: zero changed frames;
* event scenes: every labelled change is detected within ``DETECT_WITHIN`` frames with
  bbox IoU >= ``MIN_IOU``, and no changed frame falls outside those windows.

Per-scene precision/recall are attached to the test report (``record_property``) and a
summary table is printed by ``test_scene_summary``.
"""

from __future__ import annotations

import math
import threading

import numpy as np
import pytest
from harness.plugin_loader import plugin_module
from harness.scenes import (
    EVENT_SCENARIOS,
    NUISANCE_SCENARIOS,
    SCENARIOS,
    Scene,
    build_scene,
    object_appears,
    static_scene,
)

gate_mod = plugin_module("sentinel.gate")
ChangeGate = gate_mod.ChangeGate
GateConfig = gate_mod.GateConfig
GateResult = gate_mod.GateResult
bbox_iou = gate_mod.bbox_iou

DETECT_WITHIN = 3
MIN_IOU = 0.3
SEEDS = (0, 1, 2)
BACKENDS = ["numpy"] + (["cv2"] if gate_mod.cv2_available() else [])


# ------------------------------------------------------------------ helpers


def run_scene(scene: Scene, gate: ChangeGate | None = None, **cfg) -> list:
    gate = gate or ChangeGate(GateConfig(**cfg), backend="numpy")
    return [gate.process(f, i / scene.fps) for i, f in enumerate(scene.frames)]


def event_bbox(scene: Scene, index: int):
    # shake_with_mug labels are in content coordinates; the object bbox is the same box.
    return scene.meta.get("object_bbox") or scene.labels[index].bbox


def evaluate(scene: Scene, results: list) -> dict:
    """Precision/recall of changed frames against labelled events (onset + DETECT_WITHIN)."""
    changed = [i for i, r in enumerate(results) if r.changed]
    events = scene.change_indices
    matched_frames: set[int] = set()
    detected = 0
    ious: list[float] = []
    for ev in events:
        hits = [i for i in changed if ev <= i <= ev + DETECT_WITHIN]
        if not hits:
            continue
        best = max(bbox_iou(results[i].bbox, event_bbox(scene, ev)) for i in hits)
        ious.append(best)
        if best >= MIN_IOU:
            detected += 1
            matched_frames.update(hits)
    tp = len(matched_frames)
    fp = len(changed) - tp
    precision = tp / len(changed) if changed else 1.0
    recall = detected / len(events) if events else 1.0
    return {
        "changed": changed,
        "events": events,
        "fp": fp,
        "precision": precision,
        "recall": recall,
        "min_iou": min(ious) if ious else math.nan,
    }


_SUMMARY: dict[tuple[str, int, str], dict] = {}


# ------------------------------------------------------------- scene sweep


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scene_accuracy(name: str, seed: int, backend: str, record_property) -> None:
    scene = build_scene(name, seed=seed)
    gate = ChangeGate(backend=backend)
    results = run_scene(scene, gate)
    m = evaluate(scene, results)
    _SUMMARY[(name, seed, backend)] = m
    record_property("precision", m["precision"])
    record_property("recall", m["recall"])
    record_property("min_iou", m["min_iou"])
    if name in NUISANCE_SCENARIOS or not scene.should_trigger:
        assert m["changed"] == [], f"{name}: nuisance produced changed frames {m['changed']}"
    else:
        assert name in EVENT_SCENARIOS
        assert m["recall"] == 1.0, f"{name}: events {m['events']} changed {m['changed']} {m}"
        assert m["precision"] == 1.0, f"{name}: spurious changed frames {m}"
        assert m["min_iou"] >= MIN_IOU


def test_scene_summary(capsys) -> None:
    """Aggregate precision/recall over every scene (runs the numpy path if not yet swept)."""
    for name in sorted(SCENARIOS):
        for seed in SEEDS:
            key = (name, seed, "numpy")
            if key not in _SUMMARY:
                scene = build_scene(name, seed=seed)
                _SUMMARY[key] = evaluate(scene, run_scene(scene))
    tp = fp = n_events = detected = 0
    lines = [f"{'scene':26s} {'prec':>5s} {'recall':>6s} {'min_iou':>7s} {'fp':>3s}"]
    for name in sorted(SCENARIOS):
        rows = [_SUMMARY[(name, s, "numpy")] for s in SEEDS]
        prec = float(np.mean([r["precision"] for r in rows]))
        rec = float(np.mean([r["recall"] for r in rows]))
        ious = [r["min_iou"] for r in rows if not math.isnan(r["min_iou"])]
        f = sum(r["fp"] for r in rows)
        lines.append(
            f"{name:26s} {prec:5.2f} {rec:6.2f} {min(ious) if ious else float('nan'):7.2f} {f:3d}"
        )
        for r in rows:
            fp += r["fp"]
            tp += len(r["changed"]) - r["fp"]
            n_events += len(r["events"])
            detected += round(r["recall"] * len(r["events"]))
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = detected / n_events if n_events else 1.0
    lines.append(f"OVERALL precision={precision:.3f} recall={recall:.3f}")
    with capsys.disabled():
        print("\n" + "\n".join(lines))
    assert precision == 1.0
    assert recall == 1.0


@pytest.mark.parametrize("name", ["object_appears", "object_removed"])
def test_persistent_change_fires_once(name: str) -> None:
    scene = build_scene(name, seed=3, n_frames=40)
    changed = [i for i, r in enumerate(run_scene(scene)) if r.changed]
    assert changed == [10]


def test_event_detected_at_larger_resolution() -> None:
    """A 4x upscaled (640x480) scene is downscaled to 320 and bboxes map back to input pixels."""
    scene = object_appears(seed=1)
    big = [np.kron(f, np.ones((4, 4, 1), dtype=np.uint8)) for f in scene.frames]
    gate = ChangeGate(backend="numpy")
    results = [gate.process(f, i) for i, f in enumerate(big)]
    changed = [i for i, r in enumerate(results) if r.changed]
    assert changed == [10]
    x, y, w, h = scene.meta["object_bbox"]
    assert bbox_iou(results[10].bbox, (4 * x, 4 * y, 4 * w, 4 * h)) >= 0.4


def test_low_light_event_still_detected() -> None:
    """The noise model adapts: a mug appearing in a dark, noisy room is still caught."""
    from harness import scenes as S

    for seed in range(4):
        rng = np.random.default_rng(seed)
        bg = S._background(rng, *S.CAMERA_SIZE)
        content, present, box = S._mug_content(bg, 25, 12, appears=True)

        def nuisance(i, img, r):
            dim = img * np.float32(0.22)
            shot = r.normal(0.0, 1.0, size=dim.shape).astype(np.float32)
            return dim + shot * np.sqrt(np.maximum(dim, 0.0)) * np.float32(0.8)

        scene = S._assemble(
            name="lowlight_mug",
            seed=seed,
            fps=10.0,
            content=content,
            present=present,
            rng=rng,
            nuisance=nuisance,
            noise_sigma=11.0,
            should_trigger=True,
            description="",
            meta={"object_bbox": box},
        )
        m = evaluate(scene, run_scene(scene))
        assert m["recall"] == 1.0 and m["precision"] == 1.0, (seed, m)


# ------------------------------------------------------------- backends


@pytest.mark.skipif(not gate_mod.cv2_available(), reason="OpenCV not installed")
def test_backend_ops_parity() -> None:
    rng = np.random.default_rng(0)
    img = rng.uniform(0, 255, size=(57, 83)).astype(np.float32)
    np_ops, cv_ops = gate_mod._Ops(use_cv2=False), gate_mod._Ops(use_cv2=True)
    assert np.allclose(np_ops.box_mean(img, 7), cv_ops.box_mean(img, 7), atol=1e-2)
    assert np.allclose(np_ops.blur(img, 1.5), cv_ops.blur(img, 1.5), atol=1e-2)
    big = rng.uniform(0, 255, size=(240, 640)).astype(np.float32)
    assert np.allclose(np_ops.downscale(big, 320), cv_ops.downscale(big, 320), atol=1e-2)
    odd = rng.uniform(0, 255, size=(301, 997)).astype(np.float32)
    a, b = np_ops.downscale(odd, 320), cv_ops.downscale(odd, 320)
    assert a.shape == b.shape == (97, 320)
    assert np.abs(a - b).mean() < 1.0
    mask = rng.uniform(size=(40, 50)) > 0.6
    assert np.array_equal(np_ops.open(mask, 1), cv_ops.open(mask, 1))
    assert np.array_equal(np_ops.open(mask, 2), cv_ops.open(mask, 2))


@pytest.mark.skipif(not gate_mod.cv2_available(), reason="OpenCV not installed")
def test_backend_results_match() -> None:
    scene = build_scene("shake_with_mug", seed=1)
    a = run_scene(scene, ChangeGate(backend="numpy"))
    b = run_scene(scene, ChangeGate(backend="cv2"))
    assert [r.changed for r in a] == [r.changed for r in b]
    for ra, rb in zip(a, b, strict=True):
        if ra.changed:
            assert bbox_iou(ra.bbox, rb.bbox) > 0.8


def test_backend_selection() -> None:
    assert ChangeGate(backend="numpy").backend == "numpy"
    with pytest.raises(ValueError):
        ChangeGate(backend="gpu")  # type: ignore[arg-type]
    if gate_mod.cv2_available():
        assert ChangeGate(backend="cv2").backend == "cv2"
        assert ChangeGate().backend == "cv2"


def test_cv2_backend_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate_mod, "_cv2", None)
    with pytest.raises(RuntimeError):
        ChangeGate(backend="cv2")
    gate = ChangeGate(backend="auto")
    assert gate.backend == "numpy"
    assert not gate_mod.cv2_available()


# ------------------------------------------------------------------ ticks


def _static_frames(n: int = 5):
    return static_scene(seed=0, n_frames=n).frames


def test_tick_on_start_and_period() -> None:
    gate = ChangeGate(GateConfig(idle_check_seconds=10.0), backend="numpy")
    frames = _static_frames(1) * 30
    ticks = [t for t in range(30) if gate.process(frames[t], float(t)).tick]
    assert ticks == [0, 10, 20]
    assert gate.stats()["ticks"] == 3


def test_no_tick_on_start_when_disabled() -> None:
    gate = ChangeGate(GateConfig(idle_check_seconds=5.0, tick_on_start=False), backend="numpy")
    frame = _static_frames(1)[0]
    ticks = [t for t in range(12) if gate.process(frame, float(t)).tick]
    assert ticks == [5, 10]


def test_ticks_disabled() -> None:
    gate = ChangeGate(GateConfig(idle_check_seconds=0), backend="numpy")
    frame = _static_frames(1)[0]
    assert not any(gate.process(frame, float(t)).tick for t in range(0, 1000, 100))


def test_change_resets_idle_timer() -> None:
    scene = object_appears(seed=0, n_frames=30, appear_at=10)
    gate = ChangeGate(GateConfig(idle_check_seconds=12.0, tick_on_start=False), backend="numpy")
    results = [gate.process(f, float(i)) for i, f in enumerate(scene.frames)]
    assert results[10].changed
    ticks = [i for i, r in enumerate(results) if r.tick]
    assert ticks == [22]  # 12 s after the change at t=10, not at t=12 or t=24
    assert results[22].should_judge and not results[22].changed


def test_clock_backwards_restarts_period() -> None:
    gate = ChangeGate(GateConfig(idle_check_seconds=10.0), backend="numpy")
    frame = _static_frames(1)[0]
    assert gate.process(frame, 100.0).tick
    assert not gate.process(frame, 5.0).tick  # clock reset
    assert not gate.process(frame, 14.0).tick
    assert gate.process(frame, 15.0).tick


def test_default_clock_used_without_ts() -> None:
    now = [50.0]
    gate = ChangeGate(GateConfig(idle_check_seconds=3.0), backend="numpy", clock=lambda: now[0])
    frame = _static_frames(1)[0]
    assert gate.process(frame).tick
    now[0] = 52.0
    assert not gate.process(frame).tick
    now[0] = 53.0
    assert gate.process(frame).tick


def test_non_finite_timestamp_rejected() -> None:
    gate = ChangeGate(backend="numpy")
    with pytest.raises(ValueError):
        gate.process(_static_frames(1)[0], float("nan"))


# -------------------------------------------------------------------- ROI


def test_roi_excluding_change_stays_quiet() -> None:
    scene = object_appears(seed=2)
    # Mug is at x ~ 55-70% of the width; watch only the left 40%.
    gate = ChangeGate(roi=(0.0, 0.0, 0.4, 1.0), backend="numpy")
    assert not any(r.changed for r in run_scene(scene, gate))


def test_roi_including_change_detects_and_clips_bbox() -> None:
    scene = object_appears(seed=2)
    w, h = scene.size
    x, y, bw, bh = scene.meta["object_bbox"]
    roi = gate_mod.roi_from_pixels((x - 10, y - 10, bw + 20, bh + 20), (w, h))
    gate = ChangeGate(roi=roi, backend="numpy")
    results = run_scene(scene, gate)
    assert [i for i, r in enumerate(results) if r.changed] == [10]
    r = results[10]
    assert bbox_iou(r.bbox, scene.meta["object_bbox"]) >= MIN_IOU
    rx0, ry0 = roi[0] * w, roi[1] * h
    rx1, ry1 = rx0 + roi[2] * w, ry0 + roi[3] * h
    bx, by, bw2, bh2 = r.bbox
    assert bx >= math.floor(rx0) and by >= math.floor(ry0)
    assert bx + bw2 <= math.ceil(rx1) + 1 and by + bh2 <= math.ceil(ry1) + 1
    # Ratio is relative to the (small) ROI, so it is much larger than the full-frame ratio.
    full = run_scene(scene)[10]
    assert r.ratio > 2 * full.ratio


def test_large_roi_change_is_not_mistaken_for_global() -> None:
    """A change filling most of a small ROI must not trigger the global re-baseline."""
    scene = object_appears(seed=0)
    w, h = scene.size
    gate = ChangeGate(
        roi=gate_mod.roi_from_pixels(scene.meta["object_bbox"], (w, h)), backend="numpy"
    )
    r = run_scene(scene, gate)[10]
    assert r.changed and not r.rebaselined


def test_set_roi_runtime() -> None:
    scene = object_appears(seed=2)
    gate = ChangeGate(roi=(0.0, 0.0, 0.4, 1.0), backend="numpy")
    for i, f in enumerate(scene.frames[:10]):
        gate.process(f, float(i))
    gate.set_roi(None)
    assert gate.roi is None
    assert gate.process(scene.frames[10], 10.0).changed


@pytest.mark.parametrize(
    "roi,expected",
    [
        (None, None),
        ((0.1, 0.2, 0.3, 0.4), (0.1, 0.2, 0.3, 0.4)),
        ((-0.5, 0.5, 1.0, 1.0), (0.0, 0.5, 0.5, 0.5)),
        ([0, 0, 1, 1], (0.0, 0.0, 1.0, 1.0)),
    ],
)
def test_normalize_roi(roi, expected) -> None:
    got = gate_mod.normalize_roi(roi)
    if expected is None:
        assert got is None
    else:
        assert np.allclose(got, expected)


@pytest.mark.parametrize(
    "roi",
    [
        (0.1, 0.2, 0.3),
        (0, 0, 0, 0.5),
        (0, 0, 0.5, -1),
        (1.5, 0, 0.2, 0.2),
        ("a", 0, 1, 1),
        (float("nan"), 0, 1, 1),
        5,
    ],
)
def test_normalize_roi_rejects(roi) -> None:
    with pytest.raises(ValueError):
        gate_mod.normalize_roi(roi)


def test_roi_from_pixels() -> None:
    assert np.allclose(gate_mod.roi_from_pixels((16, 12, 80, 60), (160, 120)), (0.1, 0.1, 0.5, 0.5))
    with pytest.raises(ValueError):
        gate_mod.roi_from_pixels((0, 0, 1, 1), (0, 10))


def test_tiny_roi_still_valid() -> None:
    rows, cols = gate_mod._roi_slices((0.999, 0.999, 0.001, 0.001), 100, 50)
    assert rows.stop - rows.start >= 1 and cols.stop - cols.start >= 1


# --------------------------------------------------- baseline / robustness


def test_first_frame_is_warmup() -> None:
    gate = ChangeGate(backend="numpy")
    r = gate.process(_static_frames(1)[0], 0.0)
    assert not r.changed and r.rebaselined and r.reason == "warmup" and r.bbox is None


def _textured_view(seed: int, n: int) -> list[np.ndarray]:
    """Busy random 6x6-block texture plus sensor noise: a view with structure everywhere."""
    rng = np.random.default_rng(seed)
    blocks = rng.uniform(20, 235, size=(20, 27, 3))
    base = np.kron(blocks, np.ones((6, 6, 1)))[:120, :160]
    return [np.clip(base + rng.normal(0, 2, base.shape), 0, 255).astype(np.uint8) for _ in range(n)]


def test_scene_cut_rebaselines() -> None:
    a = _textured_view(0, 6)
    b = _textured_view(99, 6)  # camera now points at something else entirely
    gate = ChangeGate(backend="numpy")
    results = [gate.process(f, float(i)) for i, f in enumerate(a + b)]
    cut = results[6]
    assert not cut.changed and cut.rebaselined and cut.reason == "global_change"
    assert cut.ratio > gate.config.global_change_max
    assert not any(r.changed for r in results[7:])
    assert gate.stats()["rebaselines"] == 2


def test_resize_rebaselines() -> None:
    gate = ChangeGate(backend="numpy")
    small = static_scene(seed=0, n_frames=3).frames
    big = static_scene(seed=0, n_frames=3, size=(200, 150)).frames
    for i, f in enumerate(small):
        gate.process(f, float(i))
    r = gate.process(big[0], 3.0)
    assert r.rebaselined and r.reason == "resized" and not r.changed
    assert not gate.process(big[1], 4.0).changed


def test_reset_forgets_background_and_ticks_again() -> None:
    gate = ChangeGate(GateConfig(idle_check_seconds=100.0), backend="numpy")
    frame = _static_frames(1)[0]
    gate.process(frame, 0.0)
    gate.process(frame, 1.0)
    gate.reset()
    r = gate.process(frame, 2.0)
    assert r.rebaselined and r.reason == "warmup" and r.tick


def test_update_config() -> None:
    gate = ChangeGate(backend="numpy")
    frame = _static_frames(1)[0]
    gate.process(frame, 0.0)
    gate.update_config(min_area_ratio=0.01)
    assert gate.config.min_area_ratio == 0.01
    assert not gate.process(frame, 1.0).rebaselined  # non-geometry change keeps baseline
    gate.update_config(window=9)
    assert gate.process(frame, 2.0).rebaselined
    with pytest.raises(ValueError):
        gate.update_config(window=4)
    with pytest.raises(TypeError):
        gate.update_config(not_a_field=1)


def test_input_formats_accepted() -> None:
    scene = object_appears(seed=0)
    gray = [
        (0.114 * f[..., 0] + 0.587 * f[..., 1] + 0.299 * f[..., 2]).astype(np.uint8)
        for f in scene.frames
    ]
    variants = {
        "gray": gray,
        "gray1": [g[..., None] for g in gray],
        "bgra": [np.dstack([f, np.full(f.shape[:2], 255, np.uint8)]) for f in scene.frames],
        "float": [f.astype(np.float32) for f in scene.frames],
        "uint16": [f.astype(np.uint16) * 257 for f in scene.frames],
        "noncontig": [np.asfortranarray(f) for f in scene.frames],
    }
    for name, frames in variants.items():
        gate = ChangeGate(backend="numpy")
        changed = [i for i, f in enumerate(frames) if gate.process(f, float(i)).changed]
        assert changed == [10], name


def test_nan_frame_does_not_crash() -> None:
    frames = static_scene(seed=0, n_frames=4).frames
    gate = ChangeGate(backend="numpy")
    for i, f in enumerate(frames):
        fl = f.astype(np.float32)
        if i == 2:
            fl[0, 0] = np.nan
        r = gate.process(fl, float(i))
        assert math.isfinite(r.ratio)


def test_black_frames_do_not_crash() -> None:
    gate = ChangeGate(backend="numpy")
    black = np.zeros((60, 80, 3), np.uint8)
    for i in range(4):
        r = gate.process(black, float(i))
        assert not r.changed and r.ratio == 0.0


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [[1, 2], [3, 4]],
        np.zeros((0, 10, 3), np.uint8),
        np.zeros((1, 10, 3), np.uint8),
        np.zeros((10, 10, 2), np.uint8),
        np.zeros((10,), np.uint8),
        np.zeros((2, 10, 10, 3), np.uint8),
        np.zeros((10, 10, 3), dtype=object),
        np.zeros((10, 10, 3), dtype=bool),
    ],
)
def test_invalid_frames_rejected(bad) -> None:
    with pytest.raises(ValueError):
        ChangeGate(backend="numpy").process(bad, 0.0)


def test_thread_safety() -> None:
    scene = build_scene("object_appears", seed=4)
    gate = ChangeGate(backend="numpy")
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for i, f in enumerate(scene.frames):
                r = gate.process(f, float(i))
                assert isinstance(r, GateResult)
        except BaseException as exc:  # pragma: no cover - only on failure
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors
    assert gate.stats()["frames"] == 4 * len(scene.frames)


def test_state_is_bounded() -> None:
    """Long runs keep a fixed amount of state (no per-frame growth)."""
    gate = ChangeGate(backend="numpy")
    frames = static_scene(seed=0, n_frames=3).frames
    for i in range(200):
        gate.process(frames[i % 3], float(i))
    st = gate._state
    assert st.prev is not None and st.background is not None
    assert st.prev.img.shape == st.background.shape == (120, 160)
    assert len(gate._ops._resize_cache) <= 17


# ----------------------------------------------------------------- config


def test_config_validation() -> None:
    for bad in [
        {"window": 4},
        {"window": 1},
        {"max_width": 8},
        {"background_alpha": 0},
        {"global_change_max": 0},
        {"min_area_ratio": 1.0},
        {"noise_k": 0},
        {"shift_tolerance": 9},
        {"idle_check_seconds": float("inf")},
        {"noise_bins": 0},
        {"min_dissimilarity": 1.0},
        {"blur_sigma": -1},
    ]:
        with pytest.raises(ValueError):
            GateConfig(**bad)


def test_config_from_mapping() -> None:
    cfg = GateConfig.from_mapping(
        {
            "idle_check_seconds": 60,
            "window": 9.0,
            "tick_on_start": False,
            "unknown": 1,
            "min_score": None,
        }
    )
    assert cfg.idle_check_seconds == 60.0 and cfg.window == 9 and cfg.tick_on_start is False
    assert cfg.min_score == GateConfig().min_score
    assert GateConfig.from_mapping(None) == GateConfig()
    for bad in [
        {"window": 7.5},
        {"window": "x"},
        {"tick_on_start": "yes"},
        {"min_score": True},
        {"window": True},
    ]:
        with pytest.raises(ValueError):
            GateConfig.from_mapping(bad)


# ------------------------------------------------------------ primitives


@pytest.mark.parametrize("shift", [(0, 0), (2, -1), (-3, 4), (5, 0)])
def test_estimate_shift(shift) -> None:
    ref = static_scene(seed=1, n_frames=1).frames[0].mean(axis=2).astype(np.float32)
    cur = gate_mod._shifted(ref, *shift)
    assert gate_mod.estimate_shift(ref, cur, 8) == shift


def test_estimate_shift_rejects_large_and_flat() -> None:
    ref = static_scene(seed=1, n_frames=1).frames[0].mean(axis=2).astype(np.float32)
    assert gate_mod.estimate_shift(ref, gate_mod._shifted(ref, 6, 0), 4) == (0, 0)
    flat = np.full((40, 40), 7.0, np.float32)
    assert gate_mod.estimate_shift(flat, flat, 8) == (0, 0)
    assert gate_mod.estimate_shift(ref, ref, 0) == (0, 0)


def test_bbox_iou() -> None:
    assert bbox_iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    assert bbox_iou((0, 0, 10, 10), (5, 0, 10, 10)) == pytest.approx(50 / 150)
    assert bbox_iou((0, 0, 10, 10), (20, 20, 5, 5)) == 0.0
    assert bbox_iou(None, (0, 0, 1, 1)) == 0.0
    assert bbox_iou((0, 0, 0, 0), (0, 0, 0, 0)) == 0.0


def test_noise_level_tracks_brightness() -> None:
    rng = np.random.default_rng(0)
    brightness = np.repeat(np.linspace(0, 255, 64)[None, :], 64, axis=0).astype(np.float32)
    score = (rng.exponential(size=(64, 64)) * (1 + brightness / 10)).astype(np.float32)
    level = gate_mod._noise_level(score, brightness, 8)
    assert level[:, -1].mean() > 5 * level[:, 0].mean()
    flat = gate_mod._noise_level(score, brightness, 1)
    assert np.allclose(flat, np.median(score))


def test_result_should_judge() -> None:
    assert GateResult(True, 0.1, (0, 0, 1, 1)).should_judge
    assert GateResult(False, 0.0, None, tick=True).should_judge
    assert not GateResult(False, 0.0, None).should_judge
