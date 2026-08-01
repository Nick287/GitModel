"""Compare hub output against the raw upstream response, per model per endpoint.

For every upstream model we issue the same request twice -- once through the
hub (in-process ASGI) and once straight at Copilot -- then diff the JSON
*structure*. Response text differs run to run, so comparing the shape (key
paths plus value types) is what actually tells us whether a client binding
against the upstream schema would still work against the hub.

Run:  python -m scripts.test_conformance
"""
from __future__ import annotations

import asyncio
import json
import os

import httpx

os.environ["HUB_DATA_DIR"] = os.environ.get("HUB_TEST_DATA_DIR", "/tmp/gm_test_data")

from hub import copilot_client as cc
from hub import server as srv
from hub import store
from hub.config import get_settings

if "gm_test" not in str(get_settings().db_path):
    raise SystemExit(f"refusing to run: DB not isolated -> {get_settings().db_path}")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    verdict = "PASS" if cond else "FAIL"
    (PASS if cond else FAIL).append(name)
    print(f"  {verdict}  {name}" + (f"  -- {detail}" if detail else ""))


def seed_token() -> None:
    """Borrow only the OAuth credential from the production DB."""
    import sqlite3
    from pathlib import Path

    if os.environ.get("COPILOT_OAUTH_TOKEN") or store.get_oauth_token():
        return
    prod = Path(__file__).resolve().parent.parent / "db" / "hub.db"
    conn = sqlite3.connect(prod)
    try:
        row = conn.execute("SELECT value FROM kv WHERE key='oauth_token'").fetchone()
    finally:
        conn.close()
    if not row:
        raise SystemExit("no oauth_token in production DB; log in first")
    store.set_oauth_token(row[0])


def shape(obj, path: str = "", out: set[str] | None = None, depth: int = 0) -> set[str]:
    """Flatten JSON into a set of ``path:type`` strings.

    List elements collapse onto a single ``[]`` path so that a one-item and a
    three-item list of the same element type compare equal -- we are checking
    the schema a client binds against, not the content length.
    """
    if out is None:
        out = set()
    if depth > 8:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            out.add(f"{p}:{type(v).__name__}")
            shape(v, p, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:3]:
            p = f"{path}[]"
            out.add(f"{p}:{type(v).__name__}")
            shape(v, p, out, depth + 1)
    return out


async def upstream(path: str, body: dict) -> tuple[int, dict]:
    token, endpoint = await cc.get_api_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        **cc.EDITOR_HEADERS,
    }
    if path == "/v1/messages":
        headers["anthropic-version"] = "2023-06-01"
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(f"{endpoint}{path}", headers=headers, json=body)
    try:
        return r.status_code, r.json()
    except json.JSONDecodeError:
        return r.status_code, {"_raw": r.text[:400]}


QUESTION = "Reply with exactly: OK"

HUB_PATH = {
    "/chat/completions": "/v1/chat/completions",
    "/responses": "/v1/responses",
    "/v1/messages": "/v1/messages",
}


def body_for(model: str, ep: str) -> dict:
    if ep == "/responses":
        # Reasoning models burn the budget on thinking before emitting text,
        # so this needs far more headroom than the chat shapes.
        return {"model": model, "max_output_tokens": 2000, "input": QUESTION}
    return {
        "model": model,
        "max_tokens": 40,
        "messages": [{"role": "user", "content": QUESTION}],
    }


def auth_headers() -> dict[str, str]:
    keys = [k for k in store.list_api_keys() if not k.get("revoked")]
    if not keys:
        return {}
    full = store.get_full_key(keys[0]["key"]) if hasattr(store, "get_full_key") else None
    return {"Authorization": f"Bearer {full or keys[0]['key']}"}


async def main() -> None:
    store.init_db()
    seed_token()
    cc._api_token_mem.clear()
    models = await cc.list_models()
    headers = auth_headers()

    transport = httpx.ASGITransport(app=srv.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=300
    ) as hub:
        for m in sorted(models, key=lambda x: str(x.get("id"))):
            mid = m.get("id")
            caps = m.get("capabilities") or {}
            eps = [e for e in (m.get("supported_endpoints") or []) if not e.startswith("ws:")]
            if not eps:
                # Older Azure models advertise no endpoint list but still serve
                # the classic chat shape.
                if caps.get("type") == "chat":
                    eps = ["/chat/completions"]
                else:
                    print(f"\n[{mid}] skip -- not a chat model")
                    continue
            for ep in eps:
                if ep not in HUB_PATH:
                    print(f"\n[{mid}] skip endpoint {ep}")
                    continue
                body = body_for(mid, ep)
                up_status, up_data = await upstream(ep, dict(body))
                r = await hub.post(HUB_PATH[ep], headers=headers, json=dict(body))
                ctype = r.headers.get("content-type", "")
                hub_data = (
                    r.json() if ctype.startswith("application/json")
                    else {"_raw": r.text[:200]}
                )
                tag = f"{mid} {ep}"
                print(f"\n[{tag}] upstream={up_status} hub={r.status_code}")
                if up_status != r.status_code:
                    check(f"{tag}: status match", False,
                          f"up={up_status} hub={r.status_code} "
                          f"{json.dumps(hub_data)[:150]}")
                    continue
                check(f"{tag}: status match", True)
                if up_status != 200:
                    print(f"      both non-200: {json.dumps(up_data)[:140]}")
                    continue
                s_up, s_hub = shape(up_data), shape(hub_data)
                missing = sorted(s_up - s_hub)
                extra = sorted(s_hub - s_up)
                check(f"{tag}: shape identical", not missing and not extra,
                      f"missing={missing[:6]} extra={extra[:6]}")

    print(f"\n{'=' * 64}\nPASSED {len(PASS)}   FAILED {len(FAIL)}")
    for f in FAIL:
        print("  -", f)


if __name__ == "__main__":
    asyncio.run(main())
