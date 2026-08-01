"""End-to-end verification of the /v1/messages passthrough + nano_aiu billing.

Runs the real FastAPI app in-process against the live Copilot backend.

Run:  python -m scripts.test_passthrough
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import sys

import httpx

# Isolate from the production DB *before* importing anything that reads
# settings. HUB_DATA_DIR is the real knob (not DB_PATH); getting this wrong
# silently writes test traffic into the live usage table, so assert on it.
os.environ["HUB_DATA_DIR"] = os.environ.get("HUB_TEST_DATA_DIR", "/tmp/gm_test_data")

from hub import copilot_client as cc
from hub import server as srv
from hub import store
from hub.config import get_settings

if "gm_test" not in str(get_settings().db_path):
    raise SystemExit(f"refusing to run: DB is not isolated -> {get_settings().db_path}")


def seed_oauth_token() -> None:
    """Copy the OAuth token from the production DB into the isolated test DB.

    Usage rows stay in the test DB; only the credential is borrowed, so real
    traffic accounting is never polluted by these runs.
    """
    import sqlite3
    from pathlib import Path

    if os.environ.get("COPILOT_OAUTH_TOKEN") or store.get_oauth_token():
        return
    prod = Path(__file__).resolve().parent.parent / "db" / "hub.db"
    if not prod.exists():
        raise SystemExit(f"no production DB to borrow the OAuth token from: {prod}")
    conn = sqlite3.connect(prod)
    try:
        row = conn.execute("SELECT value FROM kv WHERE key='oauth_token'").fetchone()
    finally:
        conn.close()
    if not row:
        raise SystemExit("production DB has no oauth_token; log in first")
    store.set_oauth_token(row[0])

MODEL = "claude-opus-5"
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))


def png_1x1() -> str:
    """A minimal valid 1x1 red PNG, base64-encoded."""
    raw = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    return base64.b64encode(raw).decode()


def bigger_png() -> str:
    """A 8x8 image with visible content, so the model has something to describe."""
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        return png_1x1()
    img = Image.new("RGB", (64, 64), (255, 0, 0))
    for x in range(64):
        for y in range(32):
            img.putpixel((x, y), (0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


async def client() -> httpx.AsyncClient:
    key = store.list_api_keys()
    transport = httpx.ASGITransport(app=srv.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test", timeout=180)


def auth_headers() -> dict[str, str]:
    keys = [k for k in store.list_api_keys() if not k.get("revoked")]
    if keys:
        full = store.get_full_key(keys[0]["key"]) if hasattr(store, "get_full_key") else None
        return {"Authorization": f"Bearer {full or keys[0]['key']}"}
    return {}


def rows_before() -> int:
    # Follow store's table constant rather than hard-coding a name: usage rows
    # moved to usage_v2 when the two-table split landed, and a literal here
    # would silently read the (now frozen) legacy table instead.
    with store._conn() as c:
        return c.execute(
            f"SELECT COUNT(*) n FROM {store.USAGE_TABLE}"
        ).fetchone()["n"]


def last_row() -> dict:
    with store._conn() as c:
        r = c.execute(
            f"SELECT * FROM {store.USAGE_TABLE} ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(r) if r else {}


# --------------------------------------------------------------------------- #
async def t_nonstream(c: httpx.AsyncClient, h: dict) -> None:
    print("\n[1] non-stream passthrough (native Anthropic shape)")
    r = await c.post("/v1/messages", headers=h, json={
        "model": MODEL, "max_tokens": 64,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
    })
    check("http 200", r.status_code == 200, f"got {r.status_code} {r.text[:200]}")
    if r.status_code != 200:
        return
    d = r.json()
    check("type == message", d.get("type") == "message", str(d.get("type")))
    check("content is block list", isinstance(d.get("content"), list) and
          d["content"][0].get("type") == "text", json.dumps(d.get("content"))[:120])
    check("has stop_reason", bool(d.get("stop_reason")), str(d.get("stop_reason")))
    check("usage.input_tokens real", (d.get("usage") or {}).get("input_tokens", 0) > 0,
          json.dumps(d.get("usage"))[:120])
    check("copilot_usage preserved", "copilot_usage" in d)

    row = last_row()
    check("row recorded with nano_aiu", row.get("nano_aiu", 0) > 0, f"nano_aiu={row.get('nano_aiu')}")
    check("row not marked estimated", row.get("estimated") == 0)
    check("row input matches response",
          row.get("input_tokens") == (d.get("usage") or {}).get("input_tokens"),
          f"row={row.get('input_tokens')} resp={(d.get('usage') or {}).get('input_tokens')}")
    cost = row.get("nano_aiu", 0) / store.NANO_AIU_PER_USD
    print(f"      -> cost from nano_aiu: ${cost:.6f}")


async def t_stream(c: httpx.AsyncClient, h: dict) -> None:
    print("\n[2] streaming passthrough (native Anthropic SSE)")
    events, sawdone = [], False
    async with c.stream("POST", "/v1/messages", headers=h, json={
        "model": MODEL, "max_tokens": 100, "stream": True,
        "messages": [{"role": "user", "content": "Count to three."}],
    }) as r:
        check("http 200", r.status_code == 200, str(r.status_code))
        async for line in r.aiter_lines():
            if line.startswith("event:"):
                events.append(line.split(":", 1)[1].strip())
            if line.strip() == "data: [DONE]":
                sawdone = True
    need = ["message_start", "content_block_start", "content_block_delta",
            "content_block_stop", "message_delta", "message_stop"]
    for e in need:
        check(f"event {e}", e in events)
    check("event order sane", events and events[0] == "message_start" and
          events[-1] == "message_stop", f"first={events[:1]} last={events[-1:]}")
    print(f"      -> [DONE] trailer present: {sawdone} (non-standard, from upstream)")

    row = last_row()
    check("stream row has nano_aiu", row.get("nano_aiu", 0) > 0, f"nano_aiu={row.get('nano_aiu')}")
    check("stream row has real input", row.get("input_tokens", 0) > 0,
          f"input={row.get('input_tokens')}")
    check("stream row not estimated", row.get("estimated") == 0)


async def t_image(c: httpx.AsyncClient, h: dict) -> None:
    print("\n[3] IMAGE — anthropic native image block (non-stream)")
    b64 = bigger_png()
    body = {
        "model": MODEL, "max_tokens": 100,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
             "media_type": "image/png", "data": b64}},
            {"type": "text", "text": "What are the two colors in this image? Answer in 5 words."},
        ]}],
    }
    check("vision header detected", srv.aa.has_image_content_anthropic(body) is True)
    r = await c.post("/v1/messages", headers=h, json=body)
    check("http 200", r.status_code == 200, f"got {r.status_code} {r.text[:300]}")
    if r.status_code != 200:
        return
    d = r.json()
    txt = " ".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
    check("model saw the image", len(txt.strip()) > 0, f"said: {txt[:100]!r}")
    print(f"      -> model said: {txt[:150]!r}")
    row = last_row()
    check("image row billed", row.get("nano_aiu", 0) > 0, f"nano_aiu={row.get('nano_aiu')}")


async def t_image_stream(c: httpx.AsyncClient, h: dict) -> None:
    print("\n[4] IMAGE — streaming")
    b64 = bigger_png()
    text = ""
    async with c.stream("POST", "/v1/messages", headers=h, json={
        "model": MODEL, "max_tokens": 100, "stream": True,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
             "media_type": "image/png", "data": b64}},
            {"type": "text", "text": "Name the colors. 5 words max."},
        ]}],
    }) as r:
        check("http 200", r.status_code == 200, str(r.status_code))
        async for line in r.aiter_lines():
            if line.startswith("data:"):
                p = line[5:].strip()
                if p and p != "[DONE]":
                    try:
                        o = json.loads(p)
                        if o.get("type") == "content_block_delta":
                            text += (o.get("delta") or {}).get("text", "")
                    except json.JSONDecodeError:
                        pass
    check("streamed image response non-empty", len(text.strip()) > 0, repr(text[:80]))
    print(f"      -> model said: {text[:150]!r}")


async def t_tool_use(c: httpx.AsyncClient, h: dict) -> None:
    print("\n[5] tool_use round-trip (the old translator's weak spot)")
    tools = [{
        "name": "get_weather",
        "description": "Get the weather for a city",
        "input_schema": {"type": "object",
                         "properties": {"city": {"type": "string"}},
                         "required": ["city"]},
    }]
    r = await c.post("/v1/messages", headers=h, json={
        "model": MODEL, "max_tokens": 200, "tools": tools,
        "messages": [{"role": "user", "content": "What's the weather in Paris? Use the tool."}],
    })
    check("http 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    if r.status_code != 200:
        return
    d = r.json()
    tu = [b for b in d.get("content", []) if b.get("type") == "tool_use"]
    check("returned a tool_use block", bool(tu), json.dumps(d.get("content"))[:200])
    if not tu:
        return
    check("tool name intact", tu[0].get("name") == "get_weather", str(tu[0].get("name")))
    check("tool input parsed", isinstance(tu[0].get("input"), dict), str(tu[0].get("input")))
    check("stop_reason == tool_use", d.get("stop_reason") == "tool_use", str(d.get("stop_reason")))

    # Feed the result back.
    r2 = await c.post("/v1/messages", headers=h, json={
        "model": MODEL, "max_tokens": 150, "tools": tools,
        "messages": [
            {"role": "user", "content": "What's the weather in Paris? Use the tool."},
            {"role": "assistant", "content": d["content"]},
            {"role": "user", "content": [{"type": "tool_result",
             "tool_use_id": tu[0]["id"], "content": "18C and sunny"}]},
        ],
    })
    check("tool_result round-trip 200", r2.status_code == 200, f"{r2.status_code} {r2.text[:200]}")
    if r2.status_code == 200:
        t2 = " ".join(b.get("text", "") for b in r2.json().get("content", [])
                      if b.get("type") == "text")
        check("final answer mentions result", "18" in t2 or "sunny" in t2.lower(), repr(t2[:100]))


async def t_cache_billing(c: httpx.AsyncClient, h: dict) -> None:
    print("\n[6] cache_write billing (previously under-counted 12x)")
    # The upstream prompt cache is server-side with a ~5min TTL, so a fixed
    # prefix would still be warm from an earlier run and the "first" call
    # would be a cache READ. Salt it so this run is guaranteed to be a miss.
    salt = base64.b64encode(os.urandom(12)).decode()
    big = f"Session {salt}. " + "The quick brown fox jumps over the lazy dog. " * 400
    body = {
        "model": MODEL, "max_tokens": 24,
        "system": [{"type": "text", "text": big,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": "reply OK"}],
    }
    r1 = await c.post("/v1/messages", headers=h, json=body)
    check("first call 200", r1.status_code == 200, str(r1.status_code))
    row1 = last_row()
    r2 = await c.post("/v1/messages", headers=h, json=body)
    check("second call 200", r2.status_code == 200, str(r2.status_code))
    row2 = last_row()

    check("cache_write recorded on first call", row1.get("cache_write_tokens", 0) > 0,
          f"cache_write={row1.get('cache_write_tokens')}")
    check("cache_read recorded on second call", row2.get("cached_tokens", 0) > 0,
          f"cache_read={row2.get('cached_tokens')}")
    c1 = row1.get("nano_aiu", 0) / store.NANO_AIU_PER_USD
    c2 = row2.get("nano_aiu", 0) / store.NANO_AIU_PER_USD
    check("cache hit is cheaper", c2 < c1, f"${c1:.6f} -> ${c2:.6f}")
    print(f"      -> write ${c1:.6f}  read ${c2:.6f}  ratio {c1/max(c2,1e-12):.1f}x")


async def t_openai_still_works(c: httpx.AsyncClient, h: dict) -> None:
    print("\n[7] /v1/chat/completions still OK + zero-rate fallback")
    r = await c.post("/v1/chat/completions", headers=h, json={
        "model": MODEL, "max_tokens": 32,
        "messages": [{"role": "user", "content": "say OK"}],
    })
    check("claude via openai shape 200", r.status_code == 200, str(r.status_code))
    check("openai shape intact", "choices" in (r.json() if r.status_code == 200 else {}))
    row = last_row()
    check("billed via nano_aiu", row.get("nano_aiu", 0) > 0, f"nano_aiu={row.get('nano_aiu')}")

    r2 = await c.post("/v1/chat/completions", headers=h, json={
        "model": "gpt-4.1", "max_tokens": 32,
        "messages": [{"role": "user", "content": "say OK"}],
    })
    check("gpt-4.1 200", r2.status_code == 200, str(r2.status_code))
    row2 = last_row()
    check("gpt-4.1 has zero nano_aiu (quota-billed)", row2.get("nano_aiu", 0) == 0,
          f"nano_aiu={row2.get('nano_aiu')}")
    check("gpt-4.1 still has tokens for fallback pricing",
          row2.get("input_tokens", 0) > 0, f"input={row2.get('input_tokens')}")


async def t_cost_aggregation() -> None:
    print("\n[8] cost aggregation — no double counting")
    data = store.usage_summary_with_cost(None, None)
    totals = data["totals"]
    total = totals["equivalent_cost"]
    check("total equivalent_cost > 0", total > 0, f"${total}")
    check("legacy cost alias intact", totals.get("cost") == total)
    check("quota_requests reported", "quota_requests" in totals,
          str(totals.get("quota_requests")))
    manual = 0.0
    pricing = data["pricing"]
    for row in data["by_model"]:
        manual += store.row_equivalent_cost(pricing, row)
    check("by_model sums to total", abs(manual - total) < 0.01,
          f"{manual:.4f} vs {total:.4f}")

    for row in data["by_model"]:
        nano_part = (row.get("nano_aiu") or 0) / store.NANO_AIU_PER_USD
        fb_tokens = (row.get("fb_input_tokens", 0) + row.get("fb_output_tokens", 0)
                     + row.get("fb_cache_write_tokens", 0))
        print(f"      {row['model']:26} reqs={row['requests']:3}  "
              f"nano=${nano_part:.6f}  fb_tokens={fb_tokens:<7} "
              f"equiv=${row['equivalent_cost']:.6f}  quota={row['quota_requests']}")
        # Double-counting is a question about which token buckets were used,
        # not about arithmetic on the rounded dollar figure: equivalent_cost is
        # rounded to 4dp, so subtracting the nano part back out of it yields
        # rounding noise (routinely negative) at these magnitudes, not a real
        # fallback contribution. Assert on the buckets themselves.
        if row.get("nano_aiu"):
            check(f"{row['model']}: no fallback tokens", fb_tokens == 0,
                  f"fb_tokens={fb_tokens}")
        # The two billing modes are mutually exclusive by construction.
        check(f"{row['model']}: not billed both ways",
              not (row["equivalent_cost"] > 0 and row["quota_requests"] > 0),
              f"equiv={row['equivalent_cost']} quota={row['quota_requests']}")


async def t_model_alias(c: httpx.AsyncClient, h: dict) -> None:
    print("\n[9] model alias mapping (official IDs -> copilot slugs)")
    from hub import anthropic_adapter as aa
    for src, want in [("claude-sonnet-4-5-20250929", "claude-sonnet-4.5"),
                      ("claude-opus-5", "claude-opus-5"),
                      ("claude-3-5-sonnet-20241022", "claude-sonnet-4.5")]:
        check(f"{src} -> {want}", aa.map_model(src) == want, aa.map_model(src))
    r = await c.post("/v1/messages", headers=h, json={
        "model": "claude-sonnet-4-5-20250929", "max_tokens": 24,
        "messages": [{"role": "user", "content": "say OK"}],
    })
    check("dated official ID works end-to-end", r.status_code == 200,
          f"{r.status_code} {r.text[:150]}")


async def main() -> None:
    store.init_db()
    seed_oauth_token()
    cc._api_token_mem.clear()
    h = auth_headers()
    print(f"auth header: {'yes' if h else 'NONE (auth may be disabled)'}")
    transport = httpx.ASGITransport(app=srv.app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test", timeout=300) as c:
        await t_nonstream(c, h)
        await t_stream(c, h)
        await t_image(c, h)
        await t_image_stream(c, h)
        await t_tool_use(c, h)
        await t_cache_billing(c, h)
        await t_openai_still_works(c, h)
        await t_cost_aggregation()
        await t_model_alias(c, h)

    print(f"\n{'='*60}\nPASSED {len(PASS)}   FAILED {len(FAIL)}")
    if FAIL:
        print("failures:")
        for f in FAIL:
            print("  -", f)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
