"""Setup checks about the installation: packages, vision model, settings, storage."""

from __future__ import annotations

from harness.plugin_loader import plugin_module
from harness.setup_rig import FakeStore, run

cfg_mod = plugin_module("sentinel.config")


def test_deps_missing(tmp_path):
    missing = run(tmp_path, module_available=lambda m: m not in ("numpy", "cv2"))["deps"]
    assert missing.status == "fail" and "numpy" in missing.detail
    assert "opencv-python-headless" in missing.detail
    assert "opencv-python-headless" in missing.fix and "enable" in missing.fix
    mss = run(tmp_path, module_available=lambda m: m != "mss")["deps"]
    assert mss.status == "warn" and "screen capture" in mss.detail


def test_vision_model_is_never_claimed_ok(tmp_path):
    c = run(tmp_path)["vision"]
    assert c.status == "warn" and "must accept images" in c.detail
    assert "default model" in c.detail and "sentinel_vision" in c.fix
    assert run(tmp_path, has_llm=False)["vision"].status == "fail"


def test_config_problems_listed_only_when_present(tmp_path):
    assert "config" not in run(tmp_path)
    cfg = cfg_mod.SentinelConfig(problems=("confirm_k: nope",))
    assert run(tmp_path, config=cfg)["config"].status == "warn"


def test_storage_writable_low_and_unwritable(tmp_path):
    ok = run(tmp_path)["storage"]
    assert ok.status == "ok" and "GB free" in ok.detail
    assert list(tmp_path.iterdir()) == []
    assert run(tmp_path, disk_free_mb=lambda p: 20.0)["storage"].status == "fail"
    assert run(tmp_path, disk_free_mb=lambda p: 200.0)["storage"].status == "warn"
    blocker = tmp_path / "file"
    blocker.write_text("x")
    bad = run(tmp_path, store_getter=lambda: FakeStore(blocker / "sub"))["storage"]
    assert bad.status == "fail" and "Cannot open or write" in bad.detail

    def nostore():
        raise OSError("no db")

    assert run(tmp_path, store_getter=nostore)["storage"].status == "fail"

    def nodisk(path):
        raise OSError("x")

    assert run(tmp_path, disk_free_mb=nodisk)["storage"].status == "ok"
