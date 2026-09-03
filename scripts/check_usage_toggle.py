"""Check the copilot_usage toggle against the live upstream.

The block is what billing is computed from, so the one thing that must not
happen is hiding it from the client and losing the charge with it. Every case
below therefore checks both halves: what the caller receives, and what the
usage row records.

Needs HUB_DATA_DIR with a working OAuth token. Costs a little quota.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from hub import server, store  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "claude-opus-5"
failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def last_row():
    rows = store.recent_usage(limit=1)
    return rows[0] if rows else {}


def _ok(line: str) -> bool:
    p = line[5:].strip()
    if not p or p == "[DONE]":
        return True
    try:
        json.loads(p)
        return True
    except json.JSONDecodeError:
        return False


def sse_has_usage(text: str) -> bool:
    for line in text.split("\n"):
        if not line.startswith("data:"):
            continue
        p = line[5:].strip()
        if not p or p == "[DONE]":
            continue
        try:
            o = json.loads(p)
        except json.JSONDecodeError:
            continue
        if not isinstance(o, dict):
            continue
        for cand in (o, o.get("message"), o.get("response")):
            if isinstance(cand, dict) and "copilot_usage" in cand:
                return True
    return False


body = {"model": MODEL, "max_tokens": 8,
        "messages": [{"role": "user", "content": "hi"}]}
original = None

with TestClient(server.app) as c:
    original = store.get_expose_copilot_usage()

    for expose in (True, False):
        store.set_expose_copilot_usage(expose)
        state = "exposed" if expose else "stripped"
        print(f"[{state}]")

        r = c.post("/v1/messages", json=dict(body))
        seen = "copilot_usage" in (r.json() if r.status_code == 200 else {})
        check(f"buffered: block {'present' if expose else 'absent'}",
              r.status_code == 200 and seen == expose, f"{r.status_code}, seen={seen}")
        row = last_row()
        check("buffered: billing still recorded",
              bool(row.get("nano_aiu")) or bool(row.get("equivalent_cost")),
              f"nano={row.get('nano_aiu')} cost={row.get('equivalent_cost')}")
        check("buffered: response still valid JSON with content",
              bool((r.json() or {}).get("content")) if r.status_code == 200 else False)

        r = c.post("/v1/messages", json=dict(body, stream=True))
        text = r.text
        seen = sse_has_usage(text)
        check(f"streamed: block {'present' if expose else 'absent'}",
              r.status_code == 200 and seen == expose, f"{r.status_code}, seen={seen}")
        row = last_row()
        check("streamed: billing still recorded",
              bool(row.get("nano_aiu")) or bool(row.get("equivalent_cost")),
              f"nano={row.get('nano_aiu')} cost={row.get('equivalent_cost')}")
        check("streamed: stream still well formed",
              text.count("event: ") >= 2 and "message_stop" in text,
              f"{text.count('event: ')} events")
        check("streamed: every data line still parses",
              all(_ok(l) for l in text.split("\n") if l.startswith("data:")))
        print()

    # A hub that never had the setting must behave as it always did.
    store.set_expose_copilot_usage(True)
    check("default is to expose", store.get_expose_copilot_usage() is True)

if original is not None:
    store.set_expose_copilot_usage(original)
    print(f"restored expose_copilot_usage = {original}")

print()
print("PASSED" if not failures else f"FAILED: {failures}")
raise SystemExit(1 if failures else 0)
