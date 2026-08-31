"""Tests for the passthrough request helpers on /v1/messages.

Regression context: Claude Code's ``Read`` tool returns images inside a
``tool_result`` block. Detection has to recurse into that nested content list —
if it misses the image, the ``Copilot-Vision-Request`` header is never set and
the upstream answers as if the image were not there.

These used to test the OpenAI translation layer, which forwarded those images
on a following ``user`` message. Passthrough removed the translation entirely,
so the same regression is now guarded on the function the passthrough path
actually calls.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.anthropic_adapter import (
    UNSUPPORTED_UPSTREAM_FIELDS,
    has_image_content_anthropic,
    strip_unsupported,
)

# A tiny valid 1x1 PNG, base64-encoded.
PNG_DATA = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _image_block():
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": PNG_DATA,
        },
    }


def _request(content):
    return {"model": "claude-opus-5", "messages": [{"role": "user", "content": content}]}


def test_image_inside_tool_result_is_detected():
    # The whole point: a screenshot returned by the Read tool must still set
    # the Vision header, even though it is nested one level down.
    req = _request([
        {
            "type": "tool_result",
            "tool_use_id": "toolu_abc",
            "content": [
                {"type": "text", "text": "Here is the screenshot:"},
                _image_block(),
            ],
        }
    ])
    assert has_image_content_anthropic(req) is True


def test_image_directly_in_message_content_is_detected():
    req = _request([_image_block(), {"type": "text", "text": "Describe this."}])
    assert has_image_content_anthropic(req) is True


def test_image_in_system_blocks_is_detected():
    req = {"model": "claude-opus-5", "system": [_image_block()], "messages": []}
    assert has_image_content_anthropic(req) is True


def test_text_only_tool_result_is_not_flagged():
    # Regression guard the other way: a false positive sends the Vision header
    # on every plain tool call.
    req = _request([
        {
            "type": "tool_result",
            "tool_use_id": "toolu_abc",
            "content": [{"type": "text", "text": "exit code 0"}],
        }
    ])
    assert has_image_content_anthropic(req) is False


def test_string_content_is_not_flagged():
    assert has_image_content_anthropic(_request("plain string")) is False


def test_context_management_is_stripped():
    # Claude Code sends this on every turn and the upstream rejects it with
    # 400 "Extra inputs are not permitted", so passthrough must drop it.
    req = _request("hi")
    req["context_management"] = {"edits": [{"type": "clear_tool_uses_20250919"}]}
    out, dropped = strip_unsupported(req)
    assert dropped == ["context_management"]
    assert "context_management" not in out
    assert out["messages"] == req["messages"]


def test_strip_does_not_mutate_the_input():
    # `mcp_servers` rather than `container`: probing the upstream showed
    # container is accepted (it validates the value's shape instead of
    # refusing the key), so it is no longer stripped and cannot stand in for
    # a dropped field here. What this test is really about is the copy.
    req = _request("hi")
    req["mcp_servers"] = [{"type": "url", "url": "https://x", "name": "n"}]
    out, dropped = strip_unsupported(req)
    assert dropped == ["mcp_servers"]
    assert "mcp_servers" in req  # caller's object untouched
    assert out is not req


def test_container_is_forwarded():
    # Measured against the live upstream: `container` alone answers 200, and a
    # bad value comes back as a shape error rather than "Extra inputs are not
    # permitted" -- which only a field the backend knows can produce.
    req = _request("hi")
    req["container"] = {"id": "abc"}
    out, dropped = strip_unsupported(req)
    assert dropped == []
    assert out["container"] == {"id": "abc"}


def test_context_management_survives_when_its_beta_header_is_present():
    # The upstream accepts the field only with this header, so stripping it
    # depends on the caller rather than on the field alone.
    req = _request("hi")
    req["context_management"] = {"edits": [{"type": "clear_tool_uses_20250919"}]}
    out, dropped = strip_unsupported(req, "context-management-2025-06-27")
    assert dropped == []
    assert out["context_management"] == req["context_management"]


def test_strip_returns_the_original_object_when_nothing_to_drop():
    req = _request("hi")
    out, dropped = strip_unsupported(req)
    assert dropped == []
    assert out is req  # common path stays allocation-free


def test_every_unsupported_field_is_dropped():
    req = _request("hi")
    for f in UNSUPPORTED_UPSTREAM_FIELDS:
        req[f] = {}
    out, dropped = strip_unsupported(req)
    assert sorted(dropped) == sorted(UNSUPPORTED_UPSTREAM_FIELDS)
    assert not any(f in out for f in UNSUPPORTED_UPSTREAM_FIELDS)
