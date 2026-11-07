"""Secret resolution: whole-URL placeholders, the in-memory cache and the scope-less thread."""

from __future__ import annotations

import sys

import pytest
from harness.plugin_loader import plugin_module

src = plugin_module("sentinel.sources")
secrets_mod = plugin_module("sentinel.sources.spec.secrets")
urls = plugin_module("sentinel.sources.spec.urls")
status_mod = plugin_module("sentinel.sources.spec.secret_status")

SLOT = "SENTINEL_STREAM_URL_1"
FULL_URL = "rtsp://admin:p@ss/word@10.0.0.5:554/live"


class UnscopedSecretError(RuntimeError):
    """Stand-in with the name of Hermes' fail-closed error."""


@pytest.fixture(autouse=True)
def _clean_cache():
    src.clear_cached_secrets()
    yield
    src.clear_cached_secrets()


@pytest.fixture
def no_scope(shim_binding, monkeypatch):
    """Make the shimmed ``get_secret`` raise as Hermes does on a scope-less multiplexed thread."""

    def raising(name, default=None):
        raise UnscopedSecretError(name)

    monkeypatch.setattr(sys.modules["agent.secret_scope"], "get_secret", raising)


# ------------------------------------------------------------------- whole-URL slots


def test_whole_url_placeholder_is_accepted_and_not_quoted(shim_binding):
    shim_binding.secrets[SLOT] = FULL_URL
    spec = src.SourceSpec.parse(f"stream:${{{SLOT}}}")
    assert spec.kind == "stream" and spec.canonical() == f"stream:${{{SLOT}}}"
    assert src.SourceSpec.parse(spec.canonical()) == spec
    assert urls.expand_url(str(spec.target), origin="t") == FULL_URL


def test_named_stream_can_point_at_a_slot_and_snapshot_prefix_forces_snapshot():
    named = {"door": f"${{{SLOT}}}", "yard": f"snapshot:${{{SLOT}}}"}
    assert src.SourceSpec.parse("door", named_streams=named).kind == "stream"
    assert src.SourceSpec.parse("yard", named_streams=named).kind == "snapshot"


def test_resolved_url_scheme_is_checked_and_not_echoed(shim_binding):
    shim_binding.secrets[SLOT] = "ftp://user:topsecret@host/x"
    with pytest.raises(src.SourceSpecError) as caught:
        urls.expand_url(f"${{{SLOT}}}", origin="t", schemes=frozenset({"rtsp", "http"}))
    assert "topsecret" not in str(caught.value)


def test_embedded_placeholder_is_still_quoted(shim_binding):
    shim_binding.secrets["PW"] = "p@ss/word"
    assert urls.expand_url("rtsp://u:${PW}@h/x", origin="t") == "rtsp://u:p%40ss%2Fword@h/x"


# ------------------------------------------------------------------- scope and cache


def test_read_in_scope_is_cached_and_refreshed(shim_binding):
    shim_binding.secrets["A"] = "one"
    assert secrets_mod.get_secret("A") == "one"
    shim_binding.secrets["A"] = "two"
    assert secrets_mod.get_secret("A") == "two"
    assert secrets_mod.SECRET_CACHE.recall("A") == "two"


def test_unset_secret_is_forgotten(shim_binding):
    shim_binding.secrets["A"] = "one"
    secrets_mod.get_secret("A")
    del shim_binding.secrets["A"]
    assert secrets_mod.get_secret("A") is None
    assert secrets_mod.SECRET_CACHE.recall("A") is None


def test_scopeless_thread_uses_the_cache(shim_binding, monkeypatch):
    shim_binding.secrets["A"] = "one"
    src.prime_secrets(["A"])
    monkeypatch.setattr(
        sys.modules["agent.secret_scope"],
        "get_secret",
        lambda name, default=None: (_ for _ in ()).throw(UnscopedSecretError(name)),
    )
    assert secrets_mod.get_secret("A") == "one"


def test_scopeless_thread_without_cache_waits_instead_of_failing(no_scope):
    with pytest.raises(src.SecretScopeUnavailable, match="waiting for secret scope"):
        secrets_mod.get_secret("A")
    assert issubclass(src.SecretScopeUnavailable, src.SourceNotReady)


def test_other_errors_are_source_errors_without_details(shim_binding, monkeypatch):
    def boom(name, default=None):
        raise ValueError("hunter2 leaked in message")

    monkeypatch.setattr(sys.modules["agent.secret_scope"], "get_secret", boom)
    with pytest.raises(src.SourceError) as caught:
        secrets_mod.get_secret("A")
    assert "hunter2" not in str(caught.value)


def test_clear_cached_secrets(shim_binding):
    shim_binding.secrets["A"] = "one"
    src.prime_secrets(["A", "MISSING"])
    assert len(secrets_mod.SECRET_CACHE) == 1
    src.clear_cached_secrets()
    assert len(secrets_mod.SECRET_CACHE) == 0


# ------------------------------------------------------------------- status


def test_referenced_secrets_finds_names_once():
    texts = ["rtsp://u:${PW}@h/x", f"${{{SLOT}}}", "rtsp://u:${PW}@h/y", "folder:/x"]
    assert src.referenced_secrets(texts) == ("PW", SLOT)


def test_probe_secrets_classifies_names(shim_binding):
    shim_binding.secrets["READY"] = "v"
    status = src.probe_secrets(["READY", "UNSET"])
    assert (status.ready, status.missing, status.waiting) == (("READY",), ("UNSET",), ())


def test_probe_secrets_reports_waiting(no_scope):
    status = src.probe_secrets(["A"])
    assert status.waiting == ("A",) and status.referenced == ("A",)
