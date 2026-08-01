"""Find which Claude Code request fields the Copilot upstream rejects.

Passthrough forwards the body verbatim, so any parameter the upstream does not
recognise comes back as a 400 "Extra inputs are not permitted". The translation
layer used to hide this by rebuilding the request from only the fields it knew.
This probes each field Claude Code is known to send, one at a time.
"""
from __future__ import annotations

import asyncio
import json

from hub import copilot_client as cc

BASE = {
    "model": "claude-opus-5",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "hi"}],
}

# Fields Claude Code (and the modern Anthropic SDK) may attach.
CANDIDATES = {
    "context_management": {"edits": [{"type": "clear_tool_uses_20250919"}]},
    "metadata": {"user_id": "u1"},
    "system": "You are Claude Code.",
    "tool_choice": {"type": "auto"},
    "thinking": {"type": "adaptive"},
    "output_config": {"effort": "high"},
    "service_tier": "auto",
    "top_p": 0.9,
    "temperature": 1,
    "stop_sequences": ["STOP"],
    "speed": "fast",
    "fallbacks": "default",
    "container": {"skills": []},
    "mcp_servers": [],
}


async def main() -> None:
    cc._api_token_mem.clear()
    print(f"{'field':22} {'result'}")
    print("-" * 70)
    for field, value in CANDIDATES.items():
        body = {**BASE, field: value}
        try:
            status, data = await cc.post_json("/v1/messages", body)
        except Exception as e:  # noqa: BLE001
            print(f"{field:22} EXC {type(e).__name__}: {str(e)[:40]}")
            continue
        if status == 200:
            print(f"{field:22} OK")
        else:
            msg = ((data or {}).get("error") or {}).get("message", json.dumps(data))
            print(f"{field:22} {status}  {msg[:60]}")


asyncio.run(main())
