"""End-to-end: betas and gated fields against the live Copilot backend.

Covers both what should now get through (context_management with its header,
container, the verified betas) and the recovery path: an unknown beta is
forwarded optimistically, refused, learned from the upstream's own message,
and the request retried without it -- so the caller sees 200 rather than the
400 that a verbatim forward would have produced.

Needs HUB_DATA_DIR pointing at a database with a working OAuth token.
Costs a little quota.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from hub import anthropic_adapter as aa  # noqa: E402
from hub import server  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "claude-opus-5"
CM = {"edits": [{"type": "clear_tool_uses_20250919",
                 "trigger": {"type": "input_tokens", "value": 30000}}]}
GATE = "context-management-2025-06-27"
REFUSED = "advisor-tool-2026-03-01"          # upstream refuses this one
CC_HEADER = f"claude-code-20250219,{REFUSED},context-1m-2025-08-07,{GATE}"

failures = []


def why(r):
    try:
        b = r.json()
        e = b.get("error") if isinstance(b, dict) else None
        return ((e or {}).get("message") or json.dumps(b))[:66]
    except Exception:
        return r.text[:66]


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(label)


base = {"model": MODEL, "max_tokens": 8,
        "messages": [{"role": "user", "content": "hi"}]}

with TestClient(server.app) as c:
    def post(body, hdr=None, path="/v1/messages"):
        h = {"content-type": "application/json"}
        if hdr:
            h["anthropic-beta"] = hdr
        return c.post(path, json=body, headers=h)

    print("[1] fields")
    for label, body, hdr in [
        ("plain request", base, None),
        ("context_management + its header", {**base, "context_management": CM}, GATE),
        ("context_management, no header (stripped)", {**base, "context_management": CM}, None),
        ("container forwarded", {**base, "container": {"id": "abc"}}, None),
        ("mcp_servers stripped", {**base,
            "mcp_servers": [{"type": "url", "url": "https://x", "name": "n"}]}, None),
        ("fallbacks stripped", {**base, "fallbacks": ["claude-sonnet-5"]}, None),
    ]:
        r = post(body, hdr)
        check(f"{label}", r.status_code == 200, f"{r.status_code} {why(r)}")

    print("[2] the refused beta is learned, not fatal")
    aa.reset_learned_betas()
    check("starts with nothing learned", aa.refused_betas() == frozenset())
    r = post({**base, "context_management": CM}, CC_HEADER)
    check("real Claude Code header succeeds", r.status_code == 200,
          f"{r.status_code} {why(r)}")
    check(f"learned that {REFUSED} is refused", REFUSED in aa.refused_betas(),
          str(sorted(aa.refused_betas())))
    check("verified betas were not blamed",
          GATE not in aa.refused_betas() and "context-1m-2025-08-07" not in aa.refused_betas(),
          str(sorted(aa.refused_betas())))

    print("[3] the lesson sticks (second call needs no retry)")
    r = post({**base, "context_management": CM}, CC_HEADER)
    check("same header succeeds again", r.status_code == 200, str(r.status_code))
    check("nothing new learned", aa.refused_betas() == frozenset({REFUSED}),
          str(sorted(aa.refused_betas())))

    print("[4] streaming path recovers the same way")
    aa.reset_learned_betas()
    r = post({**base, "stream": True, "context_management": CM}, CC_HEADER)
    check("stream with a refused beta succeeds", r.status_code == 200,
          f"{r.status_code} {why(r)}")
    check("learned on the streaming path too", REFUSED in aa.refused_betas(),
          str(sorted(aa.refused_betas())))

    print("[5] count_tokens too")
    aa.reset_learned_betas()
    r = post({"model": MODEL, "messages": base["messages"], "context_management": CM},
             CC_HEADER, path="/v1/messages/count_tokens")
    check("count_tokens with a refused beta succeeds", r.status_code == 200,
          f"{r.status_code} {why(r)}")

    print("[6] a non-beta 400 still reaches the caller unchanged")
    aa.reset_learned_betas()
    r = post({**base, "model": "definitely-not-a-model"})
    check("bad model still 400", r.status_code == 400, str(r.status_code))
    check("nothing learned from it", aa.refused_betas() == frozenset(),
          str(sorted(aa.refused_betas())))

print()
print("PASSED" if not failures else f"FAILED: {failures}")
raise SystemExit(1 if failures else 0)
