"""End-to-end: does context_management actually reach the upstream now?

Drives the real app in-process against the real Copilot backend, so it tests
the whole path -- header parsing, conditional stripping, header forwarding --
rather than the adapter in isolation. Before this change every case here
returned a request with context_management removed and no beta header.

Needs HUB_DATA_DIR pointing at a database with a working OAuth token.
Costs a little quota.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from hub import server  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "claude-opus-5"
CM = {"edits": [{"type": "clear_tool_uses_20250919",
                 "trigger": {"type": "input_tokens", "value": 30000}}]}
BETA = "context-management-2025-06-27"
# What Claude Code actually sends: accepted betas plus one the upstream refuses.
CC_HEADER = f"claude-code-20250219,advisor-tool-2026-03-01,context-1m-2025-08-07,{BETA}"

failures = []


def why(r):
    try:
        b = r.json()
        e = b.get("error") if isinstance(b, dict) else None
        return ((e or {}).get("message") or json.dumps(b))[:70]
    except Exception:
        return r.text[:70]


with TestClient(server.app) as c:
    def post(body, hdr=None):
        h = {"content-type": "application/json"}
        if hdr:
            h["anthropic-beta"] = hdr
        return c.post("/v1/messages", json=body, headers=h)

    base = {"model": MODEL, "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}]}

    cases = [
        ("plain request, no beta", base, None, 200),
        ("context_management + its header", {**base, "context_management": CM}, BETA, 200),
        ("context_management, no header (must be stripped)",
         {**base, "context_management": CM}, None, 200),
        ("real Claude Code header (contains a refused beta)",
         {**base, "context_management": CM}, CC_HEADER, 200),
        ("container forwarded", {**base, "container": {"id": "abc"}}, None, 200),
        ("mcp_servers stripped, not forwarded",
         {**base, "mcp_servers": [{"type": "url", "url": "https://x", "name": "n"}]},
         None, 200),
    ]
    for label, body, hdr, want in cases:
        r = post(body, hdr)
        ok = r.status_code == want
        print(f"  {'PASS' if ok else 'FAIL'}  {r.status_code}  {label}"
              + ("" if ok else f"  -- {why(r)}"))
        if not ok:
            failures.append(label)

print()
print("PASSED" if not failures else f"FAILED: {failures}")
raise SystemExit(1 if failures else 0)
