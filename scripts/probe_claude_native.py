"""One-off probe: does the Copilot upstream accept Anthropic-native requests?

Run:  python -m scripts.probe_claude_native
"""
from __future__ import annotations

import asyncio
import json

import httpx

from hub import copilot_client as cc


async def list_claude_models() -> list[str]:
    models = await cc.list_models()
    ids = [m.get("id", "") for m in models]
    claude = sorted(i for i in ids if "claude" in i.lower())
    print(f"=== upstream models: {len(ids)} total, {len(claude)} claude ===")
    for i in claude:
        print("  ", i)
    return claude


async def probe_native(model: str, path: str = "/v1/messages") -> None:
    """POST an Anthropic-shaped body straight at the upstream."""
    token, endpoint = await cc.get_api_token()
    payload = {
        "model": model,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
    }
    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.post(
            f"{endpoint}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
                **cc.EDITOR_HEADERS,
            },
            json=payload,
        )
    print(f"\n--- NATIVE {path} model={model} -> HTTP {r.status_code} ---")
    body = r.text[:800]
    print(body)
    if r.status_code == 200:
        try:
            data = r.json()
            print("  keys:", list(data.keys()))
            print("  content-blocks:", json.dumps(data.get("content"))[:300])
            print("  stop_reason:", data.get("stop_reason"))
            print("  usage:", data.get("usage"))
        except Exception as e:  # noqa: BLE001
            print("  (not json)", e)


async def probe_native_stream(model: str, path: str = "/v1/messages") -> None:
    token, endpoint = await cc.get_api_token()
    payload = {
        "model": model,
        "max_tokens": 64,
        "stream": True,
        "messages": [{"role": "user", "content": "Count: 1 2 3"}],
    }
    print(f"\n--- NATIVE STREAM {path} model={model} ---")
    async with httpx.AsyncClient(timeout=60.0) as c:
        async with c.stream(
            "POST",
            f"{endpoint}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "anthropic-version": "2023-06-01",
                **cc.EDITOR_HEADERS,
            },
            json=payload,
        ) as r:
            print("HTTP", r.status_code)
            n = 0
            async for line in r.aiter_lines():
                if not line:
                    continue
                print("   ", line[:200])
                n += 1
                if n > 25:
                    print("    ... (truncated)")
                    break


async def probe_openai(model: str) -> None:
    """Baseline: the path we use today."""
    status, body = await cc.post_json(
        "/chat/completions",
        {
            "model": model,
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        },
    )
    print(f"\n--- OPENAI /chat/completions model={model} -> HTTP {status} ---")
    print(json.dumps(body, ensure_ascii=False)[:600])


async def main() -> None:
    claude = await list_claude_models()
    if not claude:
        print("no claude models available; aborting")
        return

    # Prefer opus if present, else the first claude model.
    target = next((m for m in claude if "opus" in m), claude[0])
    print(f"\n>>> probing with model: {target}")

    await probe_openai(target)
    for path in ("/v1/messages", "/messages"):
        await probe_native(target, path)
    await probe_native_stream(target)


if __name__ == "__main__":
    asyncio.run(main())
