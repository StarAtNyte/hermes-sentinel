"""Rule routes: list, pause, resume, delete and watch."""

from __future__ import annotations

import pytest
from harness.dashboard_rig import (
    BASE,
    CAM,
    RTSP,
    add_event,
    no_secrets,
    watch_rule,
)


def test_rules_list_shape(client, rig):
    rule = watch_rule(rig)
    rig.pump(1)
    [item] = client.get(f"{BASE}/rules").json()
    assert set(item) == {
        "id",
        "text",
        "source",
        "mode",
        "status",
        "paused",
        "last_event_at",
        "vision_calls_hour",
        "cooldown_s",
        "hold_s",
    }
    assert item["id"] == rule and item["source"] == CAM and item["paused"] is False
    assert item["last_event_at"] is None and item["vision_calls_hour"] >= 0
    assert item["text"] and item["mode"] in ("trigger", "condition", "absence")


def test_rules_list_shows_last_event(client, rig):
    rule = watch_rule(rig)
    ev = add_event(rig, rule)
    assert client.get(f"{BASE}/rules").json()[0]["last_event_at"] == ev.ts


def test_pause_resume_delete_roundtrip(client, rig):
    rule = watch_rule(rig)
    paused = client.post(f"{BASE}/rules/{rule}/pause")
    assert paused.status_code == 200 and paused.json()["ok"] is True
    assert paused.json()["rule"]["paused"] is True and paused.json()["rule"]["status"] == "paused"
    assert rig.sentinel.store.get_rule(rule).enabled is False
    resumed = client.post(f"{BASE}/rules/{rule}/resume").json()
    assert resumed["rule"]["paused"] is False
    assert rig.sentinel.store.get_rule(rule).enabled is True
    deleted = client.delete(f"{BASE}/rules/{rule}")
    assert deleted.status_code == 200 and deleted.json()["rule"]["id"] == rule
    assert rig.sentinel.store.get_rule(rule) is None
    assert client.get(f"{BASE}/rules").json() == []


def test_delete_keeps_events(client, rig):
    rule = watch_rule(rig)
    add_event(rig, rule)
    client.delete(f"{BASE}/rules/{rule}")
    assert len(client.get(f"{BASE}/events").json()) == 1


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/rules/nope/pause"),
        ("post", "/rules/nope/resume"),
        ("delete", "/rules/nope"),
    ],
)
def test_unknown_rule_is_404(client, method, path):
    resp = getattr(client, method)(BASE + path)
    assert resp.status_code == 404 and resp.json()["ok"] is False


@pytest.mark.parametrize("bad", ["a b", "x" * 70, "a.b", "%24%7Bx%7D"])
def test_invalid_rule_id_is_400(client, bad):
    resp = client.post(f"{BASE}/rules/{bad}/pause")
    assert resp.status_code in (400, 404) and resp.json()["ok"] is False


def test_dashboard_owns_rules_made_in_messaging_chats(client, rig):
    rule = watch_rule(rig)
    spec = rig.sentinel.store.get_rule(rule)
    rig.sentinel.store.upsert_rule(
        spec.with_changes(origin={"platform": "telegram", "chat_id": "42"})
    )
    assert [r["id"] for r in client.get(f"{BASE}/rules").json()] == [rule]
    assert client.post(f"{BASE}/rules/{rule}/pause").status_code == 200


def test_watch_with_recipe(client, rig):
    resp = client.post(
        f"{BASE}/watch", json={"text": "my desk", "source": CAM, "recipe": "desk-intruder"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["rule"]["source"] == CAM
    assert body["rule"]["id"] and rig.sentinel.store.get_rule(body["rule"]["id"])
    assert "note" in body and "Alerts are injected" not in body["note"]
    assert rig.sentinel.engine.running


def test_watch_plain_text_uses_compiler(client, rig):
    rig.ctx.llm.respond(
        lambda call: {"question": "Is a mug on the desk?", "mode": "absence"}, task="sentinel_rules"
    )
    resp = client.post(f"{BASE}/watch", json={"text": "tell me if the mug is gone", "source": CAM})
    assert resp.status_code == 200, resp.text
    assert resp.json()["rule"]["text"]


@pytest.mark.parametrize(
    "payload,status",
    [
        ({}, 400),
        ({"text": ""}, 400),
        ({"text": 5}, 400),
        ({"text": "x" * 2500}, 400),
        ({"text": "x", "recipe": "no-such-recipe"}, 400),
        ({"text": "x", "source": "bogus://"}, 400),
        ([], 400),
    ],
)
def test_watch_validation(client, rig, payload, status):
    resp = client.post(f"{BASE}/watch", json=payload)
    assert resp.status_code == status and resp.json()["ok"] is False


def test_watch_errors_never_leak_credentials(client, rig):
    resp = client.post(f"{BASE}/watch", json={"text": "x", "source": RTSP, "recipe": "nope"})
    assert resp.status_code == 400
    no_secrets(resp)
