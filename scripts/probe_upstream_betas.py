"""Probe which beta headers and beta-gated fields the Copilot backend accepts.

Talks to the Copilot API directly, bypassing the hub's own stripping, so the
answers describe the backend rather than our handling of it. The backend
validates `anthropic-beta` all-or-nothing -- one unrecognised entry fails the
whole request -- so each beta has to be sent alone to learn anything about it.

Needs a database holding a working OAuth token; pass its directory.
Sends small max_tokens requests, so it costs a little quota.

Run:  HUB_DATA_DIR=/tmp/liangdb python scripts/probe_upstream_betas.py <model>
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub import copilot_client as cc  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "claude-opus-5"

BASE = {
    "model": MODEL,
    "max_tokens": 8,
    "messages": [{"role": "user", "content": "hi"}],
}
CTX_MGMT = {
    "edits": [{
        "type": "clear_tool_uses_20250919",
        "trigger": {"type": "input_tokens", "value": 30000},
    }]
}
CANDIDATE_BETAS = [
    "claude-code-20250219",
    "context-1m-2025-08-07",
    "context-management-2025-06-27",
    "effort-2025-11-24",
    "fallback-credit-2026-06-01",
    "interleaved-thinking-2025-05-14",
    "mid-conversation-system-2026-04-07",
    "prompt-caching-scope-2026-01-05",
    "thinking-token-count-2026-05-13",
    "advisor-tool-2026-03-01",
]


def why(body: dict) -> str:
    err = body.get("error") if isinstance(body, dict) else None
    msg = (err or {}).get("message") if isinstance(err, dict) else None
    return (msg or json.dumps(body))[:88].replace("\n", " ")


async def send(payload: dict, beta: str | None = None) -> tuple[int, str]:
    hdr = {"anthropic-beta": beta} if beta else None
    status, body = await cc.post_json("/v1/messages", payload, hdr)
    return status, ("ok" if status == 200 else why(body))


async def main() -> None:
    print(f"model: {MODEL}\n")

    print("[1] baseline")
    s, d = await send(BASE)
    print(f"  {s}  plain request                              {d}")
    if s != 200:
        print("\n  baseline failed -- the rest would be meaningless")
        return

    print("\n[2] context_management, with and without its beta header")
    s_no, d_no = await send({**BASE, "context_management": CTX_MGMT})
    print(f"  {s_no}  context_management, NO header             {d_no}")
    s_yes, d_yes = await send({**BASE, "context_management": CTX_MGMT},
                              "context-management-2025-06-27")
    print(f"  {s_yes}  context_management + header               {d_yes}")

    print("\n[3] each candidate beta sent alone (header only, no gated field)")
    accepted, refused = [], []
    for b in CANDIDATE_BETAS:
        s, d = await send(BASE, b)
        (accepted if s == 200 else refused).append(b)
        print(f"  {s}  {b:<36} {d}")

    print("\n[4] all accepted betas together")
    if accepted:
        s, d = await send(BASE, ",".join(accepted))
        print(f"  {s}  {len(accepted)} betas at once                       {d}")

    print("\n[5] one refused beta poisons the whole header")
    if refused and accepted:
        s, d = await send(BASE, ",".join(accepted + refused[:1]))
        print(f"  {s}  accepted + {refused[0]}  {d}")

    print("\n" + "=" * 72)
    print(f"accepted ({len(accepted)}):")
    for b in accepted:
        print(f"    {b}")
    print(f"refused  ({len(refused)}):")
    for b in refused:
        print(f"    {b}")
    print()
    print(f"context_management is beta-gated: "
          f"{s_no != 200 and s_yes == 200}   (no-header={s_no}, with-header={s_yes})")


asyncio.run(main())
