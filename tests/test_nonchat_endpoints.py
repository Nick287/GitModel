"""Tests for the non-chat Copilot routes: embeddings, decisions, images.

The upstream is replaced by fakes on ``hub.copilot_client``, so these check
what the hub itself decides: which upstream path a request is sent to, how an
image request is routed between Copilot and the Azure backend, what gets
recorded, and how upstream failures are mirrored.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["HUB_DATA_DIR"] = tempfile.mkdtemp(prefix="hub-test-")
os.environ["HUB_REQUIRE_AUTH"] = "false"

import pytest
from fastapi.testclient import TestClient

from hub import copilot_client as cc
from hub import image_client as ic
from hub import server, store

# The app creates its tables in a startup hook, which a TestClient only runs
# when used as a context manager. Auth checks read the settings table on every
# request, so create it up front.
store.init_db()

MODELS = [
    {"id": "gpt-4.1", "capabilities": {"type": "chat"}},
    {"id": "text-embedding-3-small", "capabilities": {"type": "embeddings"}},
    {"id": "gpt-image-2.5-flare", "capabilities": {"type": "image"}},
    {"id": "gpt-6-luna-decisions", "capabilities": {"type": "decisions"}},
]


@pytest.fixture()
def hub(monkeypatch):
    """A test client plus the lists the fakes append to."""
    calls: list[tuple[str, dict]] = []
    multipart: list[tuple[str, dict, list]] = []
    records: list[dict] = []
    reply = {"status": 200, "body": {"model": "served", "usage": {}}}

    async def post_json(path, payload, extra_headers=None):
        calls.append((path, payload))
        return reply["status"], reply["body"]

    async def post_multipart(path, data, files):
        multipart.append((path, data, files))
        return reply["status"], reply["body"]

    async def list_models():
        return MODELS

    monkeypatch.setattr(cc, "is_authenticated", lambda: True)
    monkeypatch.setattr(cc, "post_json", post_json)
    monkeypatch.setattr(cc, "post_multipart", post_multipart)
    monkeypatch.setattr(cc, "list_models", list_models)
    monkeypatch.setattr(store, "record_usage", lambda **kw: records.append(kw))
    monkeypatch.setattr(store, "get_expose_copilot_usage", lambda: False)
    monkeypatch.setattr(ic, "is_configured", lambda: False)
    cc._model_types.clear()

    class Hub:
        pass

    h = Hub()
    h.client = TestClient(server.app)
    h.calls, h.multipart, h.records, h.reply = calls, multipart, records, reply
    return h


def test_embeddings_forwards_without_v1_prefix_and_records_usage(hub):
    hub.reply["body"] = {
        "data": [{"embedding": [0.1, 0.2], "index": 0, "object": "embedding"}],
        "usage": {"prompt_tokens": 7, "total_tokens": 7},
    }
    body = {"model": "text-embedding-3-small", "input": ["hi"], "dimensions": 256}
    r = hub.client.post("/v1/embeddings", json=body)
    assert r.status_code == 200
    assert r.json()["data"][0]["embedding"] == [0.1, 0.2]
    assert hub.calls == [("/embeddings", body)]
    rec = hub.records[0]
    assert rec["endpoint"] == "embeddings"
    assert rec["model"] == "text-embedding-3-small"
    assert (rec["input_tokens"], rec["output_tokens"], rec["total_tokens"]) == (7, 0, 7)
    assert rec["status"] == 200


@pytest.mark.parametrize(
    "route,upstream,endpoint",
    [
        ("/v1/decisions", "/v1/decisions", "decisions"),
        ("/v1/systemone", "/v1/systemone", "systemone"),
    ],
)
def test_decision_routes_keep_v1_prefix_and_body(hub, route, upstream, endpoint):
    hub.reply["body"] = {
        "model": "served-model",
        "answers": [{"type": "predicate", "name": "angry", "probability": 0.94}],
        "usage": {"input_tokens": 266, "output_tokens": 2},
    }
    body = {"model": "m", "input": "x", "questions": [{"type": "predicate", "name": "angry"}]}
    r = hub.client.post(route, json=body)
    assert r.status_code == 200
    assert r.json()["answers"][0]["probability"] == 0.94
    assert hub.calls == [(upstream, body)]
    rec = hub.records[0]
    assert rec["endpoint"] == endpoint
    assert rec["served_model"] == "served-model"
    assert (rec["input_tokens"], rec["output_tokens"], rec["total_tokens"]) == (266, 2, 268)


def test_copilot_billing_block_is_recorded_then_hidden(hub):
    hub.reply["body"] = {
        "answers": {},
        "usage": {"input_tokens": 10, "output_tokens": 1},
        "copilot_usage": {
            "total_nano_aiu": 4321,
            "token_details": [{"token_type": "input", "token_count": 10}],
        },
    }
    r = hub.client.post("/v1/systemone", json={"model": "m"})
    assert hub.records[0]["nano_aiu"] == 4321
    assert "copilot_usage" not in r.json()


def test_upstream_client_error_is_mirrored(hub):
    hub.reply.update(status=400, body={"error": {"code": "array_below_min_length"}})
    r = hub.client.post("/v1/decisions", json={"model": "m"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "array_below_min_length"
    assert hub.records[0]["status"] == 400
    assert hub.records[0]["total_tokens"] == 0


def test_upstream_auth_rejection_becomes_502(hub):
    hub.reply.update(status=401, body={"error": {"message": "bad token"}})
    r = hub.client.post("/v1/embeddings", json={"model": "m", "input": "x"})
    assert r.status_code == 502
    assert hub.records[0]["status"] == 502


def test_transport_failure_is_502_and_recorded(hub, monkeypatch):
    async def boom(path, payload, extra_headers=None):
        raise RuntimeError("token exchange failed")

    monkeypatch.setattr(cc, "post_json", boom)
    r = hub.client.post("/v1/embeddings", json={"model": "m", "input": "x"})
    assert r.status_code == 502
    assert hub.records[0]["status"] == 502


def test_not_logged_in_is_503(hub, monkeypatch):
    monkeypatch.setattr(cc, "is_authenticated", lambda: False)
    r = hub.client.post("/v1/embeddings", json={"model": "m", "input": "x"})
    assert r.status_code == 503
    assert hub.calls == []


@pytest.mark.parametrize("payload", ["[1, 2]", "not json"])
def test_non_object_body_is_400(hub, payload):
    r = hub.client.post(
        "/v1/embeddings", content=payload, headers={"content-type": "application/json"}
    )
    assert r.status_code == 400
    assert hub.calls == []


def test_api_key_is_enforced_on_new_routes(hub, monkeypatch):
    monkeypatch.setattr(store, "get_require_auth", lambda default: True)
    monkeypatch.setattr(store, "is_valid_api_key", lambda key: key == "good")
    for route in ("/v1/embeddings", "/v1/decisions", "/v1/systemone"):
        assert hub.client.post(route, json={"model": "m"}).status_code == 401
    assert hub.calls == []
    r = hub.client.post(
        "/v1/embeddings", json={"model": "m"}, headers={"authorization": "Bearer good"}
    )
    assert r.status_code == 200


def test_copilot_image_model_is_generated_upstream(hub):
    hub.reply["body"] = {
        "data": [{"b64_json": "AAAA"}],
        "usage": {"input_tokens": 12, "output_tokens": 196, "total_tokens": 208},
    }
    body = {"model": "gpt-image-2.5-flare", "prompt": "a cube"}
    r = hub.client.post("/v1/images/generations", json=body)
    assert r.status_code == 200
    assert r.json()["data"][0]["b64_json"] == "AAAA"
    assert hub.calls == [("/v1/images/generations", body)]
    rec = hub.records[0]
    assert rec["endpoint"] == "images.generations"
    assert (rec["input_tokens"], rec["output_tokens"], rec["total_tokens"]) == (12, 196, 208)


@pytest.mark.parametrize("model", ["gpt-image-2", None, "gpt-4.1"])
def test_other_image_requests_stay_on_azure_backend(hub, monkeypatch, model):
    sent: list[dict] = []

    async def generate(payload):
        sent.append(payload)
        return 200, {"data": [{"b64_json": "AZ"}], "usage": {"input_tokens": 1, "output_tokens": 2}}

    monkeypatch.setattr(ic, "is_configured", lambda: True)
    monkeypatch.setattr(ic, "generate", generate)
    body = {"prompt": "a cube"}
    if model:
        body["model"] = model
    r = hub.client.post("/v1/images/generations", json=body)
    assert r.status_code == 200
    assert r.json()["data"][0]["b64_json"] == "AZ"
    assert sent == [body]
    assert hub.calls == []


def test_non_copilot_image_model_without_azure_backend_is_503(hub):
    r = hub.client.post("/v1/images/generations", json={"model": "gpt-image-2", "prompt": "x"})
    assert r.status_code == 503
    assert hub.calls == []


def test_model_listing_failure_falls_back_to_azure_backend(hub, monkeypatch):
    async def broken():
        raise RuntimeError("listing down")

    monkeypatch.setattr(cc, "list_models", broken)
    r = hub.client.post(
        "/v1/images/generations", json={"model": "gpt-image-2.5-flare", "prompt": "x"}
    )
    assert r.status_code == 503  # Azure backend not configured in this fixture
    assert hub.calls == []


def test_copilot_image_edit_forwards_multipart(hub):
    hub.reply["body"] = {"data": [{"b64_json": "ED"}], "usage": {"input_tokens": 3, "output_tokens": 4}}
    r = hub.client.post(
        "/v1/images/edits",
        data={"model": "gpt-image-2.5-flare", "prompt": "make it blue"},
        files={"image": ("in.png", b"\x89PNG", "image/png")},
    )
    assert r.status_code == 200
    assert r.json()["data"][0]["b64_json"] == "ED"
    path, data, files = hub.multipart[0]
    assert path == "/v1/images/edits"
    assert data == {"model": "gpt-image-2.5-flare", "prompt": "make it blue"}
    assert files == [("image", ("in.png", b"\x89PNG", "image/png"))]
    assert hub.records[0]["endpoint"] == "images.edits"


def test_model_types_are_cached_between_requests(hub, monkeypatch):
    count = {"n": 0}

    async def counting():
        count["n"] += 1
        return MODELS

    monkeypatch.setattr(cc, "list_models", counting)
    for _ in range(3):
        hub.client.post("/v1/images/generations", json={"model": "gpt-image-2.5-flare", "prompt": "x"})
    assert count["n"] == 1
