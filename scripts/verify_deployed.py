"""End-to-end verification against a deployed hub, over real HTTPS.

Unlike scripts/test_passthrough.py -- which drives the app in-process -- this
talks to the deployed URL with a *strict* HTTP client. That distinction matters:
curl silently tolerates a truncated chunked body, so a broken stream looks fine
through curl while Claude Code (Node undici) and Python's http.client reject it.
Reading responses with urllib here is what surfaces that class of bug.

Which models a hub serves depends on the Copilot account behind it, so the
suite asks /v1/models first and runs each check against a model that is
actually available. Checks with no suitable model are SKIPped, never failed --
an account without Claude access is not a broken gateway, and reporting it as
one hides any real regression in the noise.

Note the asymmetry: /v1/messages is refused upstream for anything but Claude
("no model endpoints available"), so its checks genuinely cannot run without
one. count_tokens is a tokenizer call and works on any served model, so it
runs regardless.

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
SKIPPED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    verdict = "PASS" if cond else "FAIL"
    (PASSED if cond else FAILED).append(name)
    print(f"  {verdict}  {name}" + (f"  -- {detail}" if detail else ""))


def skip(name: str, why: str) -> None:
    """Record a check the deployment cannot run, distinct from a failure.

    Which models a hub serves depends on the Copilot account behind it, and
    that changes without the code changing. Calling an absent model a failure
    reports a broken gateway when the gateway is fine, and buries any real
    regression among the noise.
    """
    SKIPPED.append(name)
    print(f"  SKIP  {name}  -- {why}")


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


print("[0] model discovery")
_s, _b = hit("/v1/models", method="GET")
if _s == 200:
    try:
        MODELS = [m.get("id") for m in json.loads(_b).get("data", []) if m.get("id")]
    except (json.JSONDecodeError, AttributeError):
        MODELS = []
else:
    MODELS = []
check("/v1/models", _s == 200 and bool(MODELS), f"{_s}, {len(MODELS)} models")


def pick(*candidates: str) -> str | None:
    """First candidate this deployment actually serves, or None."""
    for c in candidates:
        if c in MODELS:
            return c
    return None


# The Anthropic-shaped tests need a Claude model; the Responses test needs a
# GPT one. Both lists are preference-ordered, so a hub missing the newest
# still runs the suite on whatever it does have.
ANTHROPIC = pick("claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
                 "claude-sonnet-5", "claude-sonnet-4-6", "claude-haiku-4.5")
RESPONSES = pick("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5",
                 "gpt-5.4", "gpt-4.1", "gpt-4o")
OPENAI = pick("gpt-4o-mini", "gpt-4o", "gpt-4.1", "gpt-3.5-turbo")
print(f"  using anthropic={ANTHROPIC}  responses={RESPONSES}  openai={OPENAI}")
if not ANTHROPIC:
    claude = [m for m in MODELS if "claude" in m]
    print(f"  note: this account serves no Claude model "
          f"({len(MODELS)} models, claude={claude or 'none'}); "
          f"Anthropic-shaped checks will be skipped, not failed")

print("[1] health + endpoints")
s, b = hit("/api/status", method="GET")
check("status logged_in", s == 200 and b"true" in b, b.decode()[:50])
# count_tokens works on any model the account serves -- it is a tokenizer
# call, not an inference one, so it does not need Claude the way the rest of
# /v1/messages does.
COUNT_MODEL = ANTHROPIC or OPENAI or RESPONSES
if COUNT_MODEL:
    s, b = hit("/v1/messages/count_tokens",
               {"model": COUNT_MODEL, "messages": [{"role": "user", "content": "hi"}]})
    check(f"count_tokens ({COUNT_MODEL})", s == 200, f"{s} {b.decode()[:50]}")
else:
    skip("count_tokens", "no model available")
if ANTHROPIC:
    s, b = hit("/v1/chat/completions",
               {"model": ANTHROPIC, "max_tokens": 40,
                "messages": [{"role": "user", "content": "say OK"}]})
    check("chat/completions", s == 200, str(s))
else:
    skip("chat/completions", "no Claude model available")
if OPENAI:
    s, b = hit("/v1/chat/completions",
               {"model": OPENAI, "max_tokens": 40,
                "messages": [{"role": "user", "content": "say OK"}]})
    check(f"chat/completions ({OPENAI})", s == 200, str(s))
else:
    skip("chat/completions (openai)", "no GPT model available")
if RESPONSES:
    s, b = hit("/v1/responses",
               {"model": RESPONSES, "max_output_tokens": 2000, "input": "say OK"})
    check(f"responses ({RESPONSES})", s == 200, str(s))
else:
    skip("responses", "no GPT model available")

print("[2] Claude Code request shapes")
if ANTHROPIC:
    s, b = hit("/v1/messages", {
        "model": ANTHROPIC, "max_tokens": 1024, "stream": True,
        "system": [{"type": "text", "text": "You are Claude Code.",
                    "cache_control": {"type": "ephemeral"}}],
        "tools": [{"name": "Bash", "description": "Run a command",
                   "input_schema": {"type": "object",
                                    "properties": {"command": {"type": "string"}},
                                    "required": ["command"]}}],
        "messages": [{"role": "user", "content": "List files"}]})
    check("system + tools + cache_control (stream)", s == 200 and len(b) > 0, str(s))
    s, b = hit("/v1/messages",
               {"model": ANTHROPIC, "max_tokens": 32, "stream": True,
                "messages": [{"role": "user", "content": "hi"}]}, auth="apikey")
    check("x-api-key auth", s == 200 and len(b) > 0, str(s))
    s, b = hit("/v1/messages",
               {"model": ANTHROPIC, "max_tokens": 32, "stream": True,
                "messages": [{"role": "user", "content": "hi"}]},
               hdr={"anthropic-beta": "claude-code-20250219,"
                                      "fine-grained-tool-streaming-2025-05-14"})
    check("anthropic-beta header", s == 200 and len(b) > 0, str(s))
else:
    for n in ("system + tools + cache_control (stream)", "x-api-key auth",
              "anthropic-beta header"):
        skip(n, "/v1/messages accepts only Claude upstream")

print("[3] tool_use")
if ANTHROPIC:
    s, b = hit("/v1/messages", {
        "model": ANTHROPIC, "max_tokens": 200,
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
else:
    skip("tool_use block returned", "/v1/messages accepts only Claude upstream")
    skip("stop_reason == tool_use", "/v1/messages accepts only Claude upstream")

print("[4] vision")
png = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAE"
    "hQGAhKmMIQAAAABJRU5ErkJggg=="
)
if ANTHROPIC:
    s, b = hit("/v1/messages", {
        "model": ANTHROPIC, "max_tokens": 100,
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
else:
    skip("vision reply non-empty", "/v1/messages accepts only Claude upstream")

print("[5] stream reliability under a strict client")
if ANTHROPIC:
    bad = 0
    last = ""
    for _ in range(5):
        s, b = hit("/v1/messages",
                   {"model": ANTHROPIC, "max_tokens": 32, "stream": True,
                    "messages": [{"role": "user", "content": "Count to three."}]})
        if s != 200 or not b:
            bad += 1
        else:
            last = sse_text(b)
    check("5/5 streams read completely", bad == 0, f"{5 - bad}/5 ok, text={last[:30]!r}")
elif OPENAI:
    # The truncated-stream bug this guards is in the shared streaming path,
    # not in any one model, so a GPT model still exercises it.
    bad = 0
    for _ in range(5):
        s, b = hit("/v1/chat/completions",
                   {"model": OPENAI, "max_tokens": 32, "stream": True,
                    "messages": [{"role": "user", "content": "Count to three."}]})
        if s != 200 or not b:
            bad += 1
    check(f"5/5 streams read completely ({OPENAI})", bad == 0, f"{5 - bad}/5 ok")
else:
    skip("5/5 streams read completely", "no usable model available")

print(f"\n{'=' * 56}")
print(f"PASSED {len(PASSED)}   FAILED {len(FAILED)}   SKIPPED {len(SKIPPED)}")
for f in FAILED:
    print("  FAIL -", f)
for sk in SKIPPED:
    print("  skip -", sk)
raise SystemExit(1 if FAILED else 0)
