"""Reproduce a realistic Claude Code request against the deployed hub.

Claude Code sends a much heavier body than our synthetic tests: a long system
prompt with cache_control, a dozen tool schemas, and metadata -- any of which
can trip an upstream limit that a two-line "hi" never reaches. This walks up
the size/complexity ladder to find which ingredient causes the 502.

Reads HUB_BASE_URL / HUB_API_KEY from the environment or repo-root .env.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path


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


_load_dotenv()

URL = os.environ.get("HUB_BASE_URL", "").rstrip("/")
KEY = os.environ.get("HUB_API_KEY", "")
if not URL or not KEY:
    raise SystemExit("set HUB_BASE_URL and HUB_API_KEY (see .env.example)")


def go(name: str, body: dict, path: str = "/v1/messages?beta=true") -> None:
    h = {"Authorization": "Bearer " + KEY, "Content-Type": "application/json",
         "anthropic-version": "2023-06-01",
         "anthropic-beta": "claude-code-20250219,fine-grained-tool-streaming-2025-05-14"}
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), headers=h)
    size = len(json.dumps(body))
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            print(f"  {name:34} body={size:>7}B  HTTP {r.status}  got={len(r.read())}B")
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        print(f"  {name:34} body={size:>7}B  HTTP {e.code}")
        print(f"      {detail}")
    except Exception as e:  # noqa: BLE001
        print(f"  {name:34} body={size:>7}B  {type(e).__name__}: {str(e)[:120]}")


TOOL_NAMES = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch",
              "WebSearch", "Task", "TodoWrite", "NotebookEdit", "BashOutput"]


def tools(n: int) -> list[dict]:
    return [{
        "name": TOOL_NAMES[i % len(TOOL_NAMES)] + (str(i) if i >= len(TOOL_NAMES) else ""),
        "description": "Tool number %d. " % i + ("Detailed usage guidance. " * 40),
        "input_schema": {
            "type": "object",
            "properties": {
                "arg1": {"type": "string", "description": "First argument. " * 10},
                "arg2": {"type": "string", "description": "Second argument. " * 10},
            },
            "required": ["arg1"],
        },
    } for i in range(n)]


base_msg = [{"role": "user", "content": "List the files in this directory."}]

print("[A] escalating system prompt size")
for kb in (1, 8, 32):
    go(f"system ~{kb}KB", {
        "model": "claude-opus-5", "max_tokens": 1024, "stream": True,
        "system": [{"type": "text", "text": "You are Claude Code. " * (kb * 50),
                    "cache_control": {"type": "ephemeral"}}],
        "messages": base_msg,
    })

print("[B] escalating tool count")
for n in (1, 6, 12):
    go(f"{n} tools", {
        "model": "claude-opus-5", "max_tokens": 1024, "stream": True,
        "tools": tools(n),
        "messages": base_msg,
    })

print("[C] full Claude Code shape")
go("system 32KB + 12 tools + meta", {
    "model": "claude-opus-5", "max_tokens": 1024, "stream": True,
    "system": [
        {"type": "text", "text": "You are Claude Code, Anthropic's official CLI."},
        {"type": "text", "text": "You are Claude Code. " * 1600,
         "cache_control": {"type": "ephemeral"}},
    ],
    "tools": tools(12),
    "metadata": {"user_id": "test-user"},
    "messages": base_msg,
})
