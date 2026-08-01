"""Request helpers for the Anthropic-shaped endpoints.

``/v1/messages`` is forwarded to Copilot byte-for-byte rather than translated:
the upstream serves Claude models natively in the Anthropic shape, so
rebuilding the request could only lose fields. What remains here is what
passthrough still needs — mapping public model IDs onto Copilot slugs,
dropping the few fields the upstream rejects, detecting image content for the
Vision header, and estimating input tokens when a stream omits them.

The OpenAI<->Anthropic translation layer this module used to carry was removed
once passthrough landed and it had no callers left. Recover it from git
history if the upstream ever stops serving Claude in the native shape.
"""
from __future__ import annotations

import json
from typing import Any


# --------------------------------------------------------------------------- #
# Model name mapping
# --------------------------------------------------------------------------- #
# Anthropic-native clients (Claude Code, the Anthropic SDK, etc.) often send
# the official public model IDs, but the Copilot backend only knows its own
# slugs. Map the well-known official names onto the Copilot equivalents so a
# client configured against api.anthropic.com works unchanged. Unknown names
# are passed through verbatim (so Copilot-native slugs still work).
# Verified against the live upstream model list. Only these Claude slugs are
# actually served; requesting anything else returns model_not_supported.
#   claude-opus-5 / 4.8 / 4.7 / 4.6
#   claude-sonnet-5 / 4.6 / 4.5
#   claude-haiku-4.5
MODEL_ALIASES: dict[str, str] = {
    # Claude Opus
    "claude-opus-4-5": "claude-opus-4.5",
    "claude-opus-4-6": "claude-opus-4.6",
    "claude-opus-4-7": "claude-opus-4.7",
    "claude-opus-4-8": "claude-opus-4.8",
    "claude-opus-5": "claude-opus-5",
    "claude-opus-latest": "claude-opus-5",
    # Claude Sonnet
    "claude-sonnet-4-5": "claude-sonnet-4.5",
    "claude-sonnet-4-5-20250929": "claude-sonnet-4.5",
    "claude-sonnet-4-6": "claude-sonnet-4.6",
    "claude-sonnet-5": "claude-sonnet-5",
    "claude-sonnet-latest": "claude-sonnet-5",
    # Claude Haiku
    "claude-haiku-4-5": "claude-haiku-4.5",
    "claude-3-5-haiku-20241022": "claude-haiku-4.5",
    "claude-3-5-haiku-latest": "claude-haiku-4.5",
    # Retired models -> nearest served equivalent, so older clients keep working
    # rather than hard-failing with model_not_supported.
    "claude-3-5-sonnet-20240620": "claude-sonnet-4.5",
    "claude-3-5-sonnet-20241022": "claude-sonnet-4.5",
    "claude-3-5-sonnet-latest": "claude-sonnet-4.5",
    "claude-3-7-sonnet-20250219": "claude-sonnet-4.5",
    "claude-3-7-sonnet-latest": "claude-sonnet-4.5",
    "claude-3.5-sonnet": "claude-sonnet-4.5",
    "claude-3.7-sonnet": "claude-sonnet-4.5",
    "claude-3.5-haiku": "claude-haiku-4.5",
    "claude-sonnet-4-20250514": "claude-sonnet-4.5",
    "claude-sonnet-4": "claude-sonnet-4.5",
    "claude-opus-4-20250514": "claude-opus-4.6",
    "claude-opus-4-1-20250805": "claude-opus-4.6",
    "claude-opus-4": "claude-opus-4.6",
    "claude-opus-4.1": "claude-opus-4.6",
}


def map_model(name: str | None) -> str | None:
    """Translate an official Anthropic model ID to its Copilot slug.

    Matching is exact first, then a couple of tolerant fallbacks (strip a
    trailing date suffix). Unknown names pass through unchanged so that
    Copilot-native slugs keep working.
    """
    if not name:
        return name
    if name in MODEL_ALIASES:
        return MODEL_ALIASES[name]
    # Tolerant fallback: "claude-...-YYYYMMDD" -> try without the date.
    if "-20" in name:
        base = name.rsplit("-20", 1)[0]
        if base in MODEL_ALIASES:
            return MODEL_ALIASES[base]
    return name


def has_image_content(openai_payload: dict[str, Any]) -> bool:
    """True if any message in an OpenAI-shaped payload carries an image part."""
    for msg in openai_payload.get("messages") or []:
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
    return False


# Request fields the Anthropic API accepts but the Copilot backend rejects with
# 400 "Extra inputs are not permitted". Verified against the live upstream --
# see scripts/probe_rejected_fields.py, which re-runs the check.
#
# This matters only because we pass the body through verbatim. The old
# translation layer rebuilt the request from the fields it understood, so it
# dropped these as a side effect; passthrough forwards them and the upstream
# refuses the whole request.
#
# `context_management` is the one that actually bites: Claude Code sends it on
# every turn, so leaving it in makes the client fail 100% of the time. The
# others are included because they fail the same way if a client sends them.
UNSUPPORTED_UPSTREAM_FIELDS = (
    "context_management",
    "fallbacks",
    "container",
    "mcp_servers",
)


def strip_unsupported(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Drop request fields the Copilot backend cannot accept.

    Returns ``(payload, dropped)``. The input is never mutated; when nothing
    needs dropping the original object is returned unchanged so the common path
    stays allocation-free.

    Dropping is the right trade-off here: these fields tune behaviour
    (context editing, server-side fallbacks, MCP) rather than determining the
    answer, so a request that ignores them still returns a correct response --
    whereas forwarding them returns nothing at all.
    """
    dropped = [f for f in UNSUPPORTED_UPSTREAM_FIELDS if f in payload]
    if not dropped:
        return payload, []
    return {k: v for k, v in payload.items() if k not in dropped}, dropped


def has_image_content_anthropic(payload: dict[str, Any]) -> bool:
    """True if an Anthropic-shaped payload carries an image block anywhere.

    Images may sit directly in a message's content list or nested inside a
    ``tool_result`` block's own content list, so this recurses one level.
    Used to set the ``Copilot-Vision-Request`` header on passthrough, where
    the body is never converted to the OpenAI shape.
    """
    def _scan(blocks: Any) -> bool:
        if not isinstance(blocks, list):
            return False
        for part in blocks:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image":
                return True
            # tool_result carries its own nested content list.
            if part.get("type") == "tool_result" and _scan(part.get("content")):
                return True
        return False

    for msg in payload.get("messages") or []:
        if isinstance(msg, dict) and _scan(msg.get("content")):
            return True
    return _scan(payload.get("system"))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def estimate_tokens(text: str) -> int:
    """Very rough fallback token estimate (~4 chars/token)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def estimate_prompt_tokens(openai_payload: dict[str, Any]) -> int:
    """Rough input-token estimate for an OpenAI-shaped request.

    Used as a fallback when the backend does not report ``prompt_tokens``
    (common with streaming Copilot responses). Walks every message's content
    — plain strings, text parts, and tool-call arguments — plus tool schemas.
    """
    total = 0
    for msg in openai_payload.get("messages") or []:
        content = msg.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    total += estimate_tokens(part.get("text", ""))
                elif part.get("type") == "image_url":
                    # Images cost real tokens; charge a flat rough amount
                    # rather than measuring the (possibly huge) data URL.
                    total += 600
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {}) or {}
            total += estimate_tokens(fn.get("name", ""))
            total += estimate_tokens(fn.get("arguments", "") or "")
    # Tool definitions are part of the prompt too.
    for tool in openai_payload.get("tools") or []:
        fn = tool.get("function", {}) or {}
        total += estimate_tokens(fn.get("name", ""))
        total += estimate_tokens(fn.get("description", ""))
        try:
            total += estimate_tokens(json.dumps(fn.get("parameters", {})))
        except (TypeError, ValueError):
            pass
    return total
