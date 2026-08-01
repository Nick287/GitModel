"""End-to-end verification against a deployed hub (dev02), over real HTTPS.

Unlike scripts/test_passthrough.py -- which drives the app in-process -- this
talks to the deployed URL with a *strict* HTTP client. That distinction matters:
curl silently tolerates a truncated chunked body, so a broken stream looks fine
through curl while Claude Code (Node undici) and Python's http.client reject it.
Reading responses with urllib here is what surfaces that class of bug.

Reads HUB_BASE_URL / HUB_API_KEY from the environment or repo-root .env.

Run:  HUB_BASE_URL=https://<env>.azurecontainerapps.io python scripts/verify_deployed.py
"""
from __future__ import annotations

import base64
import http.client
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

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    verdict = "PASS" if cond else "FAIL"
    (PASSED if cond else FAILED).append(name)
    print(f"  {verdict}  {name}" + (f"  -- {detail}" if detail else ""))


def hit(path, body=None, method="POST", hdr=None, auth="bearer"):
    """Return (status, raw_bytes). A truncated stream comes back as
    ("INCOMPLETE", partial) rather than raising, so it can be asserted on."""
    h = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    if auth == "apikey":
        h["x-api-key"] = KEY
    else:
        h["Authorization"] = "Bearer " + KEY
    if hdr:
        h.update(hdr)
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(URL + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except http.client.IncompleteRead as e:
        return "INCOMPLETE", e.partial
    except Exception as e:  # noqa: BLE001
        return type(e).__name__, str(e).encode()


def sse_text(raw: bytes) -> str:
    out = ""
    for line in raw.decode("utf-8", "replace").split("\n"):
        if not line.startswith("data:"):
            continue
        p = line[5:].strip()
        if not p or p == "[DONE]":
            continue
        try:
            o = json.loads(p)
        except json.JSONDecodeError:
            continue
        if o.get("type") == "content_block_delta":
            out += (o.get("delta") or {}).get("text", "")
    return out


print("[1] health + endpoints")
s, b = hit("/api/status", method="GET")
check("status logged_in", s == 200 and b"true" in b, b.decode()[:50])
s, b = hit("/v1/messages/count_tokens",
           {"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi"}]})
check("count_tokens", s == 200, f"{s} {b.decode()[:50]}")
s, b = hit("/v1/chat/completions",
           {"model": "claude-opus-5", "max_tokens": 40,
            "messages": [{"role": "user", "content": "say OK"}]})
check("chat/completions", s == 200, str(s))
s, b = hit("/v1/responses",
           {"model": "gpt-5.6-sol", "max_output_tokens": 2000, "input": "say OK"})
check("responses (gpt-5.6-sol)", s == 200, str(s))

print("[2] Claude Code request shapes")
s, b = hit("/v1/messages", {
    "model": "claude-opus-5", "max_tokens": 1024, "stream": True,
    "system": [{"type": "text", "text": "You are Claude Code.",
                "cache_control": {"type": "ephemeral"}}],
    "tools": [{"name": "Bash", "description": "Run a command",
               "input_schema": {"type": "object",
                                "properties": {"command": {"type": "string"}},
                                "required": ["command"]}}],
    "messages": [{"role": "user", "content": "List files"}]})
check("system + tools + cache_control (stream)", s == 200 and len(b) > 0, str(s))
s, b = hit("/v1/messages",
           {"model": "claude-opus-5", "max_tokens": 32, "stream": True,
            "messages": [{"role": "user", "content": "hi"}]}, auth="apikey")
check("x-api-key auth", s == 200 and len(b) > 0, str(s))
s, b = hit("/v1/messages",
           {"model": "claude-opus-5", "max_tokens": 32, "stream": True,
            "messages": [{"role": "user", "content": "hi"}]},
           hdr={"anthropic-beta": "claude-code-20250219,"
                                  "fine-grained-tool-streaming-2025-05-14"})
check("anthropic-beta header", s == 200 and len(b) > 0, str(s))

print("[3] tool_use")
s, b = hit("/v1/messages", {
    "model": "claude-opus-5", "max_tokens": 200,
    "tools": [{"name": "get_weather", "description": "Weather for a city",
               "input_schema": {"type": "object",
                                "properties": {"city": {"type": "string"}},
                                "required": ["city"]}}],
    "messages": [{"role": "user", "content": "Weather in Paris? Use the tool."}]})
if s == 200:
    d = json.loads(b)
    tu = [x for x in d.get("content", []) if x.get("type") == "tool_use"]
    check("tool_use block returned", bool(tu), tu[0].get("name") if tu else "none")
    check("stop_reason == tool_use", d.get("stop_reason") == "tool_use",
          str(d.get("stop_reason")))
else:
    check("tool_use block returned", False, str(s))
    check("stop_reason == tool_use", False, str(s))

print("[4] vision")
png = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAE"
    "hQGAhKmMIQAAAABJRU5ErkJggg=="
)
s, b = hit("/v1/messages", {
    "model": "claude-opus-5", "max_tokens": 100,
    "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": base64.b64encode(png).decode()}},
        {"type": "text", "text": "Describe this image in 5 words."}]}]})
if s == 200:
    d = json.loads(b)
    txt = " ".join(x.get("text", "") for x in d.get("content", [])
                   if x.get("type") == "text")
    check("vision reply non-empty", bool(txt.strip()), repr(txt[:60]))
else:
    check("vision reply non-empty", False, str(s))

print("[5] stream reliability under a strict client")
bad = 0
last = ""
for _ in range(5):
    s, b = hit("/v1/messages",
               {"model": "claude-opus-5", "max_tokens": 32, "stream": True,
                "messages": [{"role": "user", "content": "Count to three."}]})
    if s != 200 or not b:
        bad += 1
    else:
        last = sse_text(b)
check("5/5 streams read completely", bad == 0, f"{5 - bad}/5 ok, text={last[:30]!r}")

print(f"\n{'=' * 56}\nPASSED {len(PASSED)}   FAILED {len(FAILED)}")
for f in FAILED:
    print("  -", f)
