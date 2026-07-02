#!/usr/bin/env python3
"""Test prompt (input) caching on a live GitModel Hub.

How prompt caching works
------------------------
When a request carries a long, stable *prefix* (a big system message, shared
context, etc.), the backend caches those prompt tokens. A follow-up request that
reuses the exact same prefix gets those tokens served from cache — reported as
``usage.prompt_tokens_details.cached_tokens`` — and billed cheaper. Only the new
trailing tokens are processed fresh, saving cost on long conversations.

What this script does
---------------------
1. Builds a deterministic prefix of ~N tokens (default 2048; OpenAI caches in
   1024-token blocks, so <1024 will not cache).
2. Sends a "warm" request to populate the cache.
3. Sends a "hit" request with the *same* prefix but a different trailing user
   turn, and checks that ``cached_tokens`` covers most of the prefix.

Usage
-----
    python3 scripts/test_cache.py                         # default model
    python3 scripts/test_cache.py --model gpt-4.1
    python3 scripts/test_cache.py --prefix-tokens 4096 --repeats 3

Reads HUB_BASE_URL / HUB_API_KEY from the environment or repo-root .env.
Exit code is 0 only if a cache hit is observed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://gitmodel-hub.agreeablegrass-75e2199b.centralus.azurecontainerapps.io"
# Models to test by default. gpt-5.5 is responses-only (served via /responses);
# the others use /chat/completions. gemini-2.5-pro caches reliably once the
# prefix clears its ~2048-token threshold (gemini-3.1-pro-preview does not yet).
DEFAULT_MODELS = ["gpt-5.5", "claude-opus-4.8", "gemini-2.5-pro"]

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m",
)


def _color(txt: str, code: str) -> str:
    return txt if not sys.stdout.isatty() else f"{code}{txt}{RESET}"


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from a repo-root .env into os.environ (env wins)."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_prefix(target_tokens: int) -> str:
    """Deterministic filler ~target_tokens long (~4 chars/token). The exact
    same bytes every run, so the prefix is a stable, cacheable block."""
    sentence = ("You are a meticulous assistant. Keep the following numbered "
                "reference lines in mind; they are stable context shared across "
                "requests and exist to exercise prompt caching. ")
    approx_chars = target_tokens * 4
    lines: list[str] = []
    total = 0
    i = 0
    while total < approx_chars:
        line = f"[{i:05d}] {sentence}"
        lines.append(line)
        total += len(line) + 1  # + newline
        i += 1
    return "\n".join(lines)


def _headers(api_key: str | None) -> dict[str, str]:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
def _headers(api_key: str | None, *, stream: bool = False) -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    h["Accept"] = "text/event-stream" if stream else "application/json"
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    return h


def call(base_url: str, api_key: str | None, prefix: str, suffix: str,
         model: str, timeout: float, endpoint: str = "chat",
         stream: bool = False) -> tuple[dict, str]:
    """One call sharing `prefix`; return (usage, endpoint_used).

    Uses /chat/completions by default and transparently falls back to
    /responses for models the backend only serves there (e.g. gpt-5.5).
    Set stream=True to exercise the streaming (SSE) path."""
    if endpoint == "responses":
        return _responses_call(base_url, api_key, prefix, suffix, model, timeout, stream), "responses"

    for token_key in ("max_tokens", "max_completion_tokens"):
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": prefix},
                {"role": "user", "content": suffix},
            ],
            token_key: 8,
            "stream": stream,
        }
        if stream:
            # Ask the backend to report usage on the final SSE chunk.
            body["stream_options"] = {"include_usage": True}
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{base_url}/v1/chat/completions", data=data,
            headers=_headers(api_key, stream=stream), method="POST",
        )
        try:
            if stream:
                usage = _read_stream_usage(req, timeout, responses=False)
                if usage:
                    return usage, "chat"
                # Empty stream (some responses-only models 200 with no data on
                # chat). Try the other token param, then fall back to /responses.
                if token_key == "max_tokens":
                    continue
                return _responses_call(base_url, api_key, prefix, suffix, model, timeout, stream), "responses"
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            return payload.get("usage", {}) or {}, "chat"
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            low = raw.lower()
            if token_key == "max_tokens" and e.code == 400 and "max_tokens" in low:
                continue  # this model wants max_completion_tokens
            if "not accessible via the /chat/completions" in low:
                return _responses_call(base_url, api_key, prefix, suffix, model, timeout, stream), "responses"
            raise
    raise RuntimeError("max_tokens unsupported and retry failed")


def _responses_call(base_url: str, api_key: str | None, prefix: str, suffix: str,
                    model: str, timeout: float, stream: bool = False) -> dict:
    """Cache probe via /v1/responses (for responses-only models)."""
    body = {
        "model": model,
        "input": f"{prefix}\n\n{suffix}",
        "max_output_tokens": 16,
        "stream": stream,
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{base_url}/v1/responses", data=data,
        headers=_headers(api_key, stream=stream), method="POST",
    )
    if stream:
        return _read_stream_usage(req, timeout, responses=True)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    return payload.get("usage", {}) or {}


def _read_stream_usage(req: urllib.request.Request, timeout: float,
                       *, responses: bool) -> dict:
    """Consume an SSE stream and return the usage object from its final event.

    Chat streams carry ``usage`` on a trailing chunk (with include_usage);
    Responses streams nest it under ``response.usage`` on the terminal
    response.completed / response.incomplete event."""
    usage: dict = {}
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if responses:
                resp_obj = obj.get("response") if isinstance(obj, dict) else None
                cand = (resp_obj or {}).get("usage") if isinstance(resp_obj, dict) else None
                cand = cand or obj.get("usage")
            else:
                cand = obj.get("usage")
            if cand:
                usage = cand
    return usage


def cached_tokens(usage: dict) -> int:
    """Extract cached prompt tokens across the shapes the hub can report."""
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    if isinstance(details, dict) and details.get("cached_tokens"):
        return int(details["cached_tokens"])
    # Copilot-native shape: copilot_usage.token_details[].cache_read
    for td in (usage.get("copilot_usage") or {}).get("token_details", []):
        if td.get("token_type") == "cache_read":
            return int(td.get("token_count", 0))
    return 0


def prompt_tokens(usage: dict) -> int:
    return int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)


def probe_model(base_url: str, api_key: str | None, model: str, prefix: str,
                repeats: int, delay: float, timeout: float,
                stream: bool = False) -> tuple[bool, int, int]:
    """Warm the cache then probe. Return (cache_hit, prompt_tokens, cached_tokens)."""
    try:
        warm, endpoint = call(base_url, api_key, prefix, "Warm-up. Reply: ok.",
                              model, timeout, stream=stream)
    except urllib.error.HTTPError as e:
        print(_color(f"  warm-up failed: {e.code} "
                     f"{e.read().decode('utf-8', 'replace')[:150]}", RED))
        return False, 0, 0
    except (urllib.error.URLError, TimeoutError, ValueError, RuntimeError) as e:
        print(_color(f"  warm-up failed: {e}", RED))
        return False, 0, 0

    print(f"  {'warm-up':10s}  via={endpoint:9s}  "
          f"prompt={prompt_tokens(warm):5d}  cached={cached_tokens(warm):5d}")
    time.sleep(delay)

    best = (0, 0)
    for n in range(1, repeats + 1):
        try:
            usage, endpoint = call(base_url, api_key, prefix,
                                   f"Follow-up #{n}. Reply with a single word.",
                                   model, timeout, endpoint=endpoint, stream=stream)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
                ValueError, RuntimeError) as e:
            print(_color(f"  hit #{n} failed: {e}", RED))
            return False, best[0], best[1]
        pt, ct = prompt_tokens(usage), cached_tokens(usage)
        if ct > best[1]:
            best = (pt, ct)
        ratio = (ct / pt * 100) if pt else 0
        line = (f"  {('hit #' + str(n)):10s}  via={endpoint:9s}  "
                f"prompt={pt:5d}  cached={ct:5d}  ({ratio:4.0f}% of prompt)")
        print(_color(line, GREEN if ct > 0 else DIM))
        if ct > 0:
            break
        if n < repeats:
            time.sleep(delay)
    return best[1] > 0, best[0], best[1]


def main() -> int:
    _load_dotenv()
    p = argparse.ArgumentParser(description="Test prompt/input caching on a live hub.")
    p.add_argument("--base-url", default=os.environ.get("HUB_BASE_URL", DEFAULT_BASE_URL))
    p.add_argument("--api-key", default=os.environ.get("HUB_API_KEY"))
    p.add_argument("--model", action="append", dest="models",
                   help="Model to test (repeatable). Defaults to gpt-5.5, "
                        "claude-opus-4.8, gemini-2.5-pro.")
    p.add_argument("--mode", choices=("nonstream", "stream", "both"), default="both",
                   help="Which request path to exercise (default both).")
    p.add_argument("--prefix-tokens", type=int, default=3072,
                   help="Approx size of the shared prefix (default 3072 → ~2600 "
                        "real tokens, above Gemini Pro's 2048 cache threshold; "
                        "<1024 won't cache at all).")
    p.add_argument("--repeats", type=int, default=3,
                   help="How many cache-hit follow-up probes to make (default 3). "
                        "Passes as soon as one probe reports cached tokens.")
    p.add_argument("--delay", type=float, default=3.0,
                   help="Seconds to wait after warming before probing (cache "
                        "population has a few seconds of latency).")
    p.add_argument("--timeout", type=float, default=60.0)
    args = p.parse_args()

    models = args.models or DEFAULT_MODELS
    modes = ["nonstream", "stream"] if args.mode == "both" else [args.mode]
    base_url = args.base_url.rstrip("/")
    prefix = build_prefix(args.prefix_tokens)
    print(f"Target : {base_url}")
    print(f"Models : {', '.join(models)}")
    print(f"Mode   : {args.mode}")
    print(f"Prefix : ~{args.prefix_tokens} tokens ({len(prefix)} chars)\n")

    if args.prefix_tokens < 1024:
        print(_color("Warning: prefixes < 1024 tokens usually don't trigger "
                     "caching; results may show 0 cached.", YELLOW) + "\n")

    results: list[tuple[str, str, bool, int, int]] = []
    for model in models:
        for mode in modes:
            print(_color(f"{model}  [{mode}]", BOLD))
            ok, pt, ct = probe_model(base_url, args.api_key, model, prefix,
                                     args.repeats, args.delay, args.timeout,
                                     stream=(mode == "stream"))
            results.append((model, mode, ok, pt, ct))
            print()

    print(_color(f"{'MODEL':26s}  {'MODE':10s}  CACHE  CACHED/PROMPT", BOLD))
    print("-" * 62)
    passed = 0
    for model, mode, ok, pt, ct in results:
        passed += ok
        mark = _color("hit ", GREEN) if ok else _color("miss", RED)
        ratio = f"{ct}/{pt} ({(ct / pt * 100) if pt else 0:.0f}%)" if ct else "-"
        print(f"{model:26s}  {mode:10s}  {mark}   {ratio}")

    print()
    summary = f"{passed}/{len(results)} probes showed prompt caching"
    print(_color(summary, GREEN if passed == len(results) else RED))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
