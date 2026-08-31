"""Probe which request fields the Copilot backend accepts, with and without betas.

A 400 alone does not say why a field was refused: "Extra inputs are not
permitted" appears both for a field the backend lacks entirely and for one it
gates behind a beta header it was not given. Only sending the field both ways
separates the two, and the difference decides whether the field should be
dropped forever or merely dropped when un-headered.

Needs a database holding a working OAuth token; pass its directory in
HUB_DATA_DIR. Sends small max_tokens requests, so it costs a little quota.

Run:  HUB_DATA_DIR=/tmp/liangdb python scripts/probe_upstream_fields.py <model>
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub import copilot_client as cc  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "claude-opus-5"
BASE = {"model": MODEL, "max_tokens": 8,
        "messages": [{"role": "user", "content": "hi"}]}

# Each field with the beta header Anthropic documents for it. The header is
# what turns "unsupported" into "supported" when the backend does have it.
FIELDS: list[tuple[str, object, str]] = [
    ("context_management",
     {"edits": [{"type": "clear_tool_uses_20250919",
                 "trigger": {"type": "input_tokens", "value": 30000}}]},
     "context-management-2025-06-27"),
    ("fallbacks", ["claude-sonnet-5"], "fallback-credit-2026-06-01"),
    ("container", {"id": "test"}, "code-execution-2025-08-25,skills-2025-10-02"),
    ("mcp_servers",
     [{"type": "url", "url": "https://example.com/mcp", "name": "t"}],
     "mcp-client-2025-11-20"),
]


def why(body: object) -> str:
    err = body.get("error") if isinstance(body, dict) else None
    msg = (err or {}).get("message") if isinstance(err, dict) else None
    return (msg or json.dumps(body))[:76].replace("\n", " ")


async def send(payload: dict, beta: str | None = None) -> tuple[int, str]:
    hdr = {"anthropic-beta": beta} if beta else None
    status, body = await cc.post_json("/v1/messages", payload, hdr)
    return status, ("ok" if status == 200 else why(body))


async def main() -> None:
    print(f"model: {MODEL}\n")
    s, d = await send(BASE)
    print(f"[baseline] {s}  {d}")
    if s != 200:
        return

    print(f"\n  {'field':<20} {'no header':<11} {'with its beta header'}")
    print("  " + "-" * 72)
    verdicts: dict[str, str] = {}
    for name, value, beta in FIELDS:
        s_no, d_no = await send({**BASE, name: value})
        s_yes, d_yes = await send({**BASE, name: value}, beta)
        if s_yes == 200 and s_no != 200:
            v = "BETA-GATED -- forward when the caller sends the header"
        elif s_yes == 200 and s_no == 200:
            v = "always accepted -- no need to strip at all"
        else:
            v = "genuinely unsupported -- keep stripping"
        verdicts[name] = v
        print(f"  {name:<20} {s_no:<11} {s_yes}")
        print(f"  {'':<20} {d_no[:46]:<46} {d_yes[:40]}")
        print(f"  {'':<20} -> {v}")
        print()

    print("=" * 74)
    for name, v in verdicts.items():
        print(f"  {name:<20} {v}")


asyncio.run(main())
