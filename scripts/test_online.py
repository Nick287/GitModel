#!/usr/bin/env python3
"""Smoke-test a live GitModel Hub deployment.

Runs an end-to-end check against a running hub:
  1. GET  /api/status            — is the hub up and logged in to Copilot?
  2. GET  /v1/models             — does the API key work and models list?
  3. POST /v1/chat/completions   — does a real model call return content?
  4. POST /v1/messages           — (optional) Anthropic-compatible path.

Usage
-----
    export HUB_API_KEY=sk-...            # required when the hub enforces auth
    python3 scripts/test_online.py                     # tests production
    python3 scripts/test_online.py --base-url https://<env>.azurecontainerapps.io
    python3 scripts/test_online.py --model claude-sonnet-4.5 --anthropic

Exit code is 0 only if every selected check passes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from a repo-root .env into os.environ.

    Existing environment variables win, so an explicit `export` or shell var
    still overrides the file. Missing file is a no-op.
    """
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
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()

# Production ("default" workspace) URL — override with --base-url or HUB_BASE_URL.
DEFAULT_BASE_URL = "https://gitmodel-hub.agreeablegrass-75e2199b.centralus.azurecontainerapps.io"
DEFAULT_MODEL = "gpt-4o-mini"

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def _color(txt: str, code: str) -> str:
    return txt if not sys.stdout.isatty() else f"{code}{txt}{RESET}"


def ok(msg: str) -> None:
    print(f"{_color('PASS', GREEN)}  {msg}")


def fail(msg: str) -> None:
    print(f"{_color('FAIL', RED)}  {msg}")


def info(msg: str) -> None:
    print(f"{_color('····', DIM)}  {msg}")


def _request(
    method: str,
    url: str,
    *,
    api_key: str | None = None,
    body: dict | None = None,
    timeout: float = 60.0,
) -> tuple[int, dict | str]:
    """Perform an HTTP request; return (status_code, parsed_json_or_text)."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    except (urllib.error.URLError, TimeoutError) as e:
        return 0, f"{type(e).__name__}: {e}"
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def check_status(base_url: str) -> bool:
    info("GET /api/status")
    status, data = _request("GET", f"{base_url}/api/status")
    if status != 200 or not isinstance(data, dict):
        fail(f"/api/status returned {status}: {data}")
        return False
    logged_in = data.get("logged_in")
    require_auth = data.get("require_auth")
    info(f"logged_in={logged_in}  require_auth={require_auth}")
    if not logged_in:
        fail("Hub is up but NOT logged in to Copilot — model calls will 503.")
        return False
    ok("Hub is up and logged in to Copilot")
    return True


def check_models(base_url: str, api_key: str | None) -> bool:
    info("GET /v1/models")
    status, data = _request("GET", f"{base_url}/v1/models", api_key=api_key)
    if status == 401:
        fail("401 Unauthorized — missing/invalid API key (set HUB_API_KEY).")
        return False
    if status != 200 or not isinstance(data, dict):
        fail(f"/v1/models returned {status}: {data}")
        return False
    ids = [m.get("id") for m in data.get("data", [])]
    ok(f"{len(ids)} models available")
    info("e.g. " + ", ".join(str(i) for i in ids[:6]) + (" ..." if len(ids) > 6 else ""))
    return True


def check_chat(base_url: str, api_key: str | None, model: str) -> bool:
    info(f"POST /v1/chat/completions (model={model})")
    body = {
        "model": model,
        "messages": [
            {"role": "user", "content": "Reply with exactly: pong"},
        ],
        "max_tokens": 16,
        "stream": False,
    }
    status, data = _request("POST", f"{base_url}/v1/chat/completions", api_key=api_key, body=body)
    if status != 200 or not isinstance(data, dict):
        fail(f"chat/completions returned {status}: {data}")
        return False
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        fail(f"Unexpected response shape: {json.dumps(data)[:300]}")
        return False
    usage = data.get("usage", {})
    ok(f"Model replied: {content.strip()!r}")
    info(f"served={data.get('model')}  tokens in/out={usage.get('prompt_tokens')}/"
         f"{usage.get('completion_tokens')}")
    return True


def check_anthropic(base_url: str, api_key: str | None, model: str) -> bool:
    info(f"POST /v1/messages (model={model})")
    body = {
        "model": model,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
    }
    status, data = _request("POST", f"{base_url}/v1/messages", api_key=api_key, body=body)
    if status != 200 or not isinstance(data, dict):
        fail(f"/v1/messages returned {status}: {data}")
        return False
    try:
        text = "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )
    except (AttributeError, TypeError):
        fail(f"Unexpected response shape: {json.dumps(data)[:300]}")
        return False
    ok(f"Model replied: {text.strip()!r}")
    return True


def main() -> int:
    p = argparse.ArgumentParser(description="Smoke-test a live GitModel Hub.")
    p.add_argument("--base-url", default=os.environ.get("HUB_BASE_URL", DEFAULT_BASE_URL),
                   help="Hub base URL (default: production).")
    p.add_argument("--api-key", default=os.environ.get("HUB_API_KEY"),
                   help="Client API key (or set HUB_API_KEY).")
    p.add_argument("--model", default=os.environ.get("HUB_MODEL", DEFAULT_MODEL),
                   help=f"Model id for the chat test (default: {DEFAULT_MODEL}).")
    p.add_argument("--anthropic", action="store_true",
                   help="Also test the Anthropic /v1/messages endpoint.")
    args = p.parse_args()

    base_url = args.base_url.rstrip("/")
    print(f"Target: {base_url}\n")

    results: list[bool] = []
    results.append(check_status(base_url))
    print()
    results.append(check_models(base_url, args.api_key))
    print()
    results.append(check_chat(base_url, args.api_key, args.model))
    if args.anthropic:
        print()
        results.append(check_anthropic(base_url, args.api_key, args.model))

    print()
    passed = sum(results)
    total = len(results)
    line = f"{passed}/{total} checks passed"
    print(_color(line, GREEN if passed == total else RED))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
