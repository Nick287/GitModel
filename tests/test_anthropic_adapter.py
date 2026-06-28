"""Tests for Anthropic<->OpenAI translation, focused on tool_result images.

Regression context: Claude Code's ``Read`` tool returns images inside a
``tool_result`` block. The OpenAI ``tool`` role cannot carry image parts, so
those images must be forwarded on a following ``user`` message rather than
being silently dropped (which is what the gateway used to do).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.anthropic_adapter import anthropic_to_openai, has_image_content

# A tiny valid 1x1 PNG, base64-encoded.
PNG_DATA = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _tool_result_request(tr_content):
    return {
        "model": "claude-sonnet-4",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_abc",
                        "content": tr_content,
                    }
                ],
            }
        ],
    }


def _image_block():
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": PNG_DATA,
        },
    }


def _all_image_urls(payload):
    urls = []
    for msg in payload["messages"]:
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    urls.append(part["image_url"]["url"])
    return urls


def test_tool_result_image_is_not_dropped():
    req = _tool_result_request(
        [
            {"type": "text", "text": "Here is the screenshot:"},
            _image_block(),
        ]
    )
    payload = anthropic_to_openai(req)
    assert _all_image_urls(payload) == [f"data:image/png;base64,{PNG_DATA}"]


def test_tool_result_image_rides_on_user_message_not_tool_role():
    # OpenAI 'tool' role cannot carry image parts; they must move to a user msg.
    req = _tool_result_request([_image_block()])
    payload = anthropic_to_openai(req)

    for msg in payload["messages"]:
        if msg.get("role") == "tool":
            assert isinstance(msg["content"], str)  # no image parts on tool msg

    image_carriers = [
        m["role"]
        for m in payload["messages"]
        if isinstance(m.get("content"), list)
        and any(p.get("type") == "image_url" for p in m["content"])
    ]
    assert image_carriers == ["user"]


def test_tool_result_keeps_tool_message_with_id_and_text():
    req = _tool_result_request(
        [
            {"type": "text", "text": "Here is the screenshot:"},
            _image_block(),
        ]
    )
    payload = anthropic_to_openai(req)
    tool_msgs = [m for m in payload["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "toolu_abc"
    assert "Here is the screenshot:" in tool_msgs[0]["content"]


def test_tool_result_image_sets_vision_detectable_payload():
    # The whole point: has_image_content must now see the image so the
    # Copilot-Vision-Request header gets attached downstream.
    req = _tool_result_request([_image_block()])
    payload = anthropic_to_openai(req)
    assert has_image_content(payload) is True


def test_tool_result_text_only_is_unchanged():
    # Regression guard: a plain-text tool_result stays a single tool message
    # with a string body and no extra user message.
    req = _tool_result_request([{"type": "text", "text": "exit code 0"}])
    payload = anthropic_to_openai(req)
    assert len(payload["messages"]) == 1
    msg = payload["messages"][0]
    assert msg["role"] == "tool"
    assert msg["content"] == "exit code 0"


def test_tool_result_string_content_is_unchanged():
    # Regression guard: tool_result.content as a plain string stays a tool msg.
    req = _tool_result_request("plain string result")
    payload = anthropic_to_openai(req)
    assert len(payload["messages"]) == 1
    assert payload["messages"][0]["role"] == "tool"
    assert payload["messages"][0]["content"] == "plain string result"
