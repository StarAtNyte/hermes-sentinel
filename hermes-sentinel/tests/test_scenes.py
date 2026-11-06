"""Checks for the synthetic scenes and fake sources: determinism and ground-truth labels."""

from __future__ import annotations

import numpy as np
import pytest
from harness.fake_sources import FakeClock, FakeSource
from harness.plugin_loader import plugin_module
from harness.scenes import (
    COMBINED_SCENARIOS,
    EVENT_SCENARIOS,
    NUISANCE_SCENARIOS,
    SCENARIOS,
    build_scene,
    diff_bbox,
    jpeg_like,
    mug_bbox,
    warp_affine,
)

_sources = plugin_module("sentinel.sources")
FrameSource = _sources.FrameSource
SourceDisconnected = _sources.SourceDisconnected
SourceError = _sources.SourceError
SourceExhausted = _sources.SourceExhausted


def _mean_abs_diff(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())


def _crop(img: np.ndarray, bbox) -> np.ndarray:
    x, y, w, h = bbox
    return img[y : y + h, x : x + w]


# ---------------------------------------------------------------- determinism


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_same_seed_gives_identical_frames(name):
    a = build_scene(name, seed=123)
    b = build_scene(name, seed=123)
    assert len(a) == len(b) > 0
    for fa, fb in zip(a.frames, b.frames, strict=True):
        assert np.array_equal(fa, fb)
    assert a.labels == b.labels


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_different_seed_gives_different_frames(name):
    a = build_scene(name, seed=1)
    b = build_scene(name, seed=2)
    assert not np.array_equal(a.frames[0], b.frames[0])


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_frame_format(name):
    scene = build_scene(name)
    w, h = scene.size
    for frame in scene.frames:
        assert frame.dtype == np.uint8
        assert frame.shape == (h, w, 3)
    assert [lab.index for lab in scene.labels] == list(range(len(scene)))
    assert scene.labels[0].changed is False  # nothing to compare frame 0 against


def test_unknown_scenario():
    with pytest.raises(KeyError):
        build_scene("nope")


# ----------------------------------------------------------- label correctness


@pytest.mark.parametrize("name", NUISANCE_SCENARIOS + ("disconnect",))
def test_nuisance_scenes_have_no_real_change(name):
    scene = build_scene(name, seed=5)
    assert scene.should_trigger is False
    assert scene.change_indices == []
    assert all(lab.bbox is None for lab in scene.labels)


@pytest.mark.parametrize("name", EVENT_SCENARIOS)
def test_event_scenes_have_labelled_change(name):
    scene = build_scene(name, seed=5)
    assert scene.should_trigger is True
    assert scene.change_indices
    for i in scene.change_indices:
        lab = scene.labels[i]
        assert lab.bbox is not None
        x, y, w, h = lab.bbox
        fw, fh = scene.size
        assert w > 0 and h > 0 and x >= 0 and y >= 0 and x + w <= fw and y + h <= fh


def test_static_scene_frames_differ_only_by_sensor_noise():
    scene = build_scene("static_scene", seed=3)
    for prev, cur in zip(scene.frames, scene.frames[1:], strict=False):
        assert not np.array_equal(prev, cur)  # noise is present
        assert _mean_abs_diff(prev, cur) < 4.0


@pytest.mark.parametrize("name", ["object_appears", "object_removed"])
def test_mug_change_frame_and_bbox(name):
    at = 12
    key = "appear_at" if name == "object_appears" else "remove_at"
    scene = build_scene(name, seed=9, **{key: at})
    assert scene.change_indices == [at]
    lab = scene.labels[at]
    w, h = scene.size
    expected = mug_bbox(w, h)
    # The changed pixels sit inside the mug's footprint and cover most of it.
    bx, by, bw, bh = lab.bbox
    ex, ey, ew, eh = expected
    assert ex <= bx and ey <= by and bx + bw <= ex + ew and by + bh <= ey + eh
    assert bw * bh >= 0.7 * ew * eh

    # The real pixels agree: big change inside the bbox, only noise outside.
    prev, cur = scene.frames[at - 1], scene.frames[at]
    assert _mean_abs_diff(_crop(prev, lab.bbox), _crop(cur, lab.bbox)) > 30.0
    outside = np.ones(prev.shape[:2], dtype=bool)
    outside[by : by + bh, bx : bx + bw] = False
    assert _mean_abs_diff(prev[outside], cur[outside]) < 4.0

    # Presence labels flip at the event frame.
    for i, label in enumerate(scene.labels):
        visible = (i >= at) if name == "object_appears" else (i < at)
        assert (label.present is not None) == visible


def test_lighting_flicker_is_global_and_large():
    scene = build_scene("lighting_flicker", seed=4)
    means = [float(f.mean()) for f in scene.frames]
    assert max(means) - min(means) > 10.0  # strong enough to fool a naive diff gate


def test_camera_shake_moves_the_whole_frame():
    scene = build_scene("camera_shake", seed=4, max_shift=2)
    shifts = scene.meta["shifts"]
    assert all(abs(dx) <= 2 and abs(dy) <= 2 for dx, dy in shifts)
    assert any(s != shifts[0] for s in shifts)


def test_slow_drift_is_gradual():
    scene = build_scene("slow_drift", seed=4, n_frames=40, total_delta=40.0)
    means = [float(f.mean()) for f in scene.frames]
    assert means[-1] - means[0] > 25.0
    steps = np.diff(means)
    assert np.all(np.abs(steps) < 3.0)


def test_screen_text_change_labels():
    scene = build_scene("screen_text_change", seed=8, n_frames=30, change_every=10)
    assert scene.change_indices == [10, 20]
    values = scene.meta["values"]
    assert values[0] != values[10] != values[20]
    assert len(set(values[0:10])) == 1
    for i in scene.change_indices:
        lab = scene.labels[i]
        # Change is confined to the numeric field, never the "Training loss:" prefix.
        fx, fy, fw, fh = lab.present
        bx, by, bw, bh = lab.bbox
        assert fx <= bx and fy <= by and bx + bw <= fx + fw and by + bh <= fy + fh
        # Screens are noise-free, so the pixel diff equals the labelled bbox exactly.
        assert diff_bbox(scene.frames[i - 1], scene.frames[i]) == lab.bbox
    for i in range(1, len(scene)):
        if i not in scene.change_indices:
            assert np.array_equal(scene.frames[i - 1], scene.frames[i])


# --------------------------------------------------------------- fake sources


def test_fake_source_satisfies_protocol():
    assert isinstance(FakeSource(build_scene("static_scene")), FrameSource)


def test_fake_source_replays_with_virtual_clock():
    scene = build_scene("object_appears", seed=1, n_frames=20, fps=5.0)
    clock = FakeClock(start=100.0)
    src = FakeSource(scene, clock=clock)
    with pytest.raises(SourceError):
        src.read()
    src.open()
    got = [src.read() for _ in range(len(scene))]
    assert [ts for ts, _ in got] == pytest.approx([100.0 + i * 0.2 for i in range(20)])
    for (_, frame), expected in zip(got, scene.frames, strict=True):
        assert np.array_equal(frame, expected)
    with pytest.raises(SourceExhausted):
        src.read()
    src.close()
    with pytest.raises(SourceError):
        src.read()


def test_fake_source_returns_copies():
    scene = build_scene("static_scene", n_frames=3)
    src = FakeSource(scene)
    src.open()
    _, frame = src.read()
    frame[:] = 0
    assert scene.frames[0].any()


def test_fake_source_loop():
    scene = build_scene("static_scene", n_frames=3)
    src = FakeSource(scene, loop=True)
    src.open()
    frames = [src.read()[1] for _ in range(7)]
    assert np.array_equal(frames[3], scene.frames[0])
    assert np.array_equal(frames[6], scene.frames[0])


def test_disconnect_scenario_raises_after_n_frames():
    scene = build_scene("disconnect", seed=2, n_frames=6)
    src = FakeSource(scene)
    src.open()
    for _ in range(6):
        src.read()
    with pytest.raises(SourceDisconnected):
        src.read()
    with pytest.raises(SourceDisconnected):  # stays down
        src.read()
    src.open()  # reconnect starts the clip over
    src.read()
    assert src.open_count == 2


def test_disconnect_override_and_iteration():
    scene = build_scene("object_appears", n_frames=20)
    src = FakeSource(scene, disconnect_after=4)
    with src:
        assert len(list(src)) == 4
    assert src.close_count == 1


def test_open_error():
    src = FakeSource(build_scene("static_scene"), open_error=SourceError("no camera"))
    with pytest.raises(SourceError, match="no camera"):
        src.open()


def test_manual_clock():
    clock = FakeClock()
    src = FakeSource(build_scene("static_scene", n_frames=3), clock=clock, advance_clock=False)
    src.open()
    assert src.read()[0] == 0.0
    clock.advance(2.5)
    assert src.read()[0] == 2.5
    with pytest.raises(ValueError):
        clock.advance(-1)


async def test_fake_clock_async_sleep():
    clock = FakeClock()
    await clock.asleep(3.0)
    assert clock.now() == 3.0
    assert clock.sleeps == [3.0]


# ------------------------------------------------- new nuisance + combined scenes


def _blockiness(frame: np.ndarray) -> float:
    """Mean |step| across 8-px block boundaries divided by mean |step| inside blocks."""
    gray = frame.astype(np.float32).mean(axis=2)
    steps = np.abs(np.diff(gray, axis=1))  # step between column c and c+1
    cols = np.arange(steps.shape[1])
    boundary = steps[:, cols % 8 == 7].mean()
    inside = steps[:, cols % 8 != 7].mean()
    return float(boundary / max(inside, 1e-6))


def test_registry_groups_are_consistent():
    assert set(NUISANCE_SCENARIOS).isdisjoint(EVENT_SCENARIOS)
    assert set(COMBINED_SCENARIOS) <= set(EVENT_SCENARIOS)
    assert set(NUISANCE_SCENARIOS) | set(EVENT_SCENARIOS) | {"disconnect"} == set(SCENARIOS)


def test_compression_artifacts_are_blocky_and_vary():
    scene = build_scene("compression_artifacts", seed=3, quality_range=(10, 20))
    clean = build_scene("static_scene", seed=3)
    assert _blockiness(scene.frames[0]) > 1.3
    assert _blockiness(scene.frames[0]) > _blockiness(clean.frames[0]) + 0.2
    qualities = scene.meta["qualities"]
    assert all(10 <= q <= 20 for q in qualities) and len(set(qualities)) > 1
    assert not np.array_equal(scene.frames[0], scene.frames[1])
    # Codec error is bounded: the picture is still the same scene.
    assert _mean_abs_diff(scene.frames[0], clean.frames[0]) < 12.0


def test_jpeg_like_quality_controls_error():
    rng = np.random.default_rng(0)
    img = rng.uniform(0, 255, size=(24, 40, 3)).astype(np.float32)  # not a multiple of 8
    err_low = float(np.abs(jpeg_like(img, 10) - img).mean())
    err_high = float(np.abs(jpeg_like(img, 95) - img).mean())
    assert jpeg_like(img, 50).shape == img.shape
    assert err_high < err_low
    flat = np.full((16, 16, 3), 100.0, dtype=np.float32)
    out = jpeg_like(flat, 10)
    assert float(out.std()) < 1e-3  # a flat block stays flat (only DC survives)
    assert abs(float(out.mean()) - 100.0) < 6.0  # DC quantisation error: step / 16


def test_compression_rejects_bad_quality():
    with pytest.raises(ValueError):
        build_scene("compression_artifacts", quality_range=(50, 10))


def test_heavy_low_light_noise_is_dark_and_noisy():
    scene = build_scene("heavy_low_light_noise", seed=2)
    assert float(np.mean([f.mean() for f in scene.frames])) < 60.0
    diffs = [_mean_abs_diff(a, b) for a, b in zip(scene.frames, scene.frames[1:], strict=False)]
    assert min(diffs) > 8.0  # far noisier than the normal static scene (< 4)
    with pytest.raises(ValueError):
        build_scene("heavy_low_light_noise", gain=0.0)


def test_ir_night_switch_drops_colour_at_switch():
    scene = build_scene("ir_night_switch", seed=4, switch_at=10, n_frames=20)

    def chroma(frame: np.ndarray) -> float:
        f = frame.astype(np.float32)
        return float((f.max(axis=2) - f.min(axis=2)).mean())

    before = [chroma(f) for f in scene.frames[:10]]
    after = [chroma(f) for f in scene.frames[10:]]
    assert min(before) > 2 * max(after)
    assert max(after) < 8.0  # only per-channel sensor noise left
    assert _mean_abs_diff(scene.frames[9], scene.frames[10]) > 10.0  # big global jump
    assert scene.labels[10].note == "IR mode on" and scene.change_indices == []
    with pytest.raises(ValueError):
        build_scene("ir_night_switch", switch_at=0)


@pytest.mark.parametrize(
    ("name", "key", "sign", "direction"),
    [
        ("local_shadow_pass", "shadow_bboxes", -1, 1),  # darker, moves left -> right
        ("headlight_sweep", "light_bboxes", 1, -1),  # brighter, moves right -> left
    ],
)
def test_moving_light_is_local_and_moves(name, key, sign, direction):
    scene = build_scene(name, seed=6, n_frames=30)
    clean = build_scene(name, seed=6, n_frames=30)  # determinism sanity
    assert np.array_equal(scene.frames[15], clean.frames[15])
    boxes = [b for b in scene.meta[key] if b is not None]
    assert len(boxes) >= 10
    xs = [x + w / 2 for x, _, w, _ in boxes]
    steps = np.diff(xs) * direction
    assert np.all(steps >= 0) and xs[0] != xs[-1]  # sweeps one way across the frame
    fw, fh = scene.size
    mid = scene.meta[key][15]
    assert mid is not None and mid[2] < fw  # local, not the whole frame
    # The lit/shadowed region differs from the same region far from the light.
    x, y, w, h = mid
    far = 0 if (x + w / 2) > fw / 2 else len(scene) - 1
    delta = float(_crop(scene.frames[15], mid).mean()) - float(_crop(scene.frames[far], mid).mean())
    assert sign * delta > 10.0
    assert scene.change_indices == []


def test_shadow_and_headlight_validate_args():
    with pytest.raises(ValueError):
        build_scene("local_shadow_pass", darkness=1.5)


def test_subpixel_shake_rotation_is_small_and_continuous():
    scene = build_scene("subpixel_shake_rotation", seed=1, max_shift=0.8, max_angle_deg=0.6)
    shifts = np.array(scene.meta["shifts"])
    angles = np.array(scene.meta["angles_deg"])
    assert np.all(np.abs(shifts) <= 0.8) and np.all(np.abs(angles) <= 0.6)
    assert np.any(np.abs(shifts - np.round(shifts)) > 0.1)  # genuinely sub-pixel
    diffs = [_mean_abs_diff(a, b) for a, b in zip(scene.frames, scene.frames[1:], strict=False)]
    assert all(2.0 < d < 15.0 for d in diffs)


def test_warp_affine_identity_and_shift():
    rng = np.random.default_rng(0)
    img = rng.uniform(0, 255, size=(20, 30, 3)).astype(np.float32)
    assert np.allclose(warp_affine(img, 0.0, 0.0, 0.0), img)
    shifted = warp_affine(img, 1.0, 0.0, 0.0)
    assert np.allclose(shifted[:, 1:], img[:, :-1])
    half = warp_affine(img, 0.5, 0.0, 0.0)
    assert np.allclose(half[:, 1:], (img[:, 1:] + img[:, :-1]) / 2)


@pytest.mark.parametrize(
    ("name", "key", "appears"),
    [("flicker_with_mug", "remove_at", False), ("shake_with_mug", "appear_at", True)],
)
def test_combined_scenes_label_only_the_real_event(name, key, appears):
    at = 13
    scene = build_scene(name, seed=7, **{key: at})
    assert scene.should_trigger and scene.change_indices == [at]
    assert scene.meta["event_frame"] == at and scene.meta["nuisance"]
    w, h = scene.size
    ex, ey, ew, eh = mug_bbox(w, h)
    bx, by, bw, bh = scene.labels[at].bbox
    assert ex <= bx and ey <= by and bx + bw <= ex + ew and by + bh <= ey + eh
    for i, label in enumerate(scene.labels):
        assert (label.present is not None) == ((i >= at) == appears)
    # The nuisance is active on frames without an event too.
    quiet = [
        _mean_abs_diff(a, b) for a, b in zip(scene.frames[1:at], scene.frames[2:at], strict=False)
    ]
    assert max(quiet) > 4.0


def test_combined_scenes_validate_event_frame():
    with pytest.raises(ValueError):
        build_scene("flicker_with_mug", remove_at=0)
    with pytest.raises(ValueError):
        build_scene("shake_with_mug", appear_at=99)
