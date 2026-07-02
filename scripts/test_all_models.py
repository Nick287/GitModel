#!/usr/bin/env python3
"""Check availability of every model on a live GitModel Hub.

For each model returned by ``GET /v1/models`` this runs two real calls against
``/v1/chat/completions``:

  * non-stream — expects a JSON body with ``choices[0].message.content``
  * stream     — expects an SSE stream of ``data: {...}`` deltas ending in
                 ``[DONE]``

Results are printed as a table (model | non-stream | stream | latency) and a
summary. Models whose id looks like an embedding/image model are skipped, since
they don't speak chat completions.

Usage
-----
    python3 scripts/test_all_models.py                 # test all chat models
    python3 scripts/test_all_models.py --workers 8     # more concurrency
    python3 scripts/test_all_models.py --filter claude # only ids containing "claude"
    python3 scripts/test_all_models.py --include-nonchat  # don't skip embedding/image

Reads HUB_BASE_URL / HUB_API_KEY from the environment or repo-root .env.
Exit code is 0 only if every tested model passes both calls.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DEFAULT_BASE_URL = "https://gitmodel-hub.agreeablegrass-75e2199b.centralus.azurecontainerapps.io"

# Token budget per test call. Small to stay cheap, but large enough that
# reasoning models (gpt-5.x) still emit output tokens instead of spending the
# whole budget on hidden reasoning and returning an empty stream.
MAX_TOKENS = 64

# Retry budget for reasoning models (gemini-3.x, some gpt-5.x) that spend the
# whole small budget on hidden reasoning and stop with finish_reason="length"
# before emitting any answer. Big enough to leave room for the reply.
BIG_MAX_TOKENS = 2048

# ids matching these substrings don't speak /chat/completions — skip by default.
NONCHAT_HINTS = ("embedding", "gpt-image", "dall-e", "tts", "whisper")

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


def _headers(api_key: str | None, *, stream: bool) -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    h["Accept"] = "text/event-stream" if stream else "application/json"
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    return h


def list_models(base_url: str, api_key: str | None) -> list[str]:
    req = urllib.request.Request(
        f"{base_url}/v1/models", headers=_headers(api_key, stream=False), method="GET"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return [m.get("id") for m in data.get("data", []) if m.get("id")]


def _body(model: str, *, stream: bool, token_key: str = "max_tokens",
          budget: int = MAX_TOKENS) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
        token_key: budget,
        "stream": stream,
    }


def _responses_body(model: str, *, stream: bool, budget: int = MAX_TOKENS) -> dict:
    return {
        "model": model,
        "input": "Reply with exactly: pong",
        "max_output_tokens": budget,
        "stream": stream,
    }


def _chat_only_model(raw: bytes) -> bool:
    """True if the backend says a model isn't reachable via /chat/completions
    (these GPT-5 codex/reasoning models are only served by /responses)."""
    return "not accessible via the /chat/completions" in raw.decode("utf-8", "replace").lower()


def _needs_completion_tokens(e: urllib.error.HTTPError, body: bytes) -> bool:
    """True if a 400 complains that max_tokens is unsupported (GPT-5 family)."""
    if e.code != 400:
        return False
    return "max_tokens" in body.decode("utf-8", "replace").lower()


def test_nonstream(base_url: str, api_key: str | None, model: str, timeout: float,
                   budget: int = MAX_TOKENS) -> tuple[bool, str, str]:
    """Return (ok, detail, endpoint). Falls back to /responses when a model is
    not reachable via /chat/completions."""
    url = f"{base_url}/v1/chat/completions"
    for token_key in ("max_tokens", "max_completion_tokens"):
        data = json.dumps(_body(model, stream=False, token_key=token_key, budget=budget)).encode()
        req = urllib.request.Request(
            url, data=data, headers=_headers(api_key, stream=False), method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            choice = payload["choices"][0]
            message = choice["message"]
            content = message.get("content")
            if content is None:
                # Some special models (e.g. trajectory-compaction) return a
                # valid message with no textual content — still reachable and
                # responding, so count it as available.
                return True, "<no text>", "chat"
            # Reasoning models can burn the whole small budget on hidden
            # reasoning (finish_reason="length", empty content). Retry once
            # with a bigger budget so they get room to actually answer.
            if not content.strip() and choice.get("finish_reason") == "length" \
                    and budget < BIG_MAX_TOKENS:
                return test_nonstream(base_url, api_key, model, timeout, budget=BIG_MAX_TOKENS)
            return True, (content.strip() or "<empty>")[:40], "chat"
        except urllib.error.HTTPError as e:
            raw = e.read()
            if token_key == "max_tokens" and _needs_completion_tokens(e, raw):
                continue  # retry once with max_completion_tokens
            if _chat_only_model(raw):
                ok, msg = _responses_nonstream(base_url, api_key, model, timeout)
                return ok, msg, "responses"
            return False, _http_err_raw(e.code, raw), "chat"
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as e:
            return False, f"{type(e).__name__}: {e}", "chat"
    return False, "400: max_tokens unsupported", "chat"


def _responses_nonstream(base_url: str, api_key: str | None, model: str, timeout: float,
                         budget: int = MAX_TOKENS) -> tuple[bool, str]:
    data = json.dumps(_responses_body(model, stream=False, budget=budget)).encode()
    req = urllib.request.Request(
        f"{base_url}/v1/responses", data=data,
        headers=_headers(api_key, stream=False), method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        if not payload.get("output"):
            return False, "no output in response"
        text = "".join(
            c.get("text", "")
            for item in payload.get("output", [])
            if item.get("type") == "message"
            for c in item.get("content", [])
            if c.get("type") == "output_text"
        )
        # Reasoning ran out of output budget before answering — retry bigger.
        incomplete = (payload.get("incomplete_details") or {}).get("reason")
        if not text.strip() and incomplete == "max_output_tokens" and budget < BIG_MAX_TOKENS:
            return _responses_nonstream(base_url, api_key, model, timeout, budget=BIG_MAX_TOKENS)
        return True, (text.strip() or "<no text>")[:40]
    except urllib.error.HTTPError as e:
        return False, _http_err_raw(e.code, e.read())
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return False, f"{type(e).__name__}: {e}"


def test_stream(base_url: str, api_key: str | None, model: str, timeout: float,
                token_key: str = "max_tokens", budget: int = MAX_TOKENS) -> tuple[bool, str, str]:
    """Return (ok, detail, endpoint). Falls back to /responses when a model is
    not reachable via /chat/completions."""
    data = json.dumps(_body(model, stream=True, token_key=token_key, budget=budget)).encode()
    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions", data=data,
        headers=_headers(api_key, stream=True), method="POST",
    )
    chunks = 0
    text: list[str] = []
    finish_reason = None
    try:
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
                if obj.get("error"):
                    return False, str(obj["error"])[:60], "chat"
                chunks += 1
                choice = (obj.get("choices") or [{}])[0]
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                if choice.get("delta", {}).get("content"):
                    text.append(choice["delta"]["content"])
        if chunks == 0:
            # Some GPT-5 models silently return a 200 empty stream when sent
            # max_tokens (instead of a 400). Retry once with the parameter they
            # do accept before giving up.
            if token_key == "max_tokens":
                return test_stream(base_url, api_key, model, timeout,
                                   token_key="max_completion_tokens", budget=budget)
            # Still nothing over chat — responses-only models stream via
            # /responses even though non-stream 400s point us there. Fall back.
            ok, msg = _responses_stream(base_url, api_key, model, timeout)
            return ok, msg, "responses"
        # Reasoning model exhausted the small budget before emitting text —
        # retry once with a bigger budget so it has room to answer.
        if not "".join(text).strip() and finish_reason == "length" \
                and budget < BIG_MAX_TOKENS:
            return test_stream(base_url, api_key, model, timeout,
                               token_key=token_key, budget=BIG_MAX_TOKENS)
        return True, ("".join(text).strip() or f"<{chunks} chunks>")[:40], "chat"
    except urllib.error.HTTPError as e:
        raw = e.read()
        if token_key == "max_tokens" and _needs_completion_tokens(e, raw):
            return test_stream(base_url, api_key, model, timeout,
                               token_key="max_completion_tokens", budget=budget)
        if _chat_only_model(raw):
            ok, msg = _responses_stream(base_url, api_key, model, timeout)
            return ok, msg, "responses"
        return False, _http_err_raw(e.code, raw), "chat"
    except (urllib.error.URLError, TimeoutError) as e:
        return False, f"{type(e).__name__}: {e}", "chat"


def _responses_stream(base_url: str, api_key: str | None, model: str, timeout: float,
                      budget: int = MAX_TOKENS) -> tuple[bool, str]:
    data = json.dumps(_responses_body(model, stream=True, budget=budget)).encode()
    req = urllib.request.Request(
        f"{base_url}/v1/responses", data=data,
        headers=_headers(api_key, stream=True), method="POST",
    )
    chunks = 0
    text: list[str] = []
    incomplete = None
    try:
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
                if obj.get("type") == "error" or obj.get("error"):
                    return False, str(obj.get("error") or obj)[:60]
                chunks += 1
                if obj.get("type") == "response.output_text.delta":
                    text.append(obj.get("delta", ""))
                if obj.get("type") == "response.incomplete":
                    incomplete = ((obj.get("response") or {}).get(
                        "incomplete_details") or {}).get("reason")
        if chunks == 0:
            return False, "no SSE data chunks received"
        # Reasoning ran out of output budget before answering — retry bigger.
        if not "".join(text).strip() and incomplete == "max_output_tokens" \
                and budget < BIG_MAX_TOKENS:
            return _responses_stream(base_url, api_key, model, timeout, budget=BIG_MAX_TOKENS)
        return True, ("".join(text).strip() or f"<{chunks} chunks>")[:40]
    except urllib.error.HTTPError as e:
        return False, _http_err_raw(e.code, e.read())
    except (urllib.error.URLError, TimeoutError) as e:
        return False, f"{type(e).__name__}: {e}"


def _http_err_raw(code: int, raw: bytes) -> str:
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
        detail = body.get("detail") or body.get("error") or body
        return f"{code}: {str(detail)[:60]}"
    except (ValueError, AttributeError):
        return f"HTTP {code}"


def run_one(base_url: str, api_key: str | None, model: str, timeout: float) -> dict:
    t0 = time.time()
    ns_ok, ns_msg, ns_ep = test_nonstream(base_url, api_key, model, timeout)
    st_ok, st_msg, st_ep = test_stream(base_url, api_key, model, timeout)
    # Prefer showing the endpoint that actually served the model.
    endpoint = "responses" if "responses" in (ns_ep, st_ep) else "chat"
    return {
        "model": model,
        "nonstream_ok": ns_ok, "nonstream_msg": ns_msg,
        "stream_ok": st_ok, "stream_msg": st_msg,
        "endpoint": endpoint,
        "elapsed": time.time() - t0,
    }


def _mark(ok: bool) -> str:
    return _color("ok  ", GREEN) if ok else _color("FAIL", RED)


def main() -> int:
    _load_dotenv()
    p = argparse.ArgumentParser(description="Test stream + non-stream availability of all hub models.")
    p.add_argument("--base-url", default=os.environ.get("HUB_BASE_URL", DEFAULT_BASE_URL))
    p.add_argument("--api-key", default=os.environ.get("HUB_API_KEY"))
    p.add_argument("--filter", default="", help="Only test model ids containing this substring.")
    p.add_argument("--workers", type=int, default=6, help="Concurrent models (default 6).")
    p.add_argument("--timeout", type=float, default=60.0, help="Per-request timeout seconds.")
    p.add_argument("--include-nonchat", action="store_true",
                   help="Also test embedding/image models (normally skipped).")
    args = p.parse_args()

    base_url = args.base_url.rstrip("/")
    print(f"Target: {base_url}\n")

    try:
        models = list_models(base_url, args.api_key)
    except urllib.error.HTTPError as e:
        print(_color(f"Failed to list models: {_http_err_raw(e.code, e.read())}", RED))
        return 1
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        print(_color(f"Failed to list models: {e}", RED))
        return 1

    if args.filter:
        models = [m for m in models if args.filter.lower() in m.lower()]

    skipped = []
    if not args.include_nonchat:
        keep = []
        for m in models:
            if any(h in m.lower() for h in NONCHAT_HINTS):
                skipped.append(m)
            else:
                keep.append(m)
        models = keep

    if not models:
        print(_color("No models to test.", YELLOW))
        return 1

    print(f"Testing {len(models)} model(s) "
          f"({'incl.' if args.include_nonchat else 'skipping'} non-chat), "
          f"{args.workers} workers…\n")

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(run_one, base_url, args.api_key, m, args.timeout) for m in models
        ]
        for fut in futures:
            results.append(fut.result())

    results.sort(key=lambda r: r["model"])
    width = max(len(r["model"]) for r in results)
    header = f"{'MODEL'.ljust(width)}  NON-STREAM  STREAM  VIA        TIME   DETAIL"
    print(_color(header, BOLD))
    print("-" * (len(header) + 20))

    both_pass = 0
    for r in results:
        if r["nonstream_ok"] and r["stream_ok"]:
            both_pass += 1
        detail = r["nonstream_msg"] if not r["nonstream_ok"] else (
            r["stream_msg"] if not r["stream_ok"] else r["nonstream_msg"]
        )
        print(
            f"{r['model'].ljust(width)}  "
            f"{_mark(r['nonstream_ok'])}        "
            f"{_mark(r['stream_ok'])}    "
            f"{r['endpoint'].ljust(9)}  "
            f"{r['elapsed']:4.1f}s  "
            f"{_color(str(detail), DIM)}"
        )

    print()
    total = len(results)
    summary = f"{both_pass}/{total} models passed both stream and non-stream"
    print(_color(summary, GREEN if both_pass == total else RED))
    if skipped:
        print(_color(f"skipped (non-chat): {', '.join(skipped)}", DIM))
    return 0 if both_pass == total else 1


if __name__ == "__main__":
    sys.exit(main())
