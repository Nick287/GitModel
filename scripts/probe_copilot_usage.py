"""Which models report a copilot_usage block, streamed and buffered?

That block is where billing comes from: total_nano_aiu is the upstream's own
charge, taken verbatim. A model that omits it falls back to the local price
table, and one that omits it in only one of the two modes would be billed
differently depending on how it was called -- a discrepancy no summary would
reveal, since both land in the same column.

Each model is tried against the endpoints it might serve: newer GPT models are
Responses-only and answer "not accessible via the /chat/completions endpoint",
so probing one endpoint alone reports them as broken when they are merely
somewhere else.

Sends a tiny request per model per mode, so it costs a little quota.
Reads HUB_BASE_URL / HUB_API_KEY from the environment or repo-root .env.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_dotenv() -> None:
    try:
        text = (Path(__file__).resolve().parent.parent / ".env").read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()
URL = os.environ.get("HUB_BASE_URL", "").rstrip("/")
KEY = os.environ.get("HUB_API_KEY", "")
if not URL or not KEY:
    raise SystemExit("set HUB_BASE_URL and HUB_API_KEY")

# Embeddings and image models do not take chat messages at all; the compaction
# model is an internal helper. None of them are what billing rides on.
SKIP_PREFIXES = ("text-embedding", "gpt-image")
SKIP_EXACT = {"trajectory-compaction"}


def hit(path, body, method="POST"):
    h = {"content-type": "application/json", "x-api-key": KEY,
         "anthropic-version": "2023-06-01"}
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(URL + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # noqa: BLE001
        return type(e).__name__, str(e).encode()


def usage_buffered(raw):
    try:
        return (json.loads(raw) or {}).get("copilot_usage")
    except Exception:  # noqa: BLE001
        return None


def usage_streamed(raw):
    """copilot_usage arrives once in the event stream; its nesting varies."""
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
        if not isinstance(o, dict):
            continue
        for cand in (o, o.get("message"), o.get("response")):
            if isinstance(cand, dict) and isinstance(cand.get("copilot_usage"), dict):
                return cand["copilot_usage"]
    return None


def probe(model, anthropic, stream):
    attempts = []
    if anthropic:
        b = {"model": model, "max_tokens": 8,
             "messages": [{"role": "user", "content": "hi"}]}
        if stream:
            b["stream"] = True
        attempts.append(("/v1/messages", b))
    else:
        c = {"model": model, "max_tokens": 8,
             "messages": [{"role": "user", "content": "hi"}]}
        if stream:
            c["stream"] = True
            c["stream_options"] = {"include_usage": True}
        attempts.append(("/v1/chat/completions", c))
        r = {"model": model, "max_output_tokens": 2000, "input": "hi"}
        if stream:
            r["stream"] = True
        attempts.append(("/v1/responses", r))

    last = None
    for path, body in attempts:
        s, raw = hit(path, body)
        if s == 200:
            u = usage_streamed(raw) if stream else usage_buffered(raw)
            return s, u, path.rsplit("/", 1)[-1]
        last = s
    return last, None, "-"


def nano(u):
    if not isinstance(u, dict):
        return None
    try:
        return int(u.get("total_nano_aiu") or 0)
    except (TypeError, ValueError):
        return 0


s, raw = hit("/v1/models", None, method="GET")
if s != 200:
    raise SystemExit(f"/v1/models -> {s}")
models = [m["id"] for m in json.loads(raw).get("data", []) if m.get("id")]
models = [m for m in models if not m.startswith(SKIP_PREFIXES) and m not in SKIP_EXACT]
print(f"{len(models)} models (embeddings, image and compaction models skipped)\n")

print(f"  {'model':<26} {'buffered':<20} {'streamed':<20} endpoint")
print("  " + "-" * 80)
rows = []
for m in sorted(models):
    anth = "claude" in m
    sb, ub, eb = probe(m, anth, False)
    ss, us, es = probe(m, anth, True)
    nb, ns = nano(ub), nano(us)

    def fmt(status, u, n):
        if status != 200:
            return f"HTTP {status}"
        if u is None:
            return "NO copilot_usage"
        return f"nano={n:,}" if n else "nano=0 (quota)"

    rows.append((m, sb, ub, nb, ss, us, ns, eb if eb != "-" else es))
    print(f"  {m:<26} {fmt(sb, ub, nb):<20} {fmt(ss, us, ns):<20} {eb if eb != '-' else es}")

ok = [r for r in rows if r[1] == 200 or r[4] == 200]
both = [r for r in ok if r[2] is not None and r[5] is not None]
neither = [r for r in ok if r[2] is None and r[5] is None]
mismatch = [r for r in ok if (r[2] is None) != (r[5] is None)]
zero = [r for r in both if not r[3] and not r[6]]
unreachable = [r for r in rows if r[1] != 200 and r[4] != 200]

print()
print("=" * 82)
print(f"  reachable                     {len(ok)} / {len(rows)}")
print(f"  copilot_usage in both modes   {len(both)}")
print(f"  in neither mode               {len(neither)}")
print(f"  in ONLY one mode              {len(mismatch)}")
for r in mismatch:
    print(f"      {r[0]}: buffered={'yes' if r[2] else 'NO'} streamed={'yes' if r[5] else 'NO'}")
for r in neither:
    print(f"      no usage block: {r[0]}")
if zero:
    print(f"  nano_aiu = 0 in both (quota-billed, uses price table): {len(zero)}")
    for r in zero:
        print(f"      {r[0]}")
if unreachable:
    print(f"  unreachable on any endpoint: {len(unreachable)}")
    for r in unreachable:
        print(f"      {r[0]} (HTTP {r[1]})")
